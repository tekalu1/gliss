# -*- coding: utf-8 -*-
"""裏の準備（`vocal_engine/prep.py`）のテスト（#63）。

- 足した・開いたトラックを裏で解析し、選んだときの analyze_take はキャッシュを読むだけ
- 準備の間も編集のツールはすぐ返る（エンジンのロックを握らない）
- 準備中のトラックを選んで analyze_take すると合流する（計算は 1 回）。合流を取り消しても準備は続く
- ガイドを変えたら古い組み合わせの準備はやめる
- 準備と表の操作・外部の書き換えが同時でも project.json が壊れず、編集・歌詞が消えない
- 準備が失敗したときの状態

解析（F0・対応付け・発音の頭）は偽物に差し替え、段ごとに止められるようにしてある。
"""
import json
import os
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis.align import Alignment
from vocal_engine.analysis.f0 import F0Result

FREQ = {"take": 220, "b": 440, "c": 550, "g1": 330, "g2": 660}


def _audio(tmp_path, name):
    path = tmp_path / (name + ".wav")
    sr = 16000
    t = np.arange(sr * 2) / sr
    sf.write(path, 0.3 * np.sin(2 * np.pi * FREQ[name] * t), sr)
    return str(path)


def _freq_of(x, sr):
    return int(round(np.fft.rfftfreq(sr, 1 / sr)[np.argmax(np.abs(np.fft.rfft(x[:sr])))]))


class Fake:
    """偽の解析。gate[(段, 周波数)] に Event を入れると、その段がそこで止まる（set で進む）。"""

    def __init__(self):
        self.calls = {"f0": [], "dtw": [], "onsets": 0}
        self.gate = {}
        self.entered = {}
        self.fail = set()
        self.lock = threading.Lock()

    def _wait(self, key):
        ev = self.gate.get(key)
        self.entered.setdefault(key, threading.Event()).set()
        if ev is not None:
            assert ev.wait(20), "gate %s が開かなかった" % (key,)

    def estimate(self, x, sr, estimator="rmvpe", sweep=False):
        f = _freq_of(x, sr)
        with self.lock:
            self.calls["f0"].append(f)
        self._wait(("f0", f))
        if ("f0", f) in self.fail:
            raise RuntimeError("偽の F0 の失敗（%d Hz）" % f)
        n = int(np.floor(len(x) / sr / 0.01)) + 1
        return F0Result(np.full(n, float(f)), np.ones(n), np.ones(n, dtype=bool),
                        np.full(n, -20.0), sr=sr, estimator=estimator,
                        meta={"sweep": sweep, "threshold": 0.03,
                              "vuv_rule": "%s f0>0 AND rms > -55.0 dBFS" % estimator})

    def align(self, project, method):
        key = (int(project.take_f0.f0[0]), int(project.guide_f0.f0[0]))
        with self.lock:
            self.calls["dtw"].append(key)
        self._wait(("dtw", key[0]))
        return Alignment.from_seconds(np.array([0.0, 1.0]), np.array([0.0, 1.0]), method=method)

    def detect(self, x, sr):
        with self.lock:
            self.calls["onsets"] += 1
        return np.array([0.25, 0.75])

    def f0_count(self, name):
        return self.calls["f0"].count(FREQ[name])


