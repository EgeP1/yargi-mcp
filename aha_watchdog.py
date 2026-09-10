"""AHA bekçi (2026-09-10): süreç ASKIDA kalınca (TCP kabul, 0 bayt yanıt, /health dahil) kendini öldürür.

2026-09-10 13:30 TR: konteyner ~2,5 saat cevapsız kaldı; Railway 502 vermedi (süreç ayaktaydı), restart
politikası tetiklenmedi (çökme yok). Railway healthcheck yalnız deploy anında koşar, sürekli izleme değil.
Bu bekçi ayrı bir THREAD'de (event loop bloklansa da çalışır) kendi /health'ine bakar; ardışık
AHA_WATCHDOG_FAILS başarısızlıkta os._exit(1) → uvicorn hata koduyla çıkar → Railway ON_FAILURE restart.

Env: AHA_WATCHDOG=0 kapatır · AHA_WATCHDOG_INTERVAL_S (30) · AHA_WATCHDOG_TIMEOUT_S (10) · AHA_WATCHDOG_FAILS (3)
"""

import logging
import os
import threading
import time

logger = logging.getLogger("aha.watchdog")


def _health_url() -> str:
    port = os.getenv("PORT", "8000")
    return f"http://127.0.0.1:{port}/health"


def _loop(interval_s: float, timeout_s: float, max_fails: int) -> None:
    import httpx  # thread içinde import: başlangıcı geciktirmesin

    fails = 0
    time.sleep(interval_s)  # uvicorn dinlemeye başlasın
    while True:
        try:
            r = httpx.get(_health_url(), timeout=timeout_s)
            if r.status_code == 200:
                if fails:
                    logger.warning("aha.watchdog: /health toparladı (%d başarısızlık sonrası)", fails)
                fails = 0
            else:
                fails += 1
                logger.error("aha.watchdog: /health HTTP %s (%d/%d)", r.status_code, fails, max_fails)
        except Exception as e:  # noqa: BLE001 — her hata bir başarısızlık sayılır
            fails += 1
            logger.error("aha.watchdog: /health cevapsız: %s (%d/%d)", type(e).__name__, fails, max_fails)
        if fails >= max_fails:
            logger.critical("aha.watchdog: süreç ASKIDA — %d ardışık başarısızlık → os._exit(1) (Railway restart)", fails)
            os._exit(1)
        time.sleep(interval_s)


def start_watchdog() -> None:
    if os.getenv("AHA_WATCHDOG", "1") == "0":
        logger.info("aha.watchdog: kapalı (AHA_WATCHDOG=0)")
        return
    interval_s = float(os.getenv("AHA_WATCHDOG_INTERVAL_S", "30"))
    timeout_s = float(os.getenv("AHA_WATCHDOG_TIMEOUT_S", "10"))
    max_fails = int(os.getenv("AHA_WATCHDOG_FAILS", "3"))
    t = threading.Thread(
        target=_loop, args=(interval_s, timeout_s, max_fails), name="aha-watchdog", daemon=True
    )
    t.start()
    logger.info(
        "aha.watchdog: açık — %s her %.0fs, zaman aşımı %.0fs, %d ardışık başarısızlıkta çıkış",
        _health_url(), interval_s, timeout_s, max_fails,
    )
