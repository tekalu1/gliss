# -*- coding: utf-8 -*-
"""別エンジンの保存と、外部で差し替わるファイルに対する回帰テスト（#63）。"""
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from test_prep import FREQ, _ok, _open, _ready, _until, env  # noqa: F401


def _project(tmp_path):
    from vocal_engine.project.store import Project

    wav = tmp_path / "voice.wav"
    sf.write(wav, np.zeros(16000), 16000)
    return Project.open(str(wav), project_dir=str(tmp_path / "project"), set_log=False, memo=False)


def _other(pdir, action="lyric"):
    import vocal_engine

    script = (
        "import sys\n"
        "from vocal_engine.project.store import Project\n"
        "p=Project(sys.argv[1]).load()\n"
        "if sys.argv[2]=='changeset':\n"
        "  entries=p.lyrics_entries('take')+[{'start_sec':1.0,'end_sec':1.5,'text':'い'}]\n"
        "  p.change_lyrics(entries)\n"
        "else:\n"
        "  p.set_lyrics('い',start_sec=1.0,end_sec=1.5)\n"
    )
    child_env = dict(os.environ)
    engine_dir = os.path.dirname(os.path.dirname(os.path.abspath(vocal_engine.__file__)))
    child_env["PYTHONPATH"] = os.pathsep.join(filter(None, [engine_dir, child_env.get("PYTHONPATH")]))
    subprocess.run([sys.executable, "-c", script, pdir, action], check=True, timeout=20,
                   env=child_env)


def _replace_json_same_stat(path, change):
    stat = os.stat(path)
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    change(obj)
    tmp = str(path) + ".replacement"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    assert os.path.getsize(tmp) == stat.st_size
    os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(tmp, path)
    assert os.stat(path).st_ino != stat.st_ino


def test_plain_save_conflict_keeps_other_process_edit(tmp_path):
    from vocal_engine.project.store import Project, ProjectConflict

    p = _project(tmp_path)
    p.analysis["writer"] = "A"
    _other(p.dir)
    with pytest.raises(ProjectConflict):
        p.save()
    assert [e["text"] for e in p.lyrics_entries("take")] == ["い"]
    assert [e["text"] for e in Project(p.dir).load().lyrics_entries("take")] == ["い"]


def test_analysis_summary_merges_into_other_process_edit(tmp_path):
    from vocal_engine.project.store import Project

    p = _project(tmp_path)
    p.analysis["take"] = {"n_frames": 12}
    _other(p.dir, "changeset")
    p._save_analysis()
    q = Project(p.dir).load()
    assert q.analysis["take"]["n_frames"] == 12
    assert len(q.changesets) == 1 and [e["text"] for e in q.lyrics_entries("take")] == ["い"]


def test_whole_lyrics_conflict_does_not_retry_old_array(tmp_path, monkeypatch):
    from vocal_engine.project.store import Project, ProjectConflict

    p = _project(tmp_path)
    original = Project.save

    def interrupted(self):
        _other(self.dir)
        return original(self)

    monkeypatch.setattr(Project, "save", interrupted)
    with pytest.raises(ProjectConflict):
        p.change_lyrics([{"start_sec": 0.1, "end_sec": 0.5, "text": "あ"}])
    assert [e["text"] for e in Project(p.dir).load().lyrics_entries("take")] == ["い"]


def test_undo_conflict_does_not_change_target(tmp_path, monkeypatch):
    from vocal_engine.project.store import Project, ProjectConflict

    p = _project(tmp_path)
    cs, _ = p.change_lyrics([{"start_sec": 0.1, "end_sec": 0.5, "text": "あ"}])
    original = Project.save

    def interrupted(self):
        _other(self.dir, "changeset")
        return original(self)

    monkeypatch.setattr(Project, "save", interrupted)
    with pytest.raises(ProjectConflict):
        p.undo()
    q = Project(p.dir).load()
    assert len(q.changesets) == 2 and not any(c.undone for c in q.changesets)
    assert q.changesets[0].id == cs.id


def test_redo_conflict_does_not_change_target(tmp_path, monkeypatch):
    from vocal_engine.project.store import Project, ProjectConflict

    p = _project(tmp_path)
    cs, _ = p.change_lyrics([{"start_sec": 0.1, "end_sec": 0.5, "text": "あ"}])
    p.undo()
    original = Project.save

    def interrupted(self):
        _other(self.dir, "changeset")
        return original(self)

    monkeypatch.setattr(Project, "save", interrupted)
    with pytest.raises(ProjectConflict):
        p.redo()
    q = Project(p.dir).load()
    assert len(q.changesets) == 2 and q.changesets[0].undone
    assert not q.changesets[1].undone and q.changesets[0].id == cs.id


def test_relative_edit_conflict_does_not_apply_stale_spec(tmp_path, monkeypatch):
    from vocal_engine.project.model import Target
    from vocal_engine.project.store import Project, ProjectConflict

    p = _project(tmp_path)
    original = Project.save

    def interrupted(self):
        _other(self.dir)
        return original(self)

    monkeypatch.setattr(Project, "save", interrupted)
    with pytest.raises(ProjectConflict):
        p.apply_changes([], [{"kind": "pitch_shift", "target": Target.range(0.1, 0.5),
                              "params": {"cents": 30.0}}])
    q = Project(p.dir).load()
    assert not q.edits and [e["text"] for e in q.lyrics_entries("take")] == ["い"]