@pytest.fixture
def env(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    from vocal_engine import prep
    from vocal_engine.analysis import onsets
    from vocal_engine.project import align_helper, store
    monkeypatch.setenv("VOCAL_ENGINE_PREP", "1")
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))   # 人の作業場所に作らない
    fake = Fake()
    monkeypatch.setattr(store, "estimate_f0", fake.estimate)
    monkeypatch.setattr(align_helper, "compute_alignment", fake.align)
    monkeypatch.setattr(onsets, "detect", fake.detect)
    paths = {k: _audio(tmp_path, k) for k in FREQ}
    yield m, mt, prep, fake, paths, str(tmp_path / "song")
    for ev in fake.gate.values():
        ev.set()
    prep.reset()
    _until(lambda: prep.PREPARER._running is None)
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _until(pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("時間切れ")


def _state(prep, tid):
    for t in prep.overview()["tracks"]:
        if t["id"] == tid:
            return t
    return None


def _ready(prep, *tids):
    return lambda: all((_state(prep, t) or {}).get("state") == "ready" for t in tids)


def _open(m, paths, sdir, guide=None):
    r = _ok(m.open_project(paths["take"], guide_path=paths[guide] if guide else None,
                           project_dir=sdir))
    return r


def _tid(r, name):
    for t in r["session"]["tracks"]:
        if t["name"] == name:
            return t["id"]
    raise KeyError(name)


def _job(m, jid, timeout=15.0):
    return _until(lambda: (lambda j: j if j["status"] != "running" else None)(m.get_job(jid)), timeout)


# ---------------------------------------------------------------- 足したら裏で準備・選んだらキャッシュ
def test_added_track_is_prepared_and_select_uses_cache(env):
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1, g = r["session"]["current"], r["session"]["guide"]
    _ok(m.analyze_take())                        # 今のトラック: 準備に合流（計算は 1 回）
    r = _ok(mt.add_track(paths["b"]))
    tb = r["track"]
    _until(_ready(prep, t1, tb, g))
    assert fake.f0_count("take") == 1 and fake.f0_count("b") == 1
    # ガイドの F0: 今のトラック（最優先）のためにガイドとして 1 回、ガイドのトラック自身のテイクとして 1 回。
    # 後から準備した b は、ガイドのトラックの F0 を使い回す
    assert fake.f0_count("g1") == 2
    assert sorted(fake.calls["dtw"]) == [(220, 330), (440, 330)]
    lt = _ok(mt.list_tracks())
    prepd = {t["id"]: t["prep"] for t in lt["tracks"]}
    assert prepd[tb]["state"] == "ready" and prepd[t1]["state"] == "ready"
    # 準備は project.json のガイドの写しを書き換えない（選んだときに合わせる）
    pb = os.path.join(sdir, "tracks")
    [bdir] = [d for d in os.listdir(pb) if d.startswith("b-")]
    assert os.path.exists(os.path.join(pb, bdir, "cache", "prepared.json"))
    assert not os.path.exists(os.path.join(pb, bdir, "cache", "alignment.json"))

    n0 = (len(fake.calls["f0"]), len(fake.calls["dtw"]))
    r = _ok(mt.select_track(tb))
    assert r["analyzed"] is True
    a = _ok(m.analyze_take(background=True))
    if a.get("status") == "running":
        a = _job(m, a["job_id"])
    assert a.get("joined") is None               # 準備済み: 合流しない
    assert (len(fake.calls["f0"]), len(fake.calls["dtw"])) == n0
    assert a["alignment"] is not None
    assert os.path.exists(os.path.join(pb, bdir, "cache", "alignment.json"))


def test_stamp_survives_restart(env):
    """済みの印（cache/prepared.json）があれば、エンジンを開き直してもやり直さない。"""
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    n = len(fake.calls["f0"]) + len(fake.calls["dtw"])
    prep.reset()
    m._state.update(project=None, session=None, track=None)
    _open(m, paths, sdir)
    assert _state(prep, tb)["state"] == "ready"
    _until(_ready(prep, t1, tb))
    time.sleep(0.2)
    assert len(fake.calls["f0"]) + len(fake.calls["dtw"]) == n


# ---------------------------------------------------------------- ロックを握らない
def test_edit_tools_return_while_preparing(env):
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    nid = [n for n in _ok(m.list_notes())["notes"]][0]["id"]
    fake.gate[("f0", FREQ["b"])] = threading.Event()
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(lambda: fake.entered.get(("f0", FREQ["b"])))
    st = _state(prep, tb)
    assert st["state"] == "preparing" and st["stage"] == "take_f0" and st["stage_label"] == "テイクの音程"
    times = []
    for cents in (10.0, 20.0, 30.0):
        t0 = time.perf_counter()
        _ok(m.shift_pitch(cents, note_id=nid))
        times.append(time.perf_counter() - t0)
    for fn in (m.export_view_data, m.list_changes, mt.list_tracks):
        t0 = time.perf_counter()
        _ok(fn())
        times.append(time.perf_counter() - t0)
    assert max(times) < 2.0, times
    assert _state(prep, tb)["state"] == "preparing"          # 準備はまだ止まったまま
    fake.gate[("f0", FREQ["b"])].set()
    _until(_ready(prep, tb))
    assert len(_ok(m.list_changes())["edits"]) >= 1


# ---------------------------------------------------------------- 合流
def test_select_joins_running_prep_and_computes_once(env):
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir, guide="g1")
    _ok(m.analyze_take())
    gate = fake.gate[("dtw", FREQ["b"])] = threading.Event()
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(lambda: fake.entered.get(("dtw", FREQ["b"])))
    r = _ok(mt.select_track(tb))
    assert r["analyzed"] is False
    j = _ok(m.analyze_take(background=True))
    assert j["status"] == "running"
    g = _until(lambda: (lambda x: x if x.get("stage") else None)(m.get_job(j["job_id"])))
    assert g["joined"] is True and g["stage"] == "alignment" and g["stage_label"] == "ガイドとの対応付け"
    assert g["progress"] is not None and 0.5 <= g["progress"] < 1.0
    # 待っている間もエンジンのロックは空いている
    t0 = time.perf_counter()
    _ok(mt.list_tracks())
    assert time.perf_counter() - t0 < 1.0
    gate.set()
    done = _job(m, j["job_id"])
    assert done["status"] == "done", done
    assert done["alignment"] is not None
    assert fake.f0_count("b") == 1
    assert fake.calls["dtw"].count((440, 330)) == 1


