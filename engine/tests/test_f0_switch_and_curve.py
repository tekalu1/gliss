# -*- coding: utf-8 -*-
"""F0 の方式の切り替え・タイミングの組み直し・ピッチ曲線の重ねの回帰（合成の音だけ。素材も RMVPE の重みも要らない）。

- `analyze_take(force=true)` だけでは Gliss の第 2 版の曲を第 3 版で解析し直さない（estimator も明示したときだけ）
- 方式・版を替える前に、ノートの ID を対象にした編集を今の解析での区間の範囲対象へ付け替える（音は変わらない）
- 利用者が明示した方式は、`ara_render_dirty` の方式探し（`_fit_estimator`）で戻さない。当たらない編集は missing
- 離れた 2 か所のタイミングを 1 回で動かしても、間の動かないノートの編集は組み直さない（音もサンプル一致）
- `pitch_curve` を重ねても、隣のノートの `pitch_shift` の量が漏れない（曲線の点列の時刻が昇順のまま）
- 中継（外部の AI）のジョブの `analyze_take(estimator=…)` も、選んだ修飾の方式として覚える
"""
import time

import numpy as np
import pytest
import soundfile as sf

from test_ara_tools import Cache, _add, _ok, _open, _wav

SR = 44100
# (頭の秒, 長さ, MIDI)。つながった 4 音・3 音・2 音のフレーズ（フレーズの間は無音）
LEGATO = [(0.50, 0.40, 60), (0.90, 0.40, 64), (1.30, 0.40, 62), (1.70, 0.45, 67),
          (2.60, 0.40, 65), (3.00, 0.40, 62), (3.40, 0.50, 60), (4.30, 0.40, 64), (4.70, 0.45, 67)]
DUR = 5.6


def _legato(seed=0):
    """倍音のある音を音程だけ段で替えて続けて鳴らす（つながったノートの境目が接続になる）。"""
    n = int(SR * DUR)
    f0 = np.zeros(n)
    amp = np.zeros(n)
    for st, d, midi in LEGATO:
        a, b = int(st * SR), int((st + d) * SR)
        f0[a:b] = 440.0 * 2 ** ((midi - 69) / 12)
        amp[a:b] = 1.0
    t = np.arange(n) / SR
    vib = 25 * np.sin(2 * np.pi * 5.5 * t)
    ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
    k = int(0.02 * SR)
    env = np.convolve(amp, np.ones(k) / k, mode="same")
    y = 0.25 * env * sum((0.6 ** h) * np.sin((h + 1) * ph) for h in range(8))
    return y + np.random.default_rng(seed).normal(scale=1e-4, size=n)


@pytest.fixture
def eng(tmp_path, monkeypatch):
    from vocal_engine import ara_relay as R
    from vocal_engine import mcp_server as m     # 先に読む（mcp_ara などは mcp_server の末尾から読まれる）
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))   # 人の作業場所に作らない
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    monkeypatch.setenv(R.SESSIONS_ENV, str(tmp_path / "sessions"))
    monkeypatch.delenv(R.ALLOW_ENV, raising=False)
    F.set_preferred_estimator("praat")           # 重みが無くても解析できる
    yield m, a, md, R
    R.stop()
    R.detach()
    F.set_preferred_estimator(None)
    F.set_default_estimator(None)
    md._clear()
    a._reset_render()


def _song(m, md, tmp_path, voice=None):
    """単体の曲にトラックを 1 本足して選ぶ。(素材, トラック id)。"""
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "media" / "take.wav", _legato() if voice is None else voice)
    _ok(md.new_project())
    tid = _ok(mt.add_track(src, select=True))["track"]
    return src, tid


def _render(p):
    from vocal_engine.render.region import RegionRenderer, render_region
    y, _info = render_region(p, renderer=RegionRenderer.for_project(p, channels="all"))
    return np.asarray(y, dtype="float64")


def _notes(m):
    return [n["id"] for n in _ok(m.list_notes(kind="note"))["notes"]]


def _spans(p):
    return {e.id: tuple(round(v, 6) for v in p.edit_span(e)) for e in p.edits}


