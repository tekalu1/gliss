# -*- coding: utf-8 -*-
"""「ガイドに合わせる」のタイミングが**タイムライン上のガイドの発音の頭**へ近づくこと（issue #61）。

ユーザーの状況に近い合成データ: テイク C のノートを前後に動かして書き出したものをガイドにする（答えの位置が
分かる。`test_guide_timing.py` と同じ作り方。ただし頭に無音は足さない = 同じ時間軸）。そのうえで

- トラックの位置をずらす（テイクを後ろへ 40 ms = テイクが全体に遅れる／ガイドを後ろへ 50 ms = テイクが全体に早い）
- 補正の前に既存の編集（ノートの移動・端の伸縮・ピッチ）を入れておく
- 伴奏と別のボーカルのトラックも足しておく（複数トラック）

とし、強さ 30 / 60 / 100% で、テイクの発音の頭（エンジンと別方式の検出）とガイドの頭の差を**タイムライン上で**
測る。直す前は「ガイドの位置 + 全体のずれ（= ずらした量）」へ合わせていたので、100% でもずらした量のずれが
残り、ガイドに合っていた頭まで動かされて、強くするほどガイドから離れた。
"""
import numpy as np
import pytest
import soundfile as sf

from conftest import CLIP_A, CLIP_E, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model, pytest.mark.usefixtures("rmvpe_f0")]   # ノートの ID・区切りは RMVPE の解析のもの

MOVES = {"n003": 0.06, "n006": -0.05, "n011": 0.045, "n014": -0.04}


@pytest.fixture(scope="module")
def warped(tmp_path_factory):
    """(ガイドの WAV, W の (src, out))。ガイドの秒 = W(テイクの秒)（同じ時間軸。頭に無音を足さない）。"""
    from vocal_engine.project import Project
    from vocal_engine.project import timing as TM
    from vocal_engine.render.export import export_wav
    base = tmp_path_factory.mktemp("warp61")
    p = Project.open(TAKE, project_dir=str(base / "warp"))
    p.analyze()
    for nid, x in MOVES.items():
        plan = TM.plan_move(p, [nid])
        TM.apply_plan(p, plan, x)
    src, out = p.time_map()
    w = str(base / "guide61.wav")
    export_wav(p, path=w)
    return w, (np.asarray(src), np.asarray(out))


@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    yield m, mt
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _eval_points(p):
    from onsets_ref import detect_onsets_energy
    x, sr = p.audio("take")
    t = detect_onsets_energy(x, sr)
    return t[(t > 0.3) & (t < 3.4)]


def _session(m, mt, tmp_path, guide, take_off, guide_off):
    """テイク・ガイド・伴奏・別のボーカルの 4 トラック。既存の編集を入れてから位置をずらす。"""
    from vocal_engine.project import timing as TM
    r = _ok(m.open_project(TAKE, guide, project_dir=str(tmp_path / "s61")))
    t_take = r["session"]["current"]
    t_guide = r["session"]["guide"]
    _ok(mt.add_track(CLIP_A, kind="inst", name="オケ"))
    _ok(mt.add_track(CLIP_E, kind="vocal", name="別のテイク"))
    _ok(mt.select_track(t_take))
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    # 既存の編集: ガイドと関係の無いノートの移動・端の伸縮・ピッチ（補正はこの上に重ねる）
    TM.apply_plan(p, TM.plan_move(p, ["n008"]), 0.03)
    TM.apply_plan(p, TM.plan_edge(p, "n012", "end"), -0.02)
    _ok(m.shift_pitch(cents=20, note_id="n005"))
    if take_off:
        _ok(mt.set_track(t_take, offset_sec=take_off))
    if guide_off:
        _ok(mt.set_track(t_guide, offset_sec=guide_off))
    _ok(m.analyze_take(background=False))
    p = m._state["project"]
    assert len(p.edits) >= 3
    return p