def test_cancel_join_keeps_prep_running(env):
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    gate = fake.gate[("f0", FREQ["b"])] = threading.Event()
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(lambda: fake.entered.get(("f0", FREQ["b"])))
    _ok(mt.select_track(tb))
    j = _ok(m.analyze_take(background=True))
    assert j["status"] == "running" and j["cancellable"] is True
    _ok(m.cancel_job(j["job_id"]))
    assert _job(m, j["job_id"])["status"] == "canceled"
    assert _state(prep, tb)["state"] == "preparing"
    gate.set()
    _until(_ready(prep, tb))
    assert fake.f0_count("b") == 1
    a = _ok(m.analyze_take())
    assert a["notes"]["total"] >= 1 and fake.f0_count("b") == 1


def test_sync_analyze_take_joins(env):
    """同期の analyze_take（MCP の既定。短い素材）も合流する。"""
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    gate = fake.gate[("f0", FREQ["take"])] = threading.Event()
    _until(lambda: fake.entered.get(("f0", FREQ["take"])))
    threading.Timer(0.3, gate.set).start()
    a = _ok(m.analyze_take())
    assert a["notes"]["total"] >= 1
    assert fake.f0_count("take") == 1


def test_current_track_goes_first(env):
    """今のトラックが待っていれば、ほかのトラックの準備は段の境目で譲る。"""
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    gb = fake.gate[("f0", FREQ["b"])] = threading.Event()
    tb = _ok(mt.add_track(paths["b"]))["track"]
    tc = _ok(mt.add_track(paths["c"]))["track"]
    _until(lambda: fake.entered.get(("f0", FREQ["b"])))
    _ok(mt.select_track(tc))
    gb.set()                                     # b は F0 の段を終えたところで c に譲る
    _until(_ready(prep, tc))
    _until(_ready(prep, tb))
    hist = [h["track"] for h in prep.PREPARER.history if h["outcome"] in ("ready", "queued")]
    assert hist.index(tc) < len(hist) - 1 and hist[-1] == tb
    assert fake.f0_count("b") == 1 and fake.f0_count("c") == 1   # 譲った後も F0 はやり直さない


# ---------------------------------------------------------------- 取り消し
def test_guide_change_cancels_old_combination(env):
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    _ok(m.analyze_take())
    g2 = _ok(mt.add_track(paths["g2"]))["track"]
    _until(_ready(prep, g2))
    gate = fake.gate[("dtw", FREQ["b"])] = threading.Event()
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(lambda: fake.entered.get(("dtw", FREQ["b"])))
    sig_old = prep.PREPARER._items[tb].sig
    _ok(mt.set_guide_track(g2))
    assert prep.PREPARER._items[tb].sig != sig_old
    gate.set()
    _until(_ready(prep, tb))
    outcomes = [(h["track"], h["outcome"], h["sig"]) for h in prep.PREPARER.history if h["track"] == tb]
    assert (tb, "cancel", sig_old) in outcomes
    assert fake.calls["dtw"].count((440, 330)) == 1          # 古い組み合わせは 1 回で止まった
    assert fake.calls["dtw"].count((440, 660)) == 1
    assert fake.f0_count("b") == 1                           # テイクの F0 は残っていて使い回す
    # 新しい組み合わせで準備済み（選べばキャッシュ）
    n = len(fake.calls["dtw"])
    _ok(mt.select_track(tb))
    _ok(m.analyze_take())
    assert len(fake.calls["dtw"]) == n
    # 古い組み合わせに戻しても、鍵付きで保存した分は使う（対応付けは途中で止めたが、結果は保存してある）
    _ok(mt.set_guide_track(_tid(r, "g1")))
    _until(_ready(prep, tb))
    _ok(m.analyze_take())
    assert fake.calls["dtw"].count((440, 330)) == 1