# ================================================================ 1. force だけでは版を替えない
def test_force_without_estimator_keeps_the_gliss_v2_model(eng, tmp_path):
    m, a, md, R = eng
    from vocal_engine.analysis import f0 as F
    _song(m, md, tmp_path)
    p = m._state["project"]
    p.f0_model_version = F.GLISS_F0_V2_VERSION       # 第 2 版で解析した曲
    _ok(m.analyze_take(estimator="gliss", background=False))
    assert p.take_f0.meta["version"] == F.GLISS_F0_V2_VERSION
    n = _notes(m)
    _ok(m.shift_pitch(120, note_id=n[1]))
    before = _render(p)
    r = _ok(m.analyze_take(force=True, background=False))          # estimator なし
    assert m._state["project"].take_f0.meta["version"] == F.GLISS_F0_V2_VERSION
    assert m._state["project"].analysis["take"]["estimator_version"] == F.GLISS_F0_V2_VERSION
    assert "retargeted" not in r                                    # 方式も版も替わらない
    assert np.array_equal(_render(m._state["project"]), before)    # 保存済みの編集の音は同じ
    # force と estimator を両方明示したときだけ第 3 版へ（ノート対象の編集は区間へ付け替える）
    r = _ok(m.analyze_take(force=True, estimator="gliss", background=False))
    q = m._state["project"]
    assert q.take_f0.meta["version"] == F.estimator_version("gliss")
    assert r["retargeted"] == 1 and r["missing_note_targets"]["count"] == 0
    assert all(e.target.type == "range" for e in q.edits)


# ================================================================ 2. 方式を替える前にノート対象の編集を区間へ
def _note_target_edits(m):
    """人と AI のノート対象の編集（1 つは取り消してやり直しの列に残す）と範囲の編集。
    ノート対象 = n[0] の shift・n[2] の曲線（有効）、n[6] の shift（取り消し）。n[4] の stretch は範囲の編集になる。"""
    n = _notes(m)
    _ok(m.shift_pitch(150, note_id=n[0], author="human"))
    _ok(m.set_pitch_curve([[0.0, 0], [0.2, 80], [0.4, 0]], note_id=n[2], author="ai"))
    _ok(m.stretch(1.1, note_id=n[4], author="human"))
    _ok(m.shift_pitch(-60, start_sec=3.45, end_sec=3.8, author="ai"))
    _ok(m.shift_pitch(40, note_id=n[6], author="ai"))
    _ok(m.undo())                                                   # n[6] の編集はやり直しの列に
    return n


def test_retarget_keeps_the_sound_and_the_edit_identity(eng, tmp_path):
    """付け替えは今の解析のまま音を変えない。編集の id・author・changeset・並びも同じ。"""
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    _note_target_edits(m)
    p = m._state["project"]
    before = _render(p)
    spans = _spans(p)
    ident = [(e.id, e.kind, e.author, e.changeset, e.params) for e in p.edits]
    r = p.retarget_note_targets()
    assert r == {"retargeted": 2, "history": 1, "unresolved": [], "skipped": None}
    assert all(e.target.type == "range" for e in p.edits)
    assert [(e.id, e.kind, e.author, e.changeset, e.params) for e in p.edits] == ident
    assert _spans(p) == spans
    assert np.array_equal(_render(p), before)
    # 取り消した編集も区間になっている（やり直すと同じ区間）
    hist = [op["edit"] for cs in p.changesets for op in cs.ops if op.get("op") == "add"]
    assert all(d["target"]["type"] == "range" for d in hist)
    assert p.retarget_note_targets()["retargeted"] == 0             # 2 回目は何もしない


def test_switching_the_estimator_keeps_note_edits_on_their_spans(eng, tmp_path):
    """方式を替えても、ノート対象の編集は替える前のノートの区間に当たる（同じ番号の別のノートに当たらない）。"""
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))                            # praat
    _note_target_edits(m)
    p = m._state["project"]
    spans = _spans(p)
    r = _ok(m.analyze_take(estimator="gliss", background=False))
    q = m._state["project"]
    assert q.analysis["take"]["estimator"] == "gliss"
    assert _spans(q) == spans                                       # 区間は praat のノートのまま
    assert r["retargeted"] == 2 and r["retarget"]["history"] == 1
    assert r["missing_note_targets"] == {"count": 0, "ids": []}
    _ok(m.redo())
    q = m._state["project"]
    assert [e.target.type for e in q.edits].count("note") == 0


