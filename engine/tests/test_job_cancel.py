"""長い処理の進捗と中断（issue #36）。"""
import time
import threading
from pathlib import Path

import pytest

from conftest import needs_clips, needs_model
from vocal_engine import mcp_server as m


def test_cancellable_job_stops_before_next_step():
    first = False
    finished = False

    def work(cancel, progress, commit):
        nonlocal first, finished
        progress(0.25)
        first = True
        while not cancel.is_set():
            time.sleep(0.01)
        progress(0.5)  # 中断を受け付ける安全な境界
        finished = True

    job = m._submit_job("test", work, cancellable=True)
    for _ in range(100):
        if first:
            break
        time.sleep(0.01)
    assert first
    status = m.get_job(job_id=job["job_id"])
    assert status["progress"] == 0.25 and status["cancellable"] is True
    assert m.cancel_job(job_id=job["job_id"])["status"] == "canceling"
    for _ in range(100):
        status = m.get_job(job_id=job["job_id"])
        if status["status"] == "canceled":
            break
        time.sleep(0.01)
    assert status["status"] == "canceled"
    assert finished is False


def test_non_cancellable_job_rejects_cancel():
    job = m._submit_job("test", lambda: {"value": 1})
    assert m.cancel_job(job_id=job["job_id"])["ok"] is False


def test_cancel_before_commit_prevents_persistent_action():
    ready = threading.Event()
    proceed = threading.Event()
    saved = []

    def work(cancel, progress, commit):
        ready.set()
        assert proceed.wait(2)
        commit(lambda: saved.append("written"))
        return {"saved": True}

    job_id = m._submit_job("test-commit", work, cancellable=True)["job_id"]
    assert ready.wait(2)
    assert m.cancel_job(job_id=job_id)["status"] == "canceling"
    proceed.set()
    for _ in range(100):
        result = m.get_job(job_id=job_id)
        if result["status"] == "canceled":
            break
        time.sleep(0.01)
    assert result["status"] == "canceled"
    assert saved == []


def test_committed_job_rejects_cancel_before_result_is_published():
    committed = threading.Event()
    proceed = threading.Event()
    saved = []

    def work(cancel, progress, commit):
        commit(lambda: saved.append("written"))
        committed.set()
        assert proceed.wait(2)
        return {"saved": True}

    job_id = m._submit_job("test-commit", work, cancellable=True)["job_id"]
    assert committed.wait(2)
    state = m.get_job(job_id=job_id)
    assert state["status"] == "running" and state["cancellable"] is False
    assert m.cancel_job(job_id=job_id)["ok"] is False
    proceed.set()
    for _ in range(100):
        result = m.get_job(job_id=job_id)
        if result["status"] == "done":
            break
        time.sleep(0.01)
    assert result["status"] == "done"
    assert saved == ["written"]


@needs_clips
@needs_model
def test_export_cancel_discards_temporary_file(project, tmp_path):
    from vocal_engine.render.export import export_wav

    project.analyze()
    target = tmp_path / "canceled.wav"

    def stop_at_write(value):
        if value >= 0.96:
            raise m.JobCancelled()

    with pytest.raises(m.JobCancelled):
        export_wav(project, path=str(target), progress=stop_at_write)
    assert not target.exists()
    assert not Path(str(target) + ".tmp.wav").exists()


@needs_clips
@needs_model
def test_export_cancel_at_commit_discards_temporary_file(project, tmp_path):
    from vocal_engine.render.export import export_wav

    project.analyze()
    target = tmp_path / "commit-canceled.wav"

    def canceled_before_replace(action):
        raise m.JobCancelled()

    with pytest.raises(m.JobCancelled):
        export_wav(project, path=str(target), commit=canceled_before_replace)
    assert not target.exists()
    assert not Path(str(target) + ".tmp.wav").exists()