def test_undo_guide_change_reschedules(env):
    m, mt, prep, fake, paths, sdir = env
    r = _open(m, paths, sdir, guide="g1")
    _ok(m.analyze_take())
    tb = _ok(mt.add_track(paths["b"]))["track"]
    g2 = _ok(mt.add_track(paths["g2"]))["track"]
    _until(_ready(prep, tb, g2))
    _ok(mt.set_guide_track(g2))
    _until(_ready(prep, tb))
    assert fake.calls["dtw"].count((440, 660)) == 1
    _ok(m.undo())                                # ガイドの指定を取り消す → g1 に戻る
    assert prep.PREPARER._guide == _tid(r, "g1")
    _until(_ready(prep, tb))
    assert fake.calls["dtw"].count((440, 330)) == 1           # 前の組み合わせは鍵付きで残っている


def test_external_session_change_reschedules(env):
    """session.json を外部（別のエンジン）が書き換えた: 読み直したときに準備を入れ直す。"""
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    from vocal_engine.project.session import Session
    s2 = Session.load(sdir)
    s2.add_track(paths["b"])
    time.sleep(0.02)
    s2.save()
    lt = _ok(mt.list_tracks())
    tb = [t["id"] for t in lt["tracks"] if t["name"] == "b"][0]
    _until(_ready(prep, tb))
    assert fake.f0_count("b") == 1


# ---------------------------------------------------------------- project.json を壊さない・消さない
def test_prep_merges_with_concurrent_edits_and_lyrics(env, monkeypatch):
    """準備の間に表（編集対象）と外部が同じトラックの project.json を書いても、どちらも残る。
    歌詞の自動推定は、その間に入った歌詞と重ならない区間だけ入る。"""
    m, mt, prep, fake, paths, sdir = env
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "1")
    from vocal_engine.phoneme import auto_lyrics, hubertfa
    from vocal_engine.phoneme import analyze as PA

    class Syl:
        def __init__(self, kana):
            self.kana, self.confidence = kana, 0.9

    infer_calls = []

    def infer(x, sr):
        infer_calls.append(len(x))
        return [Syl("ら")]

    monkeypatch.setattr(hubertfa, "model_found", lambda *a, **k: True)
    monkeypatch.setattr(auto_lyrics, "utterance_ranges",
                        lambda notes, dur, **k: [(0.1, 0.8, None), (1.1, 1.8, None)])
    monkeypatch.setattr(auto_lyrics, "infer", infer)

    def no_align(*a, **k):
        raise RuntimeError("偽: 音素は切らない")
    monkeypatch.setattr(PA, "align_lyrics_ranges", no_align)

    _open(m, paths, sdir)
    _ok(m.analyze_take())
    gate = fake.gate[("f0", FREQ["b"])] = threading.Event()
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(lambda: fake.entered.get(("f0", FREQ["b"])))
    # 準備中のトラックを表で選び、歌詞を入れる（表の project.json の保存）
    _ok(mt.select_track(tb))
    _ok(m.set_lyrics("あ", start_sec=1.0, end_sec=1.5, reanalyze=False))
    # 外部（別のプロセスの Project）が同じトラックに編集を足す
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    bdir = m._state["project"].dir
    ext = Project(bdir).load()
    ext.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.2, 0.6),
                      "params": {"cents": 25.0}}], author="ai")
    gate.set()
    _until(_ready(prep, tb))
    with open(os.path.join(bdir, "project.json"), encoding="utf-8") as f:
        d = json.load(f)                          # 壊れていない
    texts = [(e["start_sec"], e["text"], e.get("origin")) for e in d["lyrics"]["take"]]
    assert (1.0, "あ", "confirmed") in texts      # 表の歌詞は残る
    assert (0.1, "ら", "estimated") in texts      # 重ならない推定は入る
    assert not any(t[0] == 1.1 for t in texts)    # 重なる推定は捨てる
    assert d["analysis"]["auto_lyrics"]["attempted"] is True
    assert len(d["edits"]) == 1                   # 外部の編集は残る
    assert d["analysis"]["take"]["n_frames"] > 0
    n_infer = len(infer_calls)
    a = _ok(m.analyze_take())                     # 表: 推定をやり直さない
    assert len(infer_calls) == n_infer
    assert a["notes"]["total"] >= 1
    p = m._state["project"]
    assert len(p.edits) == 1 and any(e["text"] == "あ" for e in p.lyrics_entries("take"))


