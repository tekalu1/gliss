# -*- coding: utf-8 -*-
"""ログ。

stdio MCP の stderr はクライアントによっては捨てられるので、**ファイルに自前で書く**。
プロジェクトを開くまでは既定の場所に書き、開いたら <project>/engine.log に切り替える。
"""
import logging
import os
import sys

_LOGGER_NAME = "vocal_engine"
_DEFAULT_LOG = os.path.join(
    os.environ.get("VOCAL_ENGINE_LOG_DIR")
    or os.path.join(os.path.expanduser("~"), ".vocal-editor"),
    "engine.log",
)

_file_handler = None


def _logger():
    lg = logging.getLogger(_LOGGER_NAME)
    if not lg.handlers:
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        set_log_file(_DEFAULT_LOG)
    return lg


def set_log_file(path):
    """ログの出力先を切り替える（プロジェクトを開いたときに呼ぶ）。"""
    global _file_handler
    lg = logging.getLogger(_LOGGER_NAME)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        h = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    if _file_handler is not None:
        lg.removeHandler(_file_handler)
        try:
            _file_handler.close()
        except Exception:
            pass
    lg.addHandler(h)
    _file_handler = h
    return os.path.abspath(path)


def current_log_file():
    return getattr(_file_handler, "baseFilename", None)


def get(name=None):
    lg = _logger()
    return lg if not name else lg.getChild(name)


def log_exception(where, exc):
    import traceback
    get().error("%s: %s\n%s", where, exc, "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)))


def guard_stdout():
    """（mcp 2.x では不要）

    mcp 2.x の `mcp/server/stdio.py` は自分で fd 1 を掴んで stdout を退避させる
    （`_claim_fd` / `_open_stdout_diversion`）ので、こちら側で sys.stdout を
    差し替えると `stream.buffer` が無くて起動に失敗する。
    互換のため関数だけ残して何もしない。
    """
    return None
