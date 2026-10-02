# -*- coding: utf-8 -*-
"""準備済みのトラックへの切り替え・一度組んだガイドの付け外しに関する高速化と整合性のテスト（#63）。

- SHA-256・音声ファイルの情報は、ファイルの署名（サイズ・更新時刻）が同じ間取り直さない
- 開いた Project をメモリに置き、選び直しで project.json を読み直さない（外部の書き換えは読み直す）
- キャッシュを読むだけの analyze_take はジョブにせずすぐ返し、何も変わらなければ project.json を書かない
- 描画データは入力が同じなら前に作ったものを返す（編集・外部の書き換え・ガイドで作り直す）
- 裏の準備が描画データも作っておき、選んだときにそのまま使われる
- 済みの印は組み合わせごとに残り、ガイドを戻しても準備をやり直さない
- 未保存の判定のハッシュは、変わったファイルだけ読み直して同じ値を出す
"""
import json
import os
import time

import numpy as np
import soundfile as sf

from test_prep import FREQ, Fake, _audio, _ok, _open, _ready, _state, _until, env  # noqa: F401


def _pdir(sdir, name):
    root = os.path.join(sdir, "tracks")
    [d] = [x for x in os.listdir(root) if x.startswith(name + "-")]
    return os.path.join(root, d)


def _analyze_now(m):
    """画面と同じく background=True で呼び、ジョブにならずにその場で返ることを確かめる。"""
    a = _ok(m.analyze_take(background=True))
    assert a.get("status") != "running" and "job_id" not in a, a
    return a


# ---------------------------------------------------------------- SHA-256・ファイルの情報
def test_sha256_is_reused_until_file_changes(tmp_path, monkeypatch):
    from vocal_engine import audio, media
    path = tmp_path / "x.wav"
    sf.write(path, np.zeros(1600), 16000)
    made = []
    real = audio.hashlib.sha256

    def counting():
        made.append(1)
        return real()
    monkeypatch.setattr(audio.hashlib, "sha256", counting)
    a = audio.sha256_file(str(path))
    assert audio.sha256_file(str(path)) == a and len(made) == 1
    info = media.file_info(str(path))
    assert info.frames == 1600 and media.file_info(str(path)) is info
    sf.write(path, np.ones(1600) * 0.5, 16000)          # 同じ大きさで中身を変える
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    b = audio.sha256_file(str(path))
    assert b != a and len(made) == 2
    sf.write(path, np.zeros(3200), 16000)
    assert media.file_info(str(path)).frames == 3200


def test_read_sound_matches_soundfile(tmp_path):
    from vocal_engine import media
    path = tmp_path / "s.wav"
    x = np.random.default_rng(0).uniform(-0.5, 0.5, (4000, 2))
    sf.write(path, x, 16000, subtype="PCM_24")
    for start, stop in ((0, None), (100, 2500)):
        want, sr = sf.read(str(path), start=start, stop=stop, dtype="float64", always_2d=True)
        got, sr2 = media.read_sound(str(path), start=start, stop=stop)
        assert sr == sr2 and np.array_equal(want, got)


# ---------------------------------------------------------------- Project をメモリに置く・書かない
def test_select_reuses_project_and_does_not_rewrite(env):
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    _ok(mt.select_track(tb))
    _analyze_now(m)
    pb = m._state["project"]
    pj = os.path.join(_pdir(sdir, "b"), "project.json")
    before = os.stat(pj).st_mtime_ns
    n0 = (len(fake.calls["f0"]), len(fake.calls["dtw"]))
    for _ in range(2):
        _ok(mt.select_track(t1))
        _analyze_now(m)
        r = _ok(mt.select_track(tb))
        assert r["analyzed"] is True
        _analyze_now(m)
        assert m._state["project"] is pb                   # 開き直さずにメモリのものを使う
    assert os.stat(pj).st_mtime_ns == before               # 何も変わらなければ書かない
    assert (len(fake.calls["f0"]), len(fake.calls["dtw"])) == n0

    # 外部（別のプロセスのエンジン）が書き換えたら、読み直した内容を使う
    from vocal_engine.project.store import Project
    _ok(mt.select_track(t1))
    other = Project(_pdir(sdir, "b")).load()
    other.set_lyrics("ら")
    r = _ok(mt.select_track(tb))
    assert m._state["project"].lyrics_text("take") == "ら"


def test_cached_analyze_waits_for_missing_alignment(env):
    """対応付けの鍵付きの保存が無い（まだ・前回失敗した）ときは、キャッシュを読むだけとは見なさない。"""
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    _until(_ready(prep, t1))
    p = m._state["project"]
    assert p.analysis_cached()
    adir = p._alignment_cache_dir()
    os.remove(os.path.join(adir, "alignment.json"))
    assert not p.analysis_cached()


