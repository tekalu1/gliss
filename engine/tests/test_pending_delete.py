# -*- coding: utf-8 -*-
"""後回しにした作業場所の削除と、開き直し時の取り消しに関するテスト（#63）。"""
import json
import os

import pytest

from test_prep import _ok, _open, _ready, _until, env  # noqa: F401


def _hold(path):
    from vocal_engine.project.store import FileLock

    lk = FileLock(str(path))
    assert lk.try_acquire()
    return lk


def test_reopened_work_dir_is_not_removed_by_pending_delete(tmp_path, monkeypatch):
    """ロック中に延期した作業場所を開き直したら、後の片付けで消さない。"""
    from vocal_engine.project import document as D

    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "root"))
    wd = tmp_path / "root" / "reopened"
    wd.mkdir(parents=True)
    lk = _hold(wd / "project.lock")
    try:
        assert D.remove_work(str(wd)) == "deferred"
    finally:
        lk.release()
    marker = D._pending_path(str(wd))
    assert os.path.exists(marker)
    assert json.load(open(marker, encoding="utf-8")).get("gen")
    assert D.claim_work(str(wd)) is True           # 開き直した（作業場所を使い始めた）
    (wd / "project.json").write_text('{"active":true}', encoding="utf-8")
    D.cleanup_pending()
    assert (wd / "project.json").exists()
    assert not os.path.exists(marker)
    assert not (wd / D.PENDING_TOKEN).exists()


def test_pending_delete_checks_generation_before_removing(tmp_path, monkeypatch):
    """片付けの直前にも世代を照らし合わせる。ロックが取れても、予定と中の世代が違えば消さない。"""
    from vocal_engine.project import document as D

    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "root"))
    wd = tmp_path / "root" / "other-gen"
    wd.mkdir(parents=True)
    lk = _hold(wd / "project.lock")
    try:
        assert D.remove_work(str(wd)) == "deferred"
    finally:
        lk.release()
    (wd / D.PENDING_TOKEN).write_text("someone-else", encoding="utf-8")
    D.cleanup_pending()
    assert wd.exists()
    assert not os.path.exists(D._pending_path(str(wd)))
    # 世代が合っていてロックも取れれば消す（ふだんの片付けは変わらない）
    lk = _hold(wd / "project.lock")
    try:
        assert D.remove_work(str(wd)) == "deferred"
    finally:
        lk.release()
    D.cleanup_pending()
    assert not wd.exists()


def test_load_project_cancels_pending_delete_of_untitled(env):
    """実際の経路: 無題を「保存しない」で閉じた（ロック中で後回し）→ load_project で同じ作業場所を開き直す
    → ロックが外れた後の片付けでも消えない。"""
    from vocal_engine import mcp_document as md
    from vocal_engine.project import document as D

    m, _mt, prep, _fake, paths, _sdir = env
    r = _ok(md.new_project(take_path=paths["take"]))
    wd = md.current().work_dir
    _until(_ready(prep, r["session"]["current"]))
    lk = _hold(os.path.join(wd, "project.lock"))
    try:
        r = _ok(md.close_project(discard=True))
        assert r["removal"] == "deferred"
        assert os.path.exists(D._pending_path(wd))
        _ok(md.load_project(wd))
    finally:
        lk.release()
    assert not os.path.exists(D._pending_path(wd))
    D.cleanup_pending()
    assert os.path.isdir(wd) and os.path.exists(os.path.join(wd, "session.json"))
    assert md.current() is not None and os.path.normcase(md.current().work_dir) == os.path.normcase(wd)
    _ok(m.list_notes())                            # 開き直した作業場所はそのまま使える
