import logging
import sys
from pathlib import Path
from config import LOG_LEVEL

LOG_FILE = Path(__file__).parent.parent / "bot.log"
MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB max log file


def setup_logger(name: str = "polymarket_bot") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File handler (for dashboard)
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass  # Non-fatal: dashboard just won't show logs

    return logger