def test_one_engine_sequential_edits_succeed(tmp_path):
    p = _project(tmp_path)
    p.set_lyrics("あ", start_sec=0.1, end_sec=0.5)
    p.set_lyrics("い", start_sec=1.0, end_sec=1.5)
    assert [e["text"] for e in p.lyrics_entries("take")] == ["あ", "い"]


def test_edit_preserves_background_analysis_only_save(tmp_path):
    from vocal_engine.project.model import Target
    from vocal_engine.project.store import Project

    p = _project(tmp_path)
    background = Project(p.dir).load()
    background.analysis["take"] = {"n_frames": 20}
    background.save()
    p.apply_changes([], [{"kind": "pitch_shift", "target": Target.range(0.1, 0.5),
                          "params": {"cents": 25.0}}])
    q = Project(p.dir).load()
    assert len(q.edits) == 1 and q.analysis["take"]["n_frames"] == 20


def test_mcp_marks_conflict(monkeypatch):
    from vocal_engine import bridge, mcp_server as m
    from vocal_engine.project.store import ProjectConflict

    monkeypatch.setattr(bridge, "is_app", lambda: True)

    @m._tool
    def conflicting():
        raise ProjectConflict("更新された")

    assert conflicting()["conflict"] is True and conflicting()["ok"] is False


def test_mcp_marks_preparation_pending(monkeypatch):
    from vocal_engine import bridge, mcp_server as m
    from vocal_engine.prep import PreparationPending

    monkeypatch.setattr(bridge, "is_app", lambda: True)

    @m._tool
    def waiting():
        raise PreparationPending("準備中")

    result = waiting()
    assert result["ok"] is False and result["preparing"] is True


def test_external_take_analysis_changes_view_key(env):
    m, _mt, prep, _fake, paths, sdir = env
    r = _open(m, paths, sdir)
    _until(_ready(prep, r["session"]["current"]))
    p = m._state["project"]
    before = _ok(m.export_view_data())
    path = p._cache_path("take-analysis.json")
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    obj["f0"]["f0"][0] += 10
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
    after = _ok(m.export_view_data())
    assert after["path"] != before["path"] and after["cached"] is False
    assert p.take_f0.f0[0] == obj["f0"]["f0"][0]


def test_external_guide_alignment_and_onsets_change_view_key(env):
    m, _mt, prep, _fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    _until(_ready(prep, r["session"]["current"]))
    _ok(m.analyze_take())
    p = m._state["project"]
    previous = _ok(m.export_view_data())["path"]
    files = [p._srcs["guide"][0], p._srcs["alignment"][0],
             os.path.join(p._guide_cache_path, "onsets.json")]
    for index, path in enumerate(files):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if index == 0:
            data["f0"]["f0"][0] += 10
        elif index == 1:
            data["take_sec"] = [v + 0.0001 for v in data["take_sec"]]
        else:
            data["sec"].append(0.1234)
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        result = _ok(m.export_view_data())
        assert result["path"] != previous and result["cached"] is False
        if index == 0:
            assert p.guide_f0.f0[0] == data["f0"]["f0"][0]
        elif index == 1:
            assert p.alignment.take_sec[0] == data["take_sec"][0]
        else:
            assert p.onsets("guide")[-1] == 0.1234
        previous = result["path"]


def test_external_phoneme_cache_change_is_loaded(env, monkeypatch):
    from vocal_engine.phoneme import analyze as PA
    from vocal_engine.phoneme.model import PhonemeResult

    def align(_x, _sr, entries, f0r=None, source="take", duration_sec=0.0, **_kw):
        text = "".join(e["text"] for e in entries)
        return PhonemeResult(source=source, lyrics=text, kana=text, phonemes=[], boundaries=[],
                             entries=list(entries), duration_sec=duration_sec)

    monkeypatch.setattr(PA, "align_lyrics_ranges", align)
    m, _mt, prep, _fake, paths, sdir = env
    r = _open(m, paths, sdir)
    _until(_ready(prep, r["session"]["current"]))
    p = m._state["project"]
    p.set_lyrics("あ", start_sec=0.1, end_sec=0.5)
    p.ensure_analyzed()
    p.analyze_phonemes("take", force=True)
    previous = _ok(m.export_view_data())["path"]
    path = p._srcs["ph_take"][0]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["kana"] = "い"
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    result = _ok(m.export_view_data())
    assert result["path"] != previous and result["cached"] is False
    assert p._phonemes["take"].kana == "い"


def test_replaced_audio_invalidates_open_project_and_sha(tmp_path):
    from vocal_engine.audio import sha256_file

    p = _project(tmp_path)
    before, _ = p.audio()
    sha = sha256_file(p.take["path"])
    path = p.take["path"]
    stat = os.stat(path)
    tmp = str(tmp_path / "replacement.wav")
    sf.write(tmp, np.ones(16000) * 0.5, 16000)
    assert os.path.getsize(tmp) == stat.st_size
    os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(tmp, path)
    after, _ = p.audio()
    assert not np.array_equal(before, after)
    assert sha256_file(path) != sha and p._media_changed


