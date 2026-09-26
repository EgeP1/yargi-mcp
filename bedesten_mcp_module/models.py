# bedesten_mcp_module/models.py

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal, Union
from datetime import datetime

# Import compressed BirimAdiEnum for chamber filtering
from .enums import BirimAdiEnum

# Court Type Options for Unified Search
BedestenCourtTypeEnum = Literal[
    "YARGITAYKARARI",  # Yargıtay (Court of Cassation)
    "DANISTAYKARAR",   # Danıştay (Council of State)
    "YERELHUKUK",      # Local Civil Courts
    "ISTINAFHUKUK",    # Civil Courts of Appeals
    "KYB"              # Extraordinary Appeals (Kanun Yararına Bozma)
]

# Search Request Models
class BedestenSearchData(BaseModel):
    pageSize: int = Field(..., description="Results per page (1-10)")
    pageNumber: int = Field(..., description="Page number (1-indexed)")
    itemTypeList: List[str] = Field(..., description="Court type filter (YARGITAYKARARI/DANISTAYKARAR/YERELHUKUK/ISTINAFHUKUK/KYB)")
    phrase: str = Field(..., description="Search phrase. Supports: 'word', \"exact phrase\", +required, -exclude, AND/OR/NOT operators. No wildcards or regex.")
    birimAdi: BirimAdiEnum = Field("ALL", description="""
        Chamber filter (optional). Abbreviated values with Turkish names:
        • Yargıtay: H1-H23 (1-23. Hukuk Dairesi), C1-C23 (1-23. Ceza Dairesi), HGK (Hukuk Genel Kurulu), CGK (Ceza Genel Kurulu), BGK (Büyük Genel Kurulu), HBK (Hukuk Daireleri Başkanlar Kurulu), CBK (Ceza Daireleri Başkanlar Kurulu)
        • Danıştay: D1-D17 (1-17. Daire), DBGK (Büyük Gen.Kur.), IDDK (İdare Dava Daireleri Kurulu), VDDK (Vergi Dava Daireleri Kurulu), IBK (İçtihatları Birleştirme Kurulu), IIK (İdari İşler Kurulu), DBK (Başkanlar Kurulu), AYIM (Askeri Yüksek İdare Mahkemesi), AYIM1-3 (Askeri Yüksek İdare Mahkemesi 1-3. Daire)
        """)
    kararTarihiStart: Optional[str] = Field(None, description="Start date (ISO 8601 format)")
    kararTarihiEnd: Optional[str] = Field(None, description="End date (ISO 8601 format)")
    sortFields: List[str] = Field(default=["KARAR_TARIHI"], description="Sort fields")
    sortDirection: str = Field(default="desc", description="Sort direction (asc/desc)")
    # AHA 2026-09-26: Bedesten esas/karar numarasını KESİN filtre olarak kabul ediyor (doğrudan API probe:
    # esasNoYil=2025+esasNoSira=7717 → total=1, 5/5 bilinen karar). "2025/7717" FRAZI ise artık
    # ADALET_PARAMETER_VALIDATION_EXCEPTION ("Lütfen arama kriteri girin") veriyor — bölü işareti frazı boşa
    # düşürüyor. Alanlar phrase boşken de reddediliyor → çağıran evrensel bir jeton ('"karar"') göndermeli.
    esasNoYil: Optional[int] = Field(None, description="Esas (case file) year, e.g. 2025 — exact filter")
    esasNoSira: Optional[int] = Field(None, description="Esas sequence number, e.g. 7717 — exact filter")
    kararNoYil: Optional[int] = Field(None, description="Karar (decision) year — exact filter")
    kararNoSira: Optional[int] = Field(None, description="Karar sequence number — exact filter")

class BedestenSearchRequest(BaseModel):
    data: BedestenSearchData
    applicationName: str = "UyapMevzuat"
    paging: bool = True

# Search Response Models
class BedestenItemType(BaseModel):
    name: str
    description: str

class BedestenDecisionEntry(BaseModel):
    documentId: str
    itemType: BedestenItemType
    birimId: Optional[str] = None
    birimAdi: Optional[str]
    esasNoYil: Optional[int] = None
    esasNoSira: Optional[int] = None
    kararNoYil: Optional[int] = None
    kararNoSira: Optional[int] = None
    kararTuru: Optional[str] = None
    kararTarihi: str
    kararTarihiStr: str
    kesinlesmeDurumu: Optional[str] = None
    kararNo: Optional[str] = None
    esasNo: Optional[str] = None

class BedestenSearchDataResponse(BaseModel):
    emsalKararList: List[BedestenDecisionEntry]
    total: int
    start: int

class BedestenSearchResponse(BaseModel):
    data: Optional[BedestenSearchDataResponse]
    metadata: Dict[str, Any]

# Document Request/Response Models
class BedestenDocumentRequestData(BaseModel):
    documentId: str

class BedestenDocumentRequest(BaseModel):
    data: BedestenDocumentRequestData
    applicationName: str = "UyapMevzuat"

class BedestenDocumentData(BaseModel):
    content: str  # Base64 encoded HTML or PDF
    mimeType: str
    version: int

class BedestenDocumentResponse(BaseModel):
    data: BedestenDocumentData
    metadata: Dict[str, Any]

class BedestenDocumentMarkdown(BaseModel):
    documentId: str = Field(..., description="The document ID (Belge Kimliği) from Bedesten")
    markdown_content: Optional[str] = Field(None, description="The decision content (Karar İçeriği) converted to Markdown")
    source_url: str = Field(..., description="The source URL (Kaynak URL) of the document")
    mime_type: Optional[str] = Field(None, description="Original content type (İçerik Türü) (text/html or application/pdf)")