def test_edit_during_prep_of_current_track_is_kept(env):
    """今のトラックの準備中（合流を待たずに）編集ツールを呼んでも、準備の書き込みで消えない。"""
    m, mt, prep, fake, paths, sdir = env
    gate = fake.gate[("f0", FREQ["take"])] = threading.Event()
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    _until(lambda: fake.entered.get(("f0", FREQ["take"])))
    threading.Timer(0.3, gate.set).start()
    # list_notes は解析を要する: 準備の終わりを待って（表で 2 回計算せずに）返る
    notes = _ok(m.list_notes())["notes"]
    assert fake.f0_count("take") == 1
    _ok(m.shift_pitch(15.0, note_id=notes[0]["id"]))
    _until(_ready(prep, t1))
    from vocal_engine.project import Project
    assert len(Project(m._state["project"].dir).load().edits) == 1


# ---------------------------------------------------------------- 失敗
def test_failed_prep_state_and_retry_on_select(env):
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    fake.fail.add(("f0", FREQ["b"]))
    tb = _ok(mt.add_track(paths["b"]))["track"]
    st = _until(lambda: (lambda s: s if s and s["state"] == "failed" else None)(_state(prep, tb)))
    assert "偽の F0 の失敗" in st["error"]
    lt = _ok(mt.list_tracks())
    assert [t["prep"] for t in lt["tracks"] if t["id"] == tb][0]["state"] == "failed"
    _ok(mt.select_track(tb))
    r = m.analyze_take()                          # 合流してもう一度やり、それでも失敗 → 表でもやって同じ理由
    assert r.get("ok") is False and "偽の F0 の失敗" in r["error"]
    fake.fail.clear()
    a = _ok(m.analyze_take())                     # 直れば通る
    assert a["notes"]["total"] >= 1
    assert _state(prep, tb)["state"] == "ready"


def test_force_analyze_does_not_race_with_prep(env):
    """force の analyze_take は準備に合流せず表で計算する。裏の準備はそのトラックを譲る。"""
    m, mt, prep, fake, paths, sdir = env
    gate = fake.gate[("f0", FREQ["take"])] = threading.Event()
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    _until(lambda: fake.entered.get(("f0", FREQ["take"])))
    threading.Timer(0.3, gate.set).start()
    _ok(m.analyze_take(force=True))
    _until(lambda: prep.PREPARER._running is None)
    st = _state(prep, t1)
    assert st["state"] in ("ready", "queued")
    _until(_ready(prep, t1))


def test_pause_and_foreground_yield(env):
    m, mt, prep, fake, paths, sdir = env
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    _ok(m.pause_prep(True))
    tb = _ok(mt.add_track(paths["b"]))["track"]
    time.sleep(0.3)
    assert _state(prep, tb)["state"] == "queued" and fake.f0_count("b") == 0
    _ok(m.pause_prep(False))
    _until(_ready(prep, tb))
    # 表の重い処理の間は、次の段へ進まない
    gate = fake.gate[("f0", FREQ["c"])] = threading.Event()
    tc = _ok(mt.add_track(paths["c"]))["track"]
    _until(lambda: fake.entered.get(("f0", FREQ["c"])))
    with prep.foreground():
        gate.set()
        _until(lambda: (_state(prep, tc) or {}).get("paused"))
        n = fake.calls["onsets"]
        time.sleep(0.2)
        assert _state(prep, tc)["state"] == "preparing"
    _until(_ready(prep, tc))
    assert fake.calls["onsets"] >= n