@pytest.mark.parametrize("take_off, guide_off", [(0.04, 0.0), (0.0, 0.05)],
                         ids=["take-late-40ms", "guide-later-50ms"])
def test_timing_moves_toward_guide_on_timeline(warped, tmp_path, mcp, take_off, guide_off):
    from vocal_engine.project import timing as TM
    m, mt = mcp
    guide, (src, out) = warped
    p = _session(m, mt, tmp_path, guide, take_off, guide_off)
    gt = p.guide_timing()
    # 全体のずれ（ずらした量）は測るが、基準はタイムライン上のガイドの位置そのもの
    assert gt.basis == "timeline" and gt.offset_sec == 0.0, gt.summary()
    assert abs(gt.measured_offset_sec - (take_off - guide_off)) < 0.015, gt.summary()
    ev = _eval_points(p)

    def err(t):
        """タイムライン上: テイクの頭 − ガイドの頭（ms）。ガイドの頭はガイドのファイルの W(t) + ガイドの位置。"""
        tm = TM.current_map(p)
        return np.array([(tm.at(v) + take_off - (float(np.interp(v, src, out)) + guide_off)) * 1000.0
                         for v in t])

    e0 = err(ev)
    base = {c.id for c in p.changesets if not c.undone}
    plan = TM.plan_guide(p)
    assert plan.info["basis"] == "timeline" and plan.info["offset_ms"] == 0.0
    med, signed = [float(np.median(np.abs(e0)))], [float(np.median(e0))]
    for x in (0.3, 0.6, 1.0):
        TM.apply_plan(p, plan, x)
        e = err(ev)
        med.append(float(np.median(np.abs(e))))
        signed.append(float(np.median(e)))
        # 強さに比例して近づく: 目標の付いた頭は、残りのずれがちょうど (1 − x) 倍
        tm = TM.current_map(p)
        for r in plan.timing:
            if r.get("reached"):
                left = tm.at(r["take_sec"]) - r["target_sec"]
                assert abs(left - (1.0 - x) * (r["cur_sec"] - r["target_sec"])) < 2e-4, (x, r)
        if x == 1.0:
            worse = int(np.sum(np.abs(e) > np.abs(e0) + 10.0))
            assert worse <= 1, list(zip(ev.round(3), e0.round(1), e.round(1)))
        for c in list(p.changesets)[::-1]:
            if c.id not in base and not c.undone:
                p.undo(c.id)
    # 補正前はずらした量だけずれている（テイクが遅い = +、早い = −）
    assert abs(signed[0] - (take_off - guide_off) * 1000.0) < 25.0, signed
    assert med[0] > 30.0, med
    # 強くするほどガイドへ近づき、100% でガイドの頭に揃う（直す前は 100% でもずらした量が残った）
    assert med[0] > med[1] > med[2] > med[3], med
    assert med[3] <= 10.0, med
    assert abs(signed[3]) <= 10.0, signed


def test_view_draws_guide_at_its_timeline_position(warped, tmp_path, mcp):
    """画面のガイドの位置 = タイムライン上のガイドの位置（補正の目標と同じ。全体のずれを足さない）。"""
    import json
    from vocal_engine.view.export_data import export_view_data
    m, mt = mcp
    guide, _ = warped
    p = _session(m, mt, tmp_path, guide, 0.04, 0.0)
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        vd = json.load(f)
    assert vd["guide_basis"]["basis"] == "guide_time" and vd["guide_basis"]["offset_ms"] == 0.0
    gn = [g for g in vd["guide_notes"] if g["kind"] == "note"]
    assert gn
    # テイクを 40 ms 後ろに置いた: ガイドはテイクの頭のタイムライン上の位置（ガイドのファイルの 0.04 秒）から切り出され、
    # 描く位置 = ガイドのクリップの秒そのもの（全体のずれ +40 ms を足さない）
    for g in gn:
        assert abs(g["start_sec"] - g["guide_start_sec"]) < 1e-3
    assert p.guide["offset_frames"] == int(round(0.04 * sf.info(guide).samplerate))