def test_switching_shifted_note_ids_does_not_hit_another_note(eng, tmp_path, monkeypatch):
    """替えた後の解析でノートの番号がずれても（頭に 1 つ増えた）、編集は元の区間に当たる。"""
    m, a, md, R = eng
    from dataclasses import replace
    from vocal_engine.project import store
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    n = _notes(m)
    p = m._state["project"]
    _ok(m.shift_pitch(300, note_id=n[1]))
    want = p.note(n[1])
    real = store.segment_notes

    def shifted(f0r, source="take", **kw):
        ns = real(f0r, source=source, **kw)
        if source != "take" or f0r.estimator != "gliss":
            return ns
        # gliss では頭に短い区間が 1 つ増え、番号が 1 つずつずれる
        head = replace(ns[0], id="n000", end_sec=ns[0].start_sec + 0.01)
        return [head] + [replace(x, id="n%03d" % (i + 2)) for i, x in enumerate(ns)]

    monkeypatch.setattr(store, "segment_notes", shifted)
    _ok(m.analyze_take(estimator="gliss", background=False))
    q = m._state["project"]
    assert q.note(n[1]).start_sec != pytest.approx(want.start_sec, abs=0.05)   # 同じ番号は別のノート
    (e,) = q.edits
    assert q.edit_span(e) == (want.start_sec, want.end_sec)


def test_set_f0_estimator_current_retargets_before_the_next_analysis(eng, tmp_path):
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    n = _notes(m)
    _ok(m.shift_pitch(100, note_id=n[3], author="human"))
    p = m._state["project"]
    spans = _spans(p)
    r = _ok(m.set_f0_estimator("gliss", scope="current"))
    _ok(m.analyze_take(background=False))
    q = m._state["project"]
    assert q.analysis["take"]["estimator"] == "gliss" and _spans(q) == spans
    assert q.edits[0].author == "human" and r["retargeted"] == 1


def test_retarget_skips_when_the_analysis_is_not_the_edits_basis(eng, tmp_path):
    """保存した解析が編集を作った方式のものでない（アーカイブを戻した直後など）: 区間が分からないので書き換えない。"""
    m, a, md, R = eng
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    n = _notes(m)
    _ok(m.shift_pitch(100, note_id=n[3]))
    p = m._state["project"]
    r = p.retarget_note_targets(basis="gliss")
    assert r["retargeted"] == 0 and r["skipped"] and p.edits[0].target.type == "note"


# ================================================================ 3. 明示した方式を方式探しで戻さない
def _daw_with_edit(m, a, tmp_path):
    from vocal_engine import mcp_tracks as mt
    src = _wav(tmp_path / "ara-src" / "mod-1.wav", _legato())
    _open(a)
    tid = _add(a, "mod-1", src, name="テイク")["track"]["id"]
    _ok(mt.select_track(tid))
    _ok(m.analyze_take(background=False))
    n = _notes(m)
    _ok(m.shift_pitch(100, note_id=n[1]))
    return src, tid


def _dangling(m, a, monkeypatch):
    """ノート対象の編集の対象を、どの解析にも無い番号にする（アーカイブを戻して作る）。praat なら全部当たることにする。"""
    import copy
    from vocal_engine.project import Project
    arc = copy.deepcopy(_ok(a.ara_archive(["mod-1"]))["archives"]["mod-1"]["archive"])
    arc["changesets"][0]["ops"][0]["edit"]["target"]["note_id"] = "n099"
    arc["f0_estimator"] = "gliss"
    _ok(a.ara_restore("mod-1", arc))
    monkeypatch.setattr(Project, "missing_note_targets_with",
                        lambda self, est: [] if est == "praat" else ["n099"])