# ---------------------------------------------------------------- 描画データ
def test_view_data_is_reused_and_rebuilt_when_inputs_change(env):
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    _until(_ready(prep, t1))
    _analyze_now(m)
    e1 = _ok(m.export_view_data())
    e2 = _ok(m.export_view_data())
    assert e2["cached"] is True and e2["path"] == e1["path"]
    assert e2["export_default_path"]
    # 編集したら作り直す
    nid = _ok(m.list_notes())["notes"][0]["id"]
    _ok(m.shift_pitch(30.0, note_id=nid))
    e3 = _ok(m.export_view_data())
    assert e3["cached"] is False and e3["path"] != e1["path"]
    with open(e3["path"], encoding="utf-8") as f:
        assert json.load(f)["edits"]
    # ガイドを変えて戻す: 前に作ったものを使う
    g2 = _ok(mt.add_track(paths["g2"]))["track"]
    g1 = r["session"]["guide"]
    _ok(mt.set_guide_track(g2))
    _until(_ready(prep, t1))
    _ok(m.analyze_take())
    e4 = _ok(m.export_view_data())
    assert e4["path"] != e3["path"]
    _ok(mt.set_guide_track(g1))
    _analyze_now(m)
    e5 = _ok(m.export_view_data())
    assert e5["cached"] is True and e5["path"] == e3["path"]
    # 外部（別のプロセス）が編集を足したら、読み直して作り直す
    from vocal_engine.project.store import Project
    other = Project(m._state["project"].dir).load()
    other.apply_edits([{"kind": "pitch_shift", "target": {"type": "note", "note_id": nid},
                        "params": {"cents": -20.0}}], author="ai")
    time.sleep(0.01)
    e6 = _ok(m.export_view_data())
    assert e6["cached"] is False
    with open(e6["path"], encoding="utf-8") as f:
        assert len(json.load(f)["edits"]) == 2


def test_prep_builds_view_data_for_select(env):
    """裏の準備が作った描画データを、選んだときにそのまま使う（表と裏で鍵が同じになる）。"""
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    vdir = os.path.join(_pdir(sdir, "b"), "cache", "view")
    assert any(n.endswith(".meta.json") for n in os.listdir(vdir))
    _ok(mt.select_track(tb))
    _analyze_now(m)
    e = _ok(m.export_view_data())
    assert e["cached"] is True
    with open(e["path"], encoding="utf-8") as f:
        d = json.load(f)
    assert d["project_dir"] == m._state["project"].dir and d["guide"] is not None


# ---------------------------------------------------------------- 済みの印を組み合わせごとに
def test_guide_back_and_forth_does_not_prepare_again(env):
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1, g1 = r["session"]["current"], r["session"]["guide"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    g2 = _ok(mt.add_track(paths["g2"]))["track"]
    _until(_ready(prep, t1, tb, g1, g2))
    for g in (g2, None, g1):                             # 1 周目: 組み合わせごとに準備する
        _ok(mt.set_guide_track(g))
        _until(_ready(prep, t1, tb, g1, g2))
        _ok(m.analyze_take())
    runs = len(prep.PREPARER.history)
    n0 = (len(fake.calls["f0"]), len(fake.calls["dtw"]))
    for g in (None, g2, g1, None, g1):
        r = _ok(mt.set_guide_track(g))
        assert r["reopened"] is True
        for t in (t1, tb):
            assert _state(prep, t)["state"] == "ready"          # すぐ準備済み（印が組み合わせごとにある）
        _analyze_now(m)
    time.sleep(0.2)
    assert (len(fake.calls["f0"]), len(fake.calls["dtw"])) == n0
    assert len(prep.PREPARER.history) == runs


# ---------------------------------------------------------------- 未保存の判定
def test_work_content_hash_matches_full_hash(tmp_path):
    from vocal_engine.project import document as D
    wd = tmp_path / "wd"
    (wd / "tracks" / "b").mkdir(parents=True)
    sess = {"format": "vocal-editor-session", "version": 1, "current": "t1", "guide": "t2",
            "tracks": [{"id": "t1", "project_dir": ".", "path": "C:/a/x.wav", "rel": "x.wav"},
                       {"id": "t2", "project_dir": "tracks/b", "path": "C:/a/y.wav"},
                       {"id": "t3", "project_dir": "tracks/none", "path": "C:/a/z.wav"}],
            "history": [{"kind": "session", "before": {"tracks": [{"project_dir": "tracks/b"}]},
                         "after": {"tracks": [{"project_dir": "tracks/none"}]}}]}
    pj = {"schema_version": 2, "dir": str(wd), "updated_at": "t", "take": {"path": "C:/a/x.wav"},
          "guide": {"path": "C:/a/y.wav"}, "lyrics": {"take": [{"text": "ら"}], "guide": [{"text": "り"}]},
          "edits": [{"id": "e001"}], "changesets": [{"id": "c001", "ops": []}], "analysis": {"take": {}}}
    empty = {"schema_version": 2, "take": {"path": "C:/a/y.wav"}, "edits": [], "changesets": [],
             "lyrics": {}}

    def write(path, d):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    write(wd / "session.json", sess)
    write(wd / "project.json", pj)
    write(wd / "tracks" / "b" / "project.json", empty)
    full = lambda: D.content_hash(D.collect(str(wd)))     # noqa: E731
    h = D.work_content_hash(str(wd))
    assert h == full()
    sess["current"] = "t2"                                # 揮発する値だけ: 同じ
    write(wd / "session.json", sess)
    assert D.work_content_hash(str(wd)) == h == full()
    pj["changesets"].append({"id": "c002", "ops": [{"op": "add"}]})
    write(wd / "project.json", pj)
    h2 = D.work_content_hash(str(wd))
    assert h2 != h and h2 == full()
    empty["edits"] = [{"id": "e009"}]
    write(wd / "tracks" / "b" / "project.json", empty)
    assert D.work_content_hash(str(wd)) == full() != h2
