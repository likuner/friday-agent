import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"


def setup_logging() -> None:
    """初始化 friday 根日志器：INFO 起控制台，DEBUG 起滚动文件 logs/backend.log。"""
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("friday")
    if logger.handlers:
        return
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(FORMAT)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_DIR / "backend.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