def test_explicit_estimator_is_not_reverted_and_missing_is_reported(eng, tmp_path, monkeypatch):
    m, a, md, R = eng
    _daw_with_edit(m, a, tmp_path)
    _dangling(m, a, monkeypatch)
    r = _ok(m.analyze_take(estimator="gliss", background=False))     # 利用者が明示
    d = a.ara_render_dirty("mod-1")
    t = m._state["session"].find_ara("mod-1")
    assert t["estimator"] == "gliss"                                 # 戻さない
    assert m._state["project"].analysis["take"]["estimator"] == "gliss"
    assert d["ok"] is False and "n099" in d["error"] and "missing_note_targets" in d["error"]
    assert t["estimator_explicit"] is True
    assert r["missing_note_targets"]["ids"] == ["n099"]
    # 戻したアーカイブの方式（gliss）と保存した解析（praat）が違う: 区間が分からないので付け替えない
    assert r["retargeted"] == 0 and r["retarget"]["skipped"]


def test_archive_estimator_is_still_fitted(eng, tmp_path, monkeypatch):
    """アーカイブで決まった方式（利用者の明示ではない）は、今までどおり全部当たる方式に替える。"""
    m, a, md, R = eng
    _daw_with_edit(m, a, tmp_path)
    _dangling(m, a, monkeypatch)
    t = m._state["session"].find_ara("mod-1")
    assert t["estimator"] == "gliss" and not t.get("estimator_explicit")
    a.ara_render_dirty("mod-1")
    assert m._state["session"].find_ara("mod-1")["estimator"] == "praat"


# ================================================================ 4. 離れた 2 か所のタイミング
def _out_span(p, a, b):
    from vocal_engine.project import timing as TM
    tm = TM.current_map(p)
    return tm.at(a, "right"), tm.at(b, "left")


def test_two_far_apart_timing_moves_leave_the_notes_between_untouched(eng, tmp_path):
    """1 回で離れた 2 つのノートを動かしても、間のノートの（人の）編集は組み直さず、音もサンプル一致。"""
    m, a, md, R = eng
    from vocal_engine.project import timing as TM
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    ns = TM.pitched_notes(p)
    first, mid_a, mid_b, last = ns[0], ns[4], ns[5], ns[-1]
    # 間のノートの境目にまたがる人の伸縮（ノートの区切りが後から変わった編集。ツールからは作れないので直に入れる）
    from vocal_engine.project.model import Target
    t = mid_a.end_sec
    p.apply_edits([{"kind": "stretch", "target": Target.range(t - 0.05, t + 0.03), "params": {"ratio": 1.2}},
                   {"kind": "stretch", "target": Target.range(t + 0.03, t + 0.15),
                    "params": {"ratio": (0.20 - 0.08 * 1.2) / 0.12}}], author="human")   # 後ろはずらさない
    mids = [(e.id, e.author, e.target.start_sec, e.target.end_sec) for e in p.edits]
    o0, o1 = _out_span(p, mid_a.start_sec + 0.03, mid_b.end_sec - 0.03)
    before = _render(p)
    plan = TM.plan_move(p, [first.id, last.id])
    x = min(0.03, plan.x_hi)
    assert x > 0.005
    cs, info = TM.apply_plan(p, plan, x)
    assert cs is not None
    assert _out_span(p, mid_a.start_sec + 0.03, mid_b.end_sec - 0.03) == (o0, o1)
    after = _render(p)
    i0, i1 = int(round(o0 * SR)), int(round(o1 * SR))
    assert np.array_equal(after[i0:i1], before[i0:i1])
    assert not np.array_equal(after, before)                         # 動かした所は変わった
    # 間の編集はそのまま（id・author・範囲）。組み直した窓は 2 つ
    ids = {i for i, *_ in mids}
    assert [(e.id, e.author, e.target.start_sec, e.target.end_sec) for e in p.edits if e.id in ids] == mids
    w = info["windows_sec"]
    assert len(w) == 2 and w[0][1] < mid_a.start_sec and w[1][0] > mid_b.end_sec