def test_save_as_moves_work_dir_while_preparing(env, tmp_path):
    """無題を名前を付けて保存すると作業場所が移る: 古い作業場所の準備はやめ、新しい作業場所で準備する。"""
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine import mcp_document as md
    _ok(md.new_project())
    gate = fake.gate[("f0", FREQ["b"])] = threading.Event()
    _ok(mt.add_track(paths["take"], select=True))
    _ok(m.analyze_take())
    old_wd = md.current().work_dir
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(lambda: fake.entered.get(("f0", FREQ["b"])))
    threading.Timer(0.3, gate.set).start()
    r = _ok(md.save_project(path=str(tmp_path / "song.gliss")))
    assert r["moved"] is True and not os.path.exists(old_wd)
    assert old_wd.startswith(str(tmp_path / "work"))
    new_wd = md.current().work_dir
    assert prep.overview()["session_dir"] == os.path.abspath(new_wd)
    _until(_ready(prep, tb))
    _ok(md.close_project())
    assert prep.overview()["session_dir"] is None


def test_other_process_preparing_is_deferred(env, monkeypatch):
    """別のプロセスのエンジン（画面と Claude Code）が同じトラックを準備中（prep.lock を握っている）なら、同時には
    やらずに後回しにする。preparing.json は状態を見る人向けの印で、それだけ（落ちたプロセスが残した印）では
    止めない（別のプロセスのロックは test_prep_integrity.py）。"""
    m, mt, prep, fake, paths, sdir = env
    monkeypatch.setattr(prep, "DEFER_SEC", 0.2)
    _open(m, paths, sdir)
    _ok(m.analyze_take())
    _ok(m.pause_prep(True))
    r = _ok(mt.add_track(paths["b"]))
    tb = r["track"]
    bdir = [t["project_dir"] for t in r["session"]["tracks"] if t["id"] == tb][0]
    os.makedirs(os.path.join(bdir, "cache"), exist_ok=True)
    lk = prep.FileLock(os.path.join(bdir, prep.PREP_LOCK_FILE))   # 別のハンドルは同じプロセスでもぶつかる
    assert lk.try_acquire()
    _ok(m.pause_prep(False))
    time.sleep(0.8)
    assert _state(prep, tb)["state"] == "queued" and fake.f0_count("b") == 0
    lk.release()
    _until(_ready(prep, tb))
    assert fake.f0_count("b") == 1
    # 落ちたプロセスが残した新しい印だけでは止めない
    _ok(m.pause_prep(True))
    tc = _ok(mt.add_track(paths["c"]))["track"]
    cdir = [t["project_dir"] for t in _ok(mt.list_tracks())["tracks"] if t["id"] == tc][0]
    os.makedirs(os.path.join(cdir, "cache"), exist_ok=True)
    marker = os.path.join(cdir, "cache", prep.RUNNING_FILE)
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid() + 100000, "at": time.time()}, f)
    _ok(m.pause_prep(False))
    _until(_ready(prep, tc))
    assert fake.f0_count("c") == 1
    assert not os.path.exists(marker)                         # 自分の印は終わったら消す


def test_test_delay_slows_only_matching_tracks(env, monkeypatch):
    """画面のテスト用の遅らせ（VOCAL_ENGINE_PREP_TEST_DELAY・_MATCH。issue #63 の 4）: 合うトラックだけ段ごとに待ち、
    その間は準備中の段が見え、合流を取り消しても準備は続いて済む。"""
    m, mt, prep, fake, paths, sdir = env
    monkeypatch.setenv("VOCAL_ENGINE_PREP_TEST_DELAY", "0.3")
    monkeypatch.setenv("VOCAL_ENGINE_PREP_TEST_MATCH", "B.WAV")      # 大文字小文字は見ない
    assert prep._test_delay(paths["take"]) == 0.0
    assert prep._test_delay(paths["b"]) == 0.3
    _open(m, paths, sdir)
    t0 = time.time()
    _ok(m.analyze_take())
    assert time.time() - t0 < 1.0                # 合わないトラックは待たない
    tb = _ok(mt.add_track(paths["b"]))["track"]
    st = _until(lambda: (_state(prep, tb) or {}).get("stage") and _state(prep, tb))
    assert st["state"] == "preparing" and st["stage_label"]
    _ok(mt.select_track(tb))
    a = _ok(m.analyze_take(background=True))
    assert a["status"] == "running"
    j = _until(lambda: (lambda x: x if x.get("joined") else None)(m.get_job(a["job_id"])))
    assert j["status"] == "running"
    _ok(m.cancel_job(a["job_id"]))
    _until(lambda: m.get_job(a["job_id"])["status"] == "canceled")
    _until(_ready(prep, tb))
    assert time.time() - t0 > 1.0
