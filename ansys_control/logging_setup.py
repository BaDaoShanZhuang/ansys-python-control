"""集中式日志系统:文件落盘 + 滚动 + 未捕获异常兜底。

设计要点:
- 日志写到用户配置目录下的 ``logs\app_YYYYMMDD.log``(与 settings.json 同目录,
  打包后也能稳定定位),按大小滚动,避免无限膨胀。
- 界面日志(``MainWindow.append_log``)与文件日志共用同一个 logger,一处产生、
  界面与文件同步。
- 安装 ``sys.excepthook`` 与 Qt 消息处理器,未捕获异常也会带完整堆栈进日志文件,
  而不是直接闪退丢失现场。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as app_config


LOGGER_NAME = "ansys_control"
_MAX_BYTES = 5 * 1024 * 1024  # 单个日志文件 5 MB
_BACKUP_COUNT = 10  # 最多保留 10 个历史文件
_CONFIGURED = False


def log_dir() -> Path:
    """日志目录:``%APPDATA%\\WindowsDuan\\logs``(随 config 的用户目录走)。"""
    return app_config.CONFIG_DIR / "logs"


def current_log_file() -> Path:
    return log_dir() / f"app_{datetime.now():%Y%m%d}.log"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging() -> logging.Logger:
    """初始化日志系统(幂等),返回应用 logger。"""
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    if _CONFIGURED:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件处理器:目录创建失败也不能让程序起不来,降级为仅控制台。
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            current_log_file(),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as exc:  # pragma: no cover - 仅在磁盘/权限异常时触发
        logger.addHandler(logging.StreamHandler(sys.stderr))
        logger.error("无法创建日志文件,已降级为控制台输出: %s", exc)

    # 控制台处理器:配合 启动并记录日志.bat 的 stdout 重定向兜底。
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    _install_excepthook()
    _CONFIGURED = True
    logger.info("日志系统已启动,日志文件: %s", current_log_file())
    return logger


def _install_excepthook() -> None:
    """未捕获异常带完整堆栈写入日志,而不是静默闪退。"""
    logger = logging.getLogger(LOGGER_NAME)
    previous_hook = sys.excepthook

    def handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "未捕获异常,程序即将异常退出",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = handler


def install_qt_message_handler() -> None:
    """把 Qt 自身的告警/错误也并入日志文件(可选,需在 QApplication 之前调用)。"""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # pragma: no cover
        return

    logger = logging.getLogger(LOGGER_NAME)
    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(msg_type, context, message):
        logger.log(level_map.get(msg_type, logging.INFO), "[Qt] %s", message)

    qInstallMessageHandler(handler)