# ================================================================ 5. 曲線を重ねても pitch_shift が漏れない
def test_zero_pitch_curve_does_not_leak_the_neighbour_shift(eng, tmp_path):
    m, a, md, R = eng
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.project import timing as TM
    from vocal_engine.project.pitch import edits_base_index, layered_segments, seg_value
    _song(m, md, tmp_path)
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    ns = TM.pitched_notes(p)
    a_, b_ = ns[1], ns[2]                                            # つながった 2 音
    _ok(m.shift_pitch(282, note_id=a_.id))
    _ok(m.set_pitch_curve([[0.0, 0.0], [b_.end_sec - a_.start_sec + 0.1, 0.0]], mode="offset",
                          start_sec=a_.start_sec - 0.05, end_sec=b_.end_sec + 0.05))
    segs = layered_segments(p)
    for sg in segs:
        if sg.curve_points:
            ts = [q[0] for q in sg.curve_points]
            assert all(t1 >= t0 - 1e-6 for t0, t1 in zip(ts, ts[1:])), (sg.start_sec, sg.end_sec, ts)
    # 後ろのノートの真ん中（つなぎの窓の外）のずらし量は 0（見積もりと同じ）
    mid = 0.5 * (b_.start_sec + b_.end_sec) + 0.08
    sg = next(s for s in segs if s.start_sec <= mid < s.end_sec)
    assert abs(seg_value(sg, mid)) < 1.0, seg_value(sg, mid)
    assert abs(edits_base_index(p).at(mid)) < 1e-9
    # 書き出しの音程も後ろのノートは元のまま（漏れていれば +282 セント）
    y = _render(p)
    y = y.mean(axis=1) if y.ndim == 2 else y
    x, _sr = p.audio("take")
    f_out = estimate_f0(x=y, sr=SR, estimator="praat")
    f_in = estimate_f0(x=x, sr=SR, estimator="praat")
    tm = TM.current_map(p)
    lo, hi = mid - 0.05, mid + 0.05

    def med(f, a0, a1):
        i0, i1 = int(a0 / f.hop_s), int(a1 / f.hop_s)
        v = f.f0[i0:i1][f.voiced[i0:i1] & (f.f0[i0:i1] > 0)]
        return float(np.median(v))

    cents = 1200 * np.log2(med(f_out, tm.at(lo), tm.at(hi)) / med(f_in, lo, hi))
    assert abs(cents) < 30, cents


# ================================================================ 6. 中継のジョブでも方式を覚える
def test_relay_job_remembers_the_estimator_of_the_selected_track(eng, tmp_path, monkeypatch):
    m, a, md, R = eng
    from vocal_engine import mcp_server as srv
    from vocal_engine import mcp_tracks as mt
    monkeypatch.setenv("GLISS_CLIENT", "ara")
    monkeypatch.setattr(srv, "JOB_THRESHOLD_SEC", 0.0)            # 短い素材でもジョブにする
    src = _wav(tmp_path / "src" / "a.wav", _legato())
    _open(a)
    ta = _add(a, "mod-A", src, offset_sec=0.0, group="Vox 1", name="テイク A")["track"]["id"]
    tb = _add(a, "mod-B", src, offset_sec=8.0, group="Vox 2", name="テイク B")["track"]["id"]
    assert mt.current_track_id() == ta                               # プラグインの画面は mod-A
    R.attach(ara_id="mod-B")
    r = R.forward("analyze_take", {"force": True, "estimator": "gliss"})
    assert r["ok"] and r.get("job_id"), r
    for _ in range(600):
        j = R.forward("get_job", {"job_id": r["job_id"]})
        if j.get("status") != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done", j
    assert j["f0"]["estimator"] == "gliss"
    s = m._state["session"]
    s.reload_if_changed()
    assert s.track(tb).get("estimator") == "gliss" and s.track(tb).get("estimator_explicit")
    assert s.track(ta).get("estimator") is None                      # 画面のトラックには書かない
    assert mt.current_track_id() == ta
    r = R.forward("analyze_take", {"background": False})             # estimator を省いても gliss のまま
    assert r["ok"] and r["f0"]["estimator"] == "gliss"
    arc = _ok(a.ara_archive(["mod-B"]))["archives"]["mod-B"]["archive"]
    assert arc["f0_estimator"] == "gliss"
    cache = Cache(src)
    cache.sync(a, "mod-B")
