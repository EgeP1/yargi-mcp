# bedesten_mcp_module/client.py

import asyncio
import base64
import io
import logging
import os
import time
from typing import Optional

import httpx
from markitdown import MarkItDown

from .models import (
    BedestenSearchRequest, BedestenSearchResponse,
    BedestenDocumentRequest, BedestenDocumentResponse,
    BedestenDocumentMarkdown, BedestenDocumentRequestData
)
from .enums import get_full_birim_adi

logger = logging.getLogger(__name__)


class BedestenRateLimited(Exception):
    """Raised when the local rate-limit bucket would block longer than allowed.

    Carries the suggested retry-after (seconds) so callers can surface a
    structured 429-style response to the MCP client instead of silently
    blocking the event-loop slot for the full bucket-pause window.
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"local bucket would block {retry_after:.1f}s")


class _SlidingWindow:
    """Asyncio sliding-window limiter mirroring the MEASURED Bedesten limit.

    2026-09-17 measurement (AHA worker, 3 bucket settings -> identical 42 s):
    Bedesten accepts ~7 requests per rolling 30 s window per source IP, no
    burst. A token bucket (capacity/refill) only approximates this; the
    window is exact, so requests wait precisely until the oldest slot
    expires instead of a fixed spacing.

    429 handling (2026-10-05, replaces the hard freeze): a server 429 used to
    call ``penalize_until`` which drained the bucket and paused EVERY caller
    for Retry-After (15-30 s). Now ``penalize`` pauses briefly
    (``BEDESTEN_429_PAUSE_S``, default 5 s) and lowers the effective limit by
    ``BEDESTEN_429_LIMIT_DROP`` (default 2) for one full window after the
    pause, so throughput degrades smoothly instead of stopping. Total time
    for a burst of N requests is unchanged (still bounded by limit/window).
    """

    def __init__(self, limit: int, window_s: float,
                 pause_429_s: float = 5.0, limit_drop_429: int = 2) -> None:
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self.pause_429_s = float(pause_429_s)
        self.limit_drop_429 = max(0, int(limit_drop_429))
        self._granted: list = []  # monotonic timestamps of granted slots
        self._not_before = 0.0
        self._reduced_until = 0.0
        self._lock = asyncio.Lock()

    def _effective_limit(self, now: float) -> int:
        if now < self._reduced_until:
            return max(1, self.limit - self.limit_drop_429)
        return self.limit

    def _purge(self, now: float) -> None:
        edge = now - self.window_s
        while self._granted and self._granted[0] <= edge:
            self._granted.pop(0)

    async def acquire(self, max_wait: Optional[float] = None) -> None:
        """Acquire one slot. With ``max_wait`` set, raise
        :class:`BedestenRateLimited` instead of sleeping longer than that."""
        deadline = (time.monotonic() + max_wait) if max_wait is not None else None
        while True:
            async with self._lock:
                now = time.monotonic()
                self._purge(now)
                if now < self._not_before:
                    wait_s = self._not_before - now
                elif len(self._granted) < self._effective_limit(now):
                    self._granted.append(now)
                    return
                else:
                    wait_s = (self._granted[0] + self.window_s) - now
                wait_s = max(wait_s, 0.05)
            if deadline is not None and wait_s > (deadline - time.monotonic()):
                raise BedestenRateLimited(retry_after=wait_s)
            await asyncio.sleep(wait_s)

    def penalize(self, retry_after: float) -> float:
        """Server said 429. Short pause + reduced limit for one window.
        Returns the pause actually applied (seconds)."""
        now = time.monotonic()
        pause = min(max(retry_after, 0.0), self.pause_429_s)
        self._not_before = max(self._not_before, now + pause)
        self._reduced_until = max(self._reduced_until, now + pause + self.window_s)
        return pause

    # Backward-compatible name (old token bucket API): hard pause until deadline.
    def penalize_until(self, monotonic_deadline: float) -> None:
        self._not_before = max(self._not_before, monotonic_deadline)


class BedestenApiClient:
    """
    API Client for Bedesten (bedesten.adalet.gov.tr) - Alternative legal decision search system.
    Currently used for Yargıtay decisions, but can be extended for other court types.
    """
    BASE_URL = "https://bedesten.adalet.gov.tr"
    SEARCH_ENDPOINT = "/emsal-karar/searchDocuments"
    DOCUMENT_ENDPOINT = "/emsal-karar/getDocumentContent"
    
    # Measured limit (per source IP): 10 requests per 30s window with full
    # refill (≈ 1 token / 3s steady). We default to 1-token capacity and
    # 3.5s spacing (no burst, ~14% safety margin). Override via env:
    #   BEDESTEN_RATE_CAPACITY (default 1)
    #   BEDESTEN_RATE_REFILL_S (default 3.5; seconds per token)
    #   BEDESTEN_RATE_MAX_WAIT_S (default 8.0; max seconds to wait in the
    #     local bucket before returning a structured 429 to the caller)
    # 2026-10-05: sliding window (measured 7 req / 30 s rolling, no burst).
    # Old BEDESTEN_RATE_CAPACITY / BEDESTEN_RATE_REFILL_S are no longer read.
    #   BEDESTEN_WINDOW_LIMIT (default 7)   requests per window
    #   BEDESTEN_WINDOW_S     (default 30)  window length, seconds
    #   BEDESTEN_429_PAUSE_S  (default 5)   short pause after a server 429
    #   BEDESTEN_429_LIMIT_DROP (default 2) limit reduction for one window after 429
    #   BEDESTEN_RATE_MAX_WAIT_S (default 8.0; unchanged) max local wait before
    #     returning a structured 429 to the caller
    _WINDOW_LIMIT = int(os.getenv("BEDESTEN_WINDOW_LIMIT", "7"))
    _WINDOW_S = float(os.getenv("BEDESTEN_WINDOW_S", "30"))
    _PAUSE_429_S = float(os.getenv("BEDESTEN_429_PAUSE_S", "5"))
    _LIMIT_DROP_429 = int(os.getenv("BEDESTEN_429_LIMIT_DROP", "2"))
    _DEFAULT_MAX_WAIT_S = float(os.getenv("BEDESTEN_RATE_MAX_WAIT_S", "8.0"))

    def __init__(self, request_timeout: float = 60.0):
        self.http_client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "*/*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "AdaletApplicationName": "UyapMevzuat",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": "https://mevzuat.adalet.gov.tr",
                "Referer": "https://mevzuat.adalet.gov.tr/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            },
            timeout=request_timeout
        )
        self._bucket = _SlidingWindow(
            limit=self._WINDOW_LIMIT,
            window_s=self._WINDOW_S,
            pause_429_s=self._PAUSE_429_S,
            limit_drop_429=self._LIMIT_DROP_429,
        )

    def _handle_429(self, response: httpx.Response, op: str) -> None:
        """Apply back-pressure to the shared bucket based on Retry-After."""
        retry_after_raw = response.headers.get("Retry-After", "")
        try:
            retry_after = float(retry_after_raw)
        except (TypeError, ValueError):
            retry_after = 30.0
        # Cap so a hostile/buggy server can't freeze us; the window then
        # applies only a short pause + reduced limit (smooth degradation).
        retry_after = max(1.0, min(retry_after, 60.0))
        pause = self._bucket.penalize(retry_after)
        logger.warning(
            f"BedestenApiClient: 429 on {op}; window paused {pause:.1f}s, "
            f"limit reduced by {self._LIMIT_DROP_429} for {self._WINDOW_S:.0f}s"
        )
    
    async def search_documents(self, search_request: BedestenSearchRequest) -> BedestenSearchResponse:
        """
        Search for documents using Bedesten API.
        Currently supports: YARGITAYKARARI, DANISTAYKARARI, YERELHUKMAHKARARI, etc.
        """
        logger.info(f"BedestenApiClient: Searching documents with phrase: {search_request.data.phrase}")
        
        # Map abbreviated birimAdi to full Turkish name before sending to API
        original_birim_adi = search_request.data.birimAdi
        mapped_birim_adi = get_full_birim_adi(original_birim_adi)
        search_request.data.birimAdi = mapped_birim_adi
        if original_birim_adi != "ALL":
            logger.info(f"BedestenApiClient: Mapped birimAdi '{original_birim_adi}' to '{mapped_birim_adi}'")
        
        try:
            # Create request dict and remove birimAdi if empty
            # exclude_none: esas/karar filtre alanları yalnız verildiğinde gövdeye girer (AHA 2026-09-26)
            request_dict = search_request.model_dump(exclude_none=True)
            if not request_dict["data"].get("birimAdi"):  # Remove if empty string
                request_dict["data"].pop("birimAdi", None)
            
            await self._bucket.acquire(max_wait=self._DEFAULT_MAX_WAIT_S)
            response = await self.http_client.post(
                self.SEARCH_ENDPOINT,
                json=request_dict
            )
            if response.status_code == 429:
                self._handle_429(response, "search")
            response.raise_for_status()
            response_json = response.json()
            # data=null + metadata.FMTY=ERROR = Bedesten parametre doğrulama hatası (örn. bölülü rakam frazı);
            # sessizce "No data" olmasın, sebebi logla (AHA 2026-09-26)
            meta = response_json.get("metadata") if isinstance(response_json, dict) else None
            if response_json.get("data") is None and isinstance(meta, dict) and meta.get("FMTY") == "ERROR":
                logger.warning(f"BedestenApiClient: search returned ERROR metadata: {meta.get('FMC')} — {meta.get('FMTE')}")

            # Parse and return the response
            return BedestenSearchResponse(**response_json)

        except httpx.RequestError as e:
            logger.error(f"BedestenApiClient: HTTP request error during search: {e}")
            raise
        except Exception as e:
            logger.error(f"BedestenApiClient: Error processing search response: {e}")
            raise
    
    async def get_document_as_markdown(self, document_id: str) -> BedestenDocumentMarkdown:
        """
        Get document content and convert to markdown.
        Handles both HTML (text/html) and PDF (application/pdf) content types.
        """
        logger.info(f"BedestenApiClient: Fetching document for markdown conversion (ID: {document_id})")
        
        try:
            # Prepare request
            doc_request = BedestenDocumentRequest(
                data=BedestenDocumentRequestData(documentId=document_id)
            )
            
            # Get document
            await self._bucket.acquire(max_wait=self._DEFAULT_MAX_WAIT_S)
            response = await self.http_client.post(
                self.DOCUMENT_ENDPOINT,
                json=doc_request.model_dump()
            )
            if response.status_code == 429:
                self._handle_429(response, f"document {document_id}")
            response.raise_for_status()
            response_json = response.json()
            doc_response = BedestenDocumentResponse(**response_json)
            
            # Add null safety checks for document data
            if not hasattr(doc_response, 'data') or doc_response.data is None:
                raise ValueError("Document response does not contain data")
            
            if not hasattr(doc_response.data, 'content') or doc_response.data.content is None:
                raise ValueError("Document data does not contain content")
                
            if not hasattr(doc_response.data, 'mimeType') or doc_response.data.mimeType is None:
                raise ValueError("Document data does not contain mimeType")
            
            # Decode base64 content with error handling
            try:
                content_bytes = base64.b64decode(doc_response.data.content)
            except Exception as e:
                raise ValueError(f"Failed to decode base64 content: {str(e)}")
            
            mime_type = doc_response.data.mimeType
            
            logger.info(f"BedestenApiClient: Document mime type: {mime_type}")
            
            # Convert to markdown based on mime type. markitdown is sync and
            # PDF parsing in particular can block the event-loop for seconds,
            # which on a single-worker uvicorn deployment stalls every other
            # in-flight MCP request and new TLS handshakes. Offload to a
            # thread so the event-loop stays responsive.
            if mime_type == "text/html":
                html_content = content_bytes.decode('utf-8')
                markdown_content = await asyncio.to_thread(
                    self._convert_html_to_markdown, html_content
                )
            elif mime_type == "application/pdf":
                markdown_content = await asyncio.to_thread(
                    self._convert_pdf_to_markdown, content_bytes
                )
            else:
                logger.warning(f"Unsupported mime type: {mime_type}")
                markdown_content = f"Unsupported content type: {mime_type}. Unable to convert to markdown."
            
            return BedestenDocumentMarkdown(
                documentId=document_id,
                markdown_content=markdown_content,
                source_url=f"https://mevzuat.adalet.gov.tr/ictihat/{document_id}",
                mime_type=mime_type
            )
            
        except httpx.RequestError as e:
            logger.error(f"BedestenApiClient: HTTP error fetching document {document_id}: {e}")
            raise
        except Exception as e:
            logger.error(f"BedestenApiClient: Error processing document {document_id}: {e}")
            raise
    
    def _convert_html_to_markdown(self, html_content: str) -> Optional[str]:
        """Convert HTML to Markdown using MarkItDown"""
        if not html_content:
            return None
            
        try:
            # Convert HTML string to bytes and create BytesIO stream
            html_bytes = html_content.encode('utf-8')
            html_stream = io.BytesIO(html_bytes)
            
            # Pass BytesIO stream to MarkItDown to avoid temp file creation
            md_converter = MarkItDown()
            result = md_converter.convert(html_stream)
            markdown_content = result.text_content
            
            logger.info("Successfully converted HTML to Markdown")
            return markdown_content
            
        except Exception as e:
            logger.error(f"Error converting HTML to Markdown: {e}")
            return f"Error converting HTML content: {str(e)}"
    
    def _convert_pdf_to_markdown(self, pdf_bytes: bytes) -> Optional[str]:
        """Convert PDF to Markdown using MarkItDown"""
        if not pdf_bytes:
            return None
            
        try:
            # Create BytesIO stream from PDF bytes
            pdf_stream = io.BytesIO(pdf_bytes)
            
            # Pass BytesIO stream to MarkItDown to avoid temp file creation
            md_converter = MarkItDown()
            result = md_converter.convert(pdf_stream)
            markdown_content = result.text_content
            
            logger.info("Successfully converted PDF to Markdown")
            return markdown_content
            
        except Exception as e:
            logger.error(f"Error converting PDF to Markdown: {e}")
            return f"Error converting PDF content: {str(e)}. The document may be corrupted or in an unsupported format."
    
    async def close_client_session(self):
        """Close HTTP client session"""
        await self.http_client.aclose()
        logger.info("BedestenApiClient: HTTP client session closed.")