def test_replaced_audio_rebuilds_analysis_and_view(env, tmp_path):
    m, _mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir)
    _until(_ready(prep, r["session"]["current"]))
    p = m._state["project"]
    before = _ok(m.export_view_data())
    path = p.take["path"]
    stat = os.stat(path)
    tmp = str(tmp_path / "new.wav")
    shutil.copyfile(paths["b"], tmp)
    os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(tmp, path)
    after = _ok(m.export_view_data())
    assert after["path"] != before["path"] and after["cached"] is False
    assert p.take_f0.f0[0] == FREQ["b"] and fake.f0_count("b") >= 1


def test_same_size_same_time_project_replacement_is_seen(tmp_path):
    from vocal_engine.project.store import Project, cached_project, remember_open

    p = _project(tmp_path)
    p.analysis["writer"] = "A"
    p.save()
    remember_open(p)
    path = p.json_path
    _replace_json_same_stat(path, lambda d: d["analysis"].update(writer="B"))
    assert cached_project(p.dir) is None
    assert p.reload_if_changed() and p.analysis["writer"] == "B"
    assert Project(p.dir).load().analysis["writer"] == "B"


def test_same_size_same_time_document_replacement_is_seen(tmp_path):
    from vocal_engine.project.document import Document

    wd = tmp_path / "work"
    wd.mkdir()
    path = wd / "session.json"
    path.write_text(json.dumps({"tracks": [], "name": "A"}, indent=2), encoding="utf-8")
    doc = Document("gliss", str(wd))
    before = doc.current_hash()
    _replace_json_same_stat(path, lambda d: d.update(name="B"))
    assert doc.current_hash() != before


def test_corrupt_same_size_analysis_is_reprepared(env):
    m, _mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir)
    tid = r["session"]["current"]
    _until(_ready(prep, tid))
    s = m._state["session"]
    t = s.track(tid)
    pdir = s.project_dir_of(t)
    sig = prep.track_sig(s, t)
    path = os.path.join(pdir, "cache", "take-analysis.json")
    with open(path, "r+b") as f:
        f.write(b"!")
    assert not prep.stamp_ok(pdir, sig)
    calls = len(fake.calls["f0"])
    _ok(m.analyze_take(background=False))
    assert prep.stamp_ok(pdir, sig) and len(fake.calls["f0"]) > calls


def test_front_and_sync_join_have_deadlines(tmp_path, monkeypatch):
    from vocal_engine import prep

    p = prep.Preparer()
    it = prep._Item("session", "t1", str(tmp_path / "track"), "sig")
    p._sdir = "session"
    p._items["t1"] = it
    monkeypatch.setattr(p, "_ensure_thread", lambda: None)
    monkeypatch.setattr(p, "_must_wait", lambda _key: it)
    monkeypatch.setattr(prep, "FRONT_WAIT_SEC", 0.02, raising=False)
    pending = getattr(prep, "PreparationPending", Exception)
    with pytest.raises(pending):
        with p.front(it.pdir):
            pass
    with pytest.raises(pending):
        p.join("session", "t1")
    assert not p._joining


def test_deferred_delete_survives_restart_and_respects_lock(tmp_path, monkeypatch):
    from vocal_engine.project import document as D
    from vocal_engine.project.store import FileLock

    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "root"))
    wd = tmp_path / "root" / "untitled-test"
    child = wd / "tracks" / "one"
    child.mkdir(parents=True)
    (child / "project.json").write_text("{}", encoding="utf-8")
    lk = FileLock(str(child / "project.lock"))
    assert lk.try_acquire()
    try:
        assert D.remove_work(str(wd)) == "deferred"
        assert os.path.isfile(D._pending_path(str(wd)))
        D.cleanup_pending()
        assert wd.exists()
    finally:
        lk.release()
    subprocess.run([sys.executable, "-c",
                    "from vocal_engine.project.document import cleanup_pending; cleanup_pending()"],
                   check=True, timeout=20)
    assert not wd.exists() and not os.path.exists(D._pending_path(str(wd)))


def test_view_key_uses_format_and_packaged_build_version(tmp_path, monkeypatch):
    from vocal_engine.project import store
    from vocal_engine.view import export_data

    p = store.Project(str(tmp_path / "empty"))
    first = p.view_key()
    monkeypatch.setattr(export_data, "VIEW_DATA_VERSION", export_data.VIEW_DATA_VERSION + 1)
    assert p.view_key() != first
    version = tmp_path / "gliss-build-version.txt"
    version.write_text("build-A", encoding="ascii")
    monkeypatch.setattr(store.sys, "frozen", True, raising=False)
    monkeypatch.setattr(store.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(store, "_code_sig_value", None)
    a = p.view_key()
    version.write_text("build-B", encoding="ascii")
    monkeypatch.setattr(store, "_code_sig_value", None)
    assert p.view_key() != a
