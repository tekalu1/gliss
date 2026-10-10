# -*- coding: utf-8 -*-
"""起動直後の下ごしらえ（warmup.py。2026-10-10 承認）。"""
import importlib
import logging
import threading

import pytest


@pytest.fixture
def warmup(monkeypatch):
    from vocal_engine import warmup as w
    w = importlib.reload(w)              # スレッド・済みの印をテストごとに作り直す
    yield w
    importlib.reload(w)


def test_off_does_nothing(warmup, monkeypatch):
    monkeypatch.setenv("GLISS_WARMUP", "0")
    assert warmup.start() is None
    assert warmup.done()


def test_runs_once_without_engine_lock(warmup, monkeypatch, caplog):
    monkeypatch.setenv("GLISS_WARMUP", "1")
    from vocal_engine import mcp_server
    seen = []
    parts = ("_librosa", "_numba", "_onnx", "_hubertfa")
    for name in parts:
        def part(name=name):
            # エンジンのロックを取らない: ほかのスレッドが持っていても止まらない
            seen.append((name, threading.current_thread().name))
        monkeypatch.setattr(warmup, name, part)
    holder = threading.Thread(target=lambda: (mcp_server._lock.acquire(), ev.wait(5), mcp_server._lock.release()))
    ev = threading.Event()
    holder.start()
    try:
        warmup.mark_ready()
        th = warmup.start()
        assert warmup.start() is th
        assert warmup.wait(5)
    finally:
        ev.set()
        holder.join()
    assert [n for n, _ in seen] == list(parts)
    assert {t for _, t in seen} == {"gliss-warmup"}
    assert set(warmup.times) >= {"librosa", "numba", "onnx", "hubertfa", "warm"}


def test_failure_is_logged_and_skipped(warmup, monkeypatch, caplog):
    monkeypatch.setenv("GLISS_WARMUP", "1")

    def boom():
        raise RuntimeError("no weights")
    monkeypatch.setattr(warmup, "_librosa", lambda: None)
    monkeypatch.setattr(warmup, "_numba", lambda: None)
    monkeypatch.setattr(warmup, "_onnx", boom)
    monkeypatch.setattr(warmup, "_hubertfa", lambda: None)
    with caplog.at_level(logging.INFO):
        warmup.start()
        assert warmup.wait(5)
    assert "onnx" in warmup.times and "hubertfa" in warmup.times
