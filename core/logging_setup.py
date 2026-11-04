"""日誌設定：寫入 logs/app.log (輪替)，不輸出到終端機以免干擾 Rich 介面"""
import logging
from logging.handlers import RotatingFileHandler

import config

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        config.LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.addHandler(handler)
    # 第三方套件的 debug 訊息過多，維持 WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "paramiko", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
