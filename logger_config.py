import logging
import sys
from logging.handlers import RotatingFileHandler
from colorlog import ColoredFormatter

def setup_logging():
    logger = logging.getLogger("sync_system")
    logger.setLevel(logging.INFO)

    # защита от дублирования строк, если функция вызовется еще раз
    if logger.hasHandlers():
        return logger

    # цветной формат для терминала
    console_format = ColoredFormatter(
        "%(log_color)s%(levelname)-8s%(reset)s %(threadName)20s - %(message)-120s - %(asctime)s (%(filename)s:%(lineno)d)",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "red,bg_white",
        }
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # обычный формат для файла
    file_format = logging.Formatter(
        "%(asctime)s %(threadName)20s %(levelname)-8s %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        "sync_system.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    return logger