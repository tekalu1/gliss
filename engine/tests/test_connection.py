# -*- coding: utf-8 -*-
"""接続 / 切り離し（後ろをずらさないタイミング編集）と「ガイドに合わせる」の計画。

- リップルが起きない: 編集したノートと隣以外の区間は、書き出しで**元とサンプル一致**
- 接続 / 切り離し / Alt（detach）/ 吸着 / undo で接続も戻る
- 計画の x を確定した結果 = 計画の式（プレビューが描く位置）
- 100% でガイドの中心・境界に一致、対応付けに交差が無い
"""
import os
import json
import shutil

import numpy as np
import pytest
import soundfile as sf

import materials as M
from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]

LYRICS = M.text("C.lyrics")
XF = 0.012          # クロスフェードの片側（10 ms）＋丸め


def _open(tmp_path, name, lyrics=None):
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / name),
                     lyrics=lyrics, guide_lyrics=lyrics)
    p.analyze()
    return p


@pytest.fixture
def plain(tmp_path):
    p = _open(tmp_path, "plain")
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


@pytest.fixture(scope="module")
def lyr_dir(tmp_path_factory):
    """歌詞つきの解析（HubertFA）は 1 回だけ。テストごとに project.json を作り直す。"""
    base = tmp_path_factory.mktemp("lyr")
    p = _open(base, "seed", LYRICS)
    return p.dir


@pytest.fixture
def lyr(lyr_dir, tmp_path):
    from vocal_engine.project import Project
    d = tmp_path / "lyr"
    shutil.copytree(os.path.join(lyr_dir, "cache"), str(d / "cache"))
    p = Project.open(TAKE, GUIDE, project_dir=str(d), lyrics=LYRICS, guide_lyrics=LYRICS)
    p.analyze()
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def _edges(p):
    from vocal_engine.project import timing as TM
    tm = TM.current_map(p)
    return {n.id: (tm.at(n.start_sec, "right"), tm.at(n.end_sec, "left"))
            for n in TM.pitched_notes(p)}


def _export_diff(p, tmp_path):
    """書き出しと元で**違うサンプル**がある区間 [最初, 最後]（秒）。一致なら None。"""
    from vocal_engine.render.export import export_wav
    out = str(tmp_path / "out.wav")
    r = export_wav(p, path=out)
    a, sr = sf.read(TAKE, dtype="int32", always_2d=True)
    b, _ = sf.read(out, dtype="int32", always_2d=True)
    assert a.shape == b.shape, "長さ・チャンネル数が元と同じ"
    d = np.flatnonzero(np.any(a != b, axis=1))
    return (None if not len(d) else (d[0] / sr, d[-1] / sr)), r


def _span(p, *ids):
    ns = {n.id: n for n in p.take_notes}
    return min(ns[i].start_sec for i in ids), max(ns[i].end_sec for i in ids)


def _check_plan_result(p, plan, x):
    """確定した後のノートの頭・尻 = 計画の式（cur + d*x）。"""
    pos = plan.new_positions(x)
    e = _edges(p)
    for nid, (ks, ke) in plan.st.note_knots.items():
        assert abs(e[nid][0] - pos[ks]) < 1e-6, nid
        assert abs(e[nid][1] - pos[ke]) < 1e-6, nid


# ---------------------------------------------------------------- 既定の接続
def test_default_connections(plain):
    from vocal_engine.project import timing as TM
    c = {(a.id, b.id): v for a, b, v, _ in TM.connections(plain)}
    assert c[("n005", "n006")] is True            # ノート分割で隣接 = 接続
    assert c[("n007", "n009")] is False           # 息（breath）を挟む = 切り離し


# ---------------------------------------------------------------- リップルなし（書き出しのサンプル一致）
@pytest.mark.parametrize("mode", ["plain", "lyr"])
@pytest.mark.parametrize("case", ["connected", "alt", "gap_grow", "gap_shrink", "move"])
def test_no_ripple_export_matches_outside(mode, case, request, tmp_path):
    from vocal_engine.project import timing as TM
    p = request.getfixturevalue(mode)
    before = _edges(p)
    if case == "connected":
        plan, x, touched = TM.plan_edge(p, "n005", "end"), 0.03, ("n005", "n006")
    elif case == "alt":
        plan, x, touched = TM.plan_edge(p, "n005", "end", detach=True), -0.04, ("n005",)
    elif case == "gap_grow":
        plan, x, touched = TM.plan_edge(p, "n007", "end"), 0.10, ("n007",)
    elif case == "gap_shrink":
        plan, x, touched = TM.plan_edge(p, "n009", "start"), 0.04, ("n009",)
    else:
        plan, x, touched = TM.plan_move(p, ["n012"]), 0.025, ("n011", "n012", "n013")
    assert plan.x_lo <= x <= plan.x_hi
    cs, info = TM.apply_plan(p, plan, x)
    assert cs is not None
    _check_plan_result(p, plan, x)
    after = _edges(p)
    for nid in before:
        if nid in touched:
            continue
        assert abs(after[nid][0] - before[nid][0]) < 1e-9, "%s の頭が動いた" % nid
        assert abs(after[nid][1] - before[nid][1]) < 1e-9, "%s の尻が動いた" % nid
    src, out = p.time_map()
    assert abs(out[-1] - src[-1]) < 1e-9, "素材の末尾がずれた（リップル）"
    diff, r = _export_diff(p, tmp_path)
    a, b = _span(p, *touched)
    # ノートの頭の子音（アタック）はノートと一緒に動くので、その範囲も「編集したノート」
    mv = [k.src for k, d in zip(plan.st.knots, plan.d) if abs(d * x) > 1e-9]
    a, b = min(a, min(mv)), max(b, max(mv))
    if case == "gap_grow":
        b += x                                   # 隙間へ伸ばしたぶん（切り取り）
    assert diff is not None
    assert diff[0] >= a - XF and diff[1] <= b + XF, \
        "編集したノートと隣の外でサンプルが違う: %s（許容 %.3f–%.3f）" % (diff, a - XF, b + XF)


def test_move_boundary_is_local_in_export(lyr, tmp_path):
    """音素境界のドラッグ（move_boundary）も、2 つの音素の外はサンプル一致。"""
    from vocal_engine.phoneme.edit import move_boundary_spec
    from vocal_engine.project.model import Target
    res = lyr.phonemes("take")
    by = {q.index: q for q in res.phonemes}
    b = next(x for x in res.boundaries
             if x.before_index in by and x.after_index in by
             and by[x.before_index].duration_sec > 0.08 and by[x.after_index].duration_sec > 0.08)
    spec, _ = move_boundary_spec(lyr, b.id, 15.0)
    spec["target"] = Target.boundary(b.id, spec["params"]["left_sec"], spec["params"]["right_sec"])
    lyr.apply_edits([spec], author="human")
    diff, _ = _export_diff(lyr, tmp_path)
    lo, hi = by[b.before_index].start_sec, by[b.after_index].end_sec
    assert diff[0] >= lo - XF and diff[1] <= hi + XF


# ---------------------------------------------------------------- 接続・切り離し・吸着
def test_connected_edge_moves_the_shared_boundary(plain):
    from vocal_engine.project import timing as TM
    b0 = _edges(plain)
    plan = TM.plan_edge(plain, "n005", "end")
    TM.apply_plan(plain, plan, -0.03)
    e = _edges(plain)
    assert abs(e["n005"][1] - (b0["n005"][1] - 0.03)) < 1e-9
    assert abs(e["n006"][0] - e["n005"][1]) < 1e-9           # 隣がそのまま長くなる
    assert abs(e["n006"][1] - b0["n006"][1]) < 1e-9


def test_alt_detach_then_snap_and_undo(plain):
    from vocal_engine.project import timing as TM
    b0 = _edges(plain)
    plan = TM.plan_edge(plain, "n005", "end", detach=True)
    assert plan.x_hi == 0.0, "隙間の無い接続を切っても、隣へは伸ばせない"
    cs1, _ = TM.apply_plan(plain, plan, -0.04)
    assert TM.connection_map(plain)[("n005", "n006")] is False
    kinds = [e.kind for e in plain.edits]
    assert "silence" in kinds and "connection" in kinds       # 縮めてできた隙間は無音
    e = _edges(plain)
    assert abs(e["n006"][0] - b0["n006"][0]) < 1e-9
    # 切り離された端を伸ばすと、隣にぶつかる位置（snap）で止まり、そこで離すと接続
    plan2 = TM.plan_edge(plain, "n005", "end")
    assert plan2.snap_x == pytest.approx(0.04, abs=1e-9)
    assert plan2.x_hi == pytest.approx(0.04, abs=1e-9)
    cs2, info = TM.apply_plan(plain, plan2, plan2.snap_x)
    assert info.get("snapped")
    assert TM.connection_map(plain)[("n005", "n006")] is True
    e = _edges(plain)
    assert abs(e["n005"][1] - b0["n005"][1]) < 1e-9
    assert not [x for x in plain.edits if x.kind in ("stretch", "silence", "crop")], \
        "元の位置に戻れば原音（編集なし）"
    plain.undo(cs2.id)
    assert TM.connection_map(plain)[("n005", "n006")] is False
    plain.undo(cs1.id)
    assert TM.connection_map(plain)[("n005", "n006")] is True
    assert not plain.edits


def test_cannot_overtake_or_collapse(plain):
    from vocal_engine.project import timing as TM
    plan = TM.plan_edge(plain, "n005", "end")
    n5 = plain.note("n005")
    n6 = plain.note("n006")
    # 右へは次のノートが 20 ms（か比の下限）になるまで、左へは自分が 20 ms まで
    assert plan.x_hi <= (n6.end_sec - n6.start_sec) - 0.02 + 1e-9
    assert plan.x_lo >= -((n5.end_sec - n5.start_sec) - 0.02) - 1e-9
    plan_m = TM.plan_move(plain, ["n009"])            # 左は息の隙間、右は接続
    assert plan_m.x_lo < -0.3                         # 隙間ぶんは左へ動ける
    assert plan_m.x_hi < 0.2


def test_set_connection_tool_and_list(plain):
    from vocal_engine import mcp_server as M
    M._state["project"] = plain
    r = M.set_connection("n005", "n006", False)
    assert r["ok"] and r["connected"] is False
    rows = {(x["a"], x["b"]): x for x in M.list_connections()["connections"]}
    assert rows[("n005", "n006")]["connected"] is False
    assert rows[("n005", "n006")]["default"] is True
    bad = M.set_connection("n005", "n007", True)
    assert bad["ok"] is False


# ---------------------------------------------------------------- MCP の編集ツールも同じ意味
def test_mcp_stretch_and_move_do_not_ripple(plain, tmp_path):
    from vocal_engine import mcp_server as M
    M._state["project"] = plain
    b0 = _edges(plain)
    r = M.stretch(1.2, note_id="n007")                # 尻の後ろは息（切り離し）
    assert r["ok"] and r["changeset"]
    r = M.move_note(-15.0, note_id="n013")            # 両隣は接続
    assert r["ok"] and r["changeset"]
    e = _edges(plain)
    for nid in b0:
        if nid in ("n007", "n012", "n013", "n014"):
            continue
        assert abs(e[nid][0] - b0[nid][0]) < 1e-9 and abs(e[nid][1] - b0[nid][1]) < 1e-9, nid
    n7 = plain.note("n007")
    assert abs((e["n007"][1] - e["n007"][0]) - 1.2 * (n7.end_sec - n7.start_sec)) < 1e-6
    src, out = plain.time_map()
    assert abs(out[-1] - src[-1]) < 1e-9


def test_reset_to_original_is_bit_exact(plain, tmp_path):
    from vocal_engine import mcp_server as M
    M._state["project"] = plain
    M.shift_pitch(-120.0, note_id="n005")
    M.stretch(0.8, note_id="n005")
    M.move_note(20.0, note_id="n010")
    r = M.reset_to_original(note_ids=["n005", "n010"])
    assert r["ok"] and r["changeset"]
    diff, _ = _export_diff(plain, tmp_path)
    assert diff is None, "戻したノートは原音のサンプルそのもの: %s" % (diff,)


# ---------------------------------------------------------------- ガイドに合わせる
def test_correspondence_has_no_crossing(plain):
    from vocal_engine.project import timing as TM
    pairs, pitch_target, _ = TM.note_correspondence(plain)
    order = {n.id: i for i, n in enumerate(TM.pitched_notes(plain))}
    gorder = {g.id: i for i, g in enumerate(plain.guide_notes)}
    seen = set()
    last_t = last_g = -1
    for pr in pairs:
        ti = [order[i] for i in pr["take"]]
        gi = [gorder[i] for i in pr["guide"]]
        assert ti == list(range(ti[0], ti[-1] + 1)), "組のテイクノートは連続"
        assert ti[0] > last_t and gi[0] > last_g, "組どうしが交差しない"
        last_t, last_g = ti[-1], gi[-1]
        assert not (set(pr["take"]) & seen), "1 つのテイクノートは 1 つの組だけ"
        seen |= set(pr["take"])
    # C では n005 と n006 が同じガイドノート（g005）に重なる → 同じ組
    same = [pr for pr in pairs if "n005" in pr["take"]]
    assert same and "n006" in same[0]["take"]


@pytest.mark.parametrize("mode", ["plain", "lyr"])
def test_guide_plan_100_hits_guide(mode, request):
    """100% で音程はガイドの中心、タイミングは発音の頭が「ガイドの頭 + 全体のずれ」へ（issue #12）。"""
    from vocal_engine.project import timing as TM
    from vocal_engine.view.export_data import current_note_pitches
    p = request.getfixturevalue(mode)
    plan = TM.plan_guide(p, match_pitch_shape=False)
    assert plan.info["reach"] == 1.0
    _, pitch_target, _ = TM.note_correspondence(p)
    cs, _ = TM.apply_plan(p, plan, 1.0, pitch=1.0)
    _check_plan_result(p, plan, 1.0)
    cur = current_note_pitches(p)
    many = {r["note"]: r["pitch_midi"] for r in plan.info["one_to_many"]}
    for nid, g in pitch_target.items():
        # 1 対多（高さの違うガイドのノートが 2 つ以上重なる）は、フレームごとの差の中央値へ（ガイドの最も重なる音ではない）
        want = many.get(nid, g.pitch_midi)
        assert abs(cur[nid] - want) < 0.005, "%s の中心がガイドに合わない" % nid
    tm = TM.current_map(p)
    reached = [e for e in plan.timing if e["reached"]]
    assert len(reached) >= 3 and len(reached) >= 0.7 * len(plan.timing)
    for e in reached:
        assert abs(tm.at(e["take_sec"]) - e["target_sec"]) < 2e-4, e
    src, out = p.time_map()
    assert abs(out[-1] - src[-1]) < 1e-9


def test_guide_plan_is_linear_in_strength(plain):
    """強度 s の確定 = 100% の計画を線形に補間した位置（画面のプレビューと同じ式）。"""
    from vocal_engine.project import timing as TM
    plan = TM.plan_guide(plain)
    for s in (0.37, 0.8):
        cs, _ = TM.apply_plan(plain, plan, s, pitch=s)
        _check_plan_result(plain, plan, s)
        plain.undo(cs.id)


def test_guide_pitch_shape_maps_frames_and_undoes(plain):
    """既定 ON は Hz 基準を計画に保存し、50% の線・色・undo が一致する。"""
    from vocal_engine.project import timing as TM
    from vocal_engine.view.export_data import export_view_data

    def view():
        with open(export_view_data(plain)["path"], encoding="utf-8") as f:
            return json.load(f)

    before = view()
    plan = TM.plan_guide(plain)
    assert plan.params["match_pitch_shape"] is True
    assert plan.pitch_curve and plan.pitch_draws
    assert all(e["kind"] == "pitch_draw" for e in plan.pitch_draws)
    hz_50 = 440 * 2 ** ((TM._guide_blend_midi(100, 200, 1, 0.5) - 69) / 12)
    assert hz_50 == pytest.approx(150.0, abs=1e-9)
    assert abs(hz_50 - np.sqrt(100 * 200)) > 8.0  # MIDI 線形補間なら約 141.42 Hz
    cs, _ = TM.apply_plan(plain, plan, 0.0, pitch=0.5)
    assert cs is not None
    after = view()
    t0, hop = before["f0"]["t0_sec"], before["f0"]["hop_sec"]
    curve = {round(t, 6): (h0, h1, w) for t, h0, h1, w in plan.pitch_curve}
    checked = 0
    for i, (old, new) in enumerate(zip(before["f0"]["take_edited_midi"],
                                       after["f0"]["take_edited_midi"])):
        if old is None or new is None:
            continue
        row = curve.get(round(t0 + i * hop, 6))
        if row is not None:
            h0, h1, w = row
            delta = TM._guide_blend_midi(h0, h1, w, 0.5) - TM._guide_blend_midi(h0, h1, w, 0)
        if row is not None and abs(delta) > 0.02:
            assert abs(new - old - delta) < 0.003, i
            checked += 1
        elif row is None:
            assert abs(new - old) < 0.003, i  # ガイド無声・未対応・選択外
    assert checked > 10
    assert any(n["pitch_corr"] and not n["pitch_corr"]["manual"]
               for n in after["notes"] if n["kind"] == "note")
    plain.undo(cs.id)
    assert not plain.edits


def test_guide_pitch_shape_mcp_switch_keeps_average_mode(plain):
    from vocal_engine import mcp_server as M

    M._state["project"] = plain
    old = M.correct_to_guide(pitch_strength=0.5, timing_strength=0,
                             match_pitch_shape=False)
    assert old["changeset"] and any(e.kind == "pitch_shift" for e in plain.edits)
    assert not any(e.kind == "pitch_draw" for e in plain.edits)
    plain.undo(old["changeset"])
    new = M.correct_to_guide(pitch_strength=0.5, timing_strength=0,
                             match_pitch_shape=True)
    assert new["changeset"] and any(e.kind == "pitch_draw" for e in plain.edits)


def test_guide_pitch_shape_skips_unvoiced_guide_frames(plain):
    """ガイド F0 が無声なら補間で埋めず、そのテイク区間を無変更にする。"""
    from vocal_engine.project import timing as TM
    from vocal_engine.view.export_data import export_view_data

    full = TM.plan_guide(plain)
    assert full.pitch_curve
    no_map = TM._guide_pitch_shape(plain, TM.note_correspondence(plain)[0], [],
                                   {n.id for n in plain.take_notes if n.kind == "note"}, 0)
    assert no_map == ([], [], {}), "信頼できる発音の頭が無い場合は写さない"
    # 対応するガイド音程ノートを無声にして、その近傍の写像点が消えることを確かめる。
    g = next(n for n in plain.guide_notes if n.id == full.pairs[0]["guide"][0])
    gf = plain.guide_f0
    a = max(0, int(g.start_sec / gf.hop_s))
    b = min(len(gf.voiced), int(np.ceil(g.end_sec / gf.hop_s)))
    gf.voiced[a:b] = False
    cut = TM.plan_guide(plain)
    before = {round(row[0], 6) for row in full.pitch_curve}
    after = {round(row[0], 6) for row in cut.pitch_curve}
    removed = before - after
    assert removed, "無声化したガイドの対応フレームが計画から外れる"
    with open(export_view_data(plain)["path"], encoding="utf-8") as f:
        source = json.load(f)["f0"]["take_edited_midi"]
    cs, _ = TM.apply_plan(plain, cut, 0.0, pitch=1.0)
    with open(export_view_data(plain)["path"], encoding="utf-8") as f:
        result = json.load(f)["f0"]["take_edited_midi"]
    for t in removed:
        i = int(round(t / plain.take_f0.hop_s))
        if source[i] is not None and result[i] is not None:
            assert abs(result[i] - source[i]) < 0.003


def test_apply_plan_replaces_uses_the_same_plan(plain):
    """ポップアップ内の当て直し: 前回の changeset を取り消してから、同じ計画を当てる。"""
    from vocal_engine import mcp_server as M
    from vocal_engine.project import timing as TM
    M._state["project"] = plain
    r = M.plan_edit("guide")
    pid = r["plan_id"]
    plan = M._plans[pid]
    a = M.apply_plan(pid, x=0.5, pitch=0.5)
    b = M.apply_plan(pid, x=0.8, pitch=0.8, replaces=a["changeset"])
    assert not b["replanned"], "取り消した後の状態 = 計画を作ったときの状態"
    _check_plan_result(plain, plan, 0.8)
    c = M.apply_plan(pid, x=0.0, pitch=0.0, replaces=b["changeset"])
    assert c["changeset"] is None
    assert not plain.edits
    assert TM.state_sig(plain) == plan.sig


def test_thresholds_live_in_the_plan(plain):
    """しきい値は計画で掛ける（プレビューと確定で同じに効く）。"""
    from vocal_engine.project import timing as TM
    full = TM.plan_guide(plain, match_pitch_shape=False)
    big = max(abs(v) for v in full.pitch.values())
    cut = TM.plan_guide(plain, threshold_cents=big - 1.0, match_pitch_shape=False)
    assert len(cut.pitch) == 1


# ---------------------------------------------------------------- pipeline: 無音と切り取り
def test_silence_inside_a_range_edit_is_rendered_in_place(plain):
    """範囲のピッチ編集の途中に無音があっても、その時刻に出る（区間を無音の時刻で切る）。"""
    from vocal_engine.project.model import Edit, Target
    from vocal_engine.render.pipeline import Renderer, edits_to_segments
    from vocal_engine.view.export_data import build_time_map
    es = [Edit(id="e1", kind="pitch_shift", target=Target.range(0.5, 2.5), params={"cents": 50}),
          Edit(id="e2", kind="silence", target=Target.range(1.6, 1.6), params={"sec": 0.05}),
          Edit(id="e3", kind="crop", target=Target.range(1.7, 1.75), params={})]
    segs = edits_to_segments(es, lambda e: (e.target.start_sec, e.target.end_sec))
    order = [(round(s.start_sec, 3), round(s.end_sec, 3), s.silence_sec > 0) for s in segs]
    assert (1.6, 1.6, True) in order
    i = order.index((1.6, 1.6, True))
    assert order[i - 1][1] == 1.6 and order[i + 1][0] == 1.6, order
    src, out = build_time_map(segs, 0.0, 3.0)
    assert abs(out[-1] - 3.0) < 1e-9, "無音 50 ms と切り取り 50 ms で差し引き 0"
    x, sr = plain.audio("take")
    f0r = plain.take_f0
    r = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    y, info = r.render_range(0.0, 3.0, segs)
    assert len(y) == int(round(3.0 * sr))
    # 無音の真ん中はほぼ 0（前後は 10 ms のフェード）
    k = int(round((1.6 + 0.025) * sr))
    assert np.max(np.abs(y[k - int(0.004 * sr):k + int(0.004 * sr)])) < 1e-9
    # 範囲の外（2.6 s 以降）は元のサンプルそのまま
    a = int(round(2.6 * sr))
    assert np.array_equal(y[a:], np.asarray(x, dtype="float64")[a:int(round(3.0 * sr))])


# ---------------------------------------------------------------- レビューで見つかったもの
def test_detach_then_snap_in_one_plan_keeps_the_last_state(plain):
    """同じ確定で「切り離し」と「接続」の両方が出たら、最後の値（接続）が残る。"""
    from vocal_engine.project import timing as TM
    rm, add = TM.connection_specs(plain, [("n005", "n006", False), ("n005", "n006", True)])
    assert not add, "既定（接続）に戻すなら上書きは入れない"
    rm, add = TM.connection_specs(plain, [("n005", "n006", True), ("n005", "n006", False)])
    assert len(add) == 1 and add[0]["params"]["connected"] is False


def test_replaced_changeset_is_not_redone(plain):
    """ポップアップの当て直しで捨てた changeset は、undo の後の redo で戻らない。"""
    from vocal_engine import mcp_server as M
    M._state["project"] = plain
    pid = M.plan_edit("guide")["plan_id"]
    a = M.apply_plan(pid, x=0.5, pitch=0.5)
    b = M.apply_plan(pid, x=0.8, pitch=0.8, replaces=a["changeset"])
    plain.undo()                                   # b を取り消す
    assert not plain.edits
    r = plain.redo()
    assert r.id == b["changeset"], "redo は b（捨てた a ではない）"
    assert not plain.can_redo()
    assert M.plan_edit("guide")["ok"]


def test_reset_removes_timing_inside_a_note(plain, tmp_path):
    """頭・尻が動いていないノートの中の伸縮（範囲の stretch）も、原音に戻すで外れる。"""
    from vocal_engine import mcp_server as M
    M._state["project"] = plain
    n = plain.note("n007")
    r = M.stretch(1.3, start_sec=n.start_sec + 0.1, end_sec=n.start_sec + 0.2)
    assert r["ok"] and r["changeset"]
    diff, _ = _export_diff(plain, tmp_path)
    assert diff is not None
    r = M.reset_to_original(note_ids=["n007"])
    assert r["ok"] and r["changeset"]
    diff, _ = _export_diff(plain, tmp_path)
    assert diff is None, diff


def test_plan_carries_connection_changes_for_the_preview(plain):
    """計画の JSON に「確定で変わる接続」と「吸着で生まれるつなぎ」が載り、確定後と同じ（#2）。

    画面はこれでドラッグ中の曲線を描く（離した後になだらかさが現れる／消えることが無いように）。"""
    from vocal_engine.project import timing as TM
    from vocal_engine.project.model import Target
    from vocal_engine.project.pitch import pitch_model
    # 後ろのノートのピッチを動かしておく（Δ ≠ 0 でないと、つなぎは曲線に出ない）
    plain.apply_edits([{"kind": "pitch_shift", "target": Target.note("n006"),
                        "params": {"cents": 200.0}}], author="human")
    trs0 = {(t.a, t.b): t for t in pitch_model(plain)[3]}
    assert ("n005", "n006") in trs0 and abs(trs0[("n005", "n006")].delta) > 100

    # Alt で切り離す計画: 確定で外れる接続が載る
    plan = TM.plan_edge(plain, "n005", "end", detach=True)
    j = plan.to_json()
    assert j["set_connections"] == [["n005", "n006", False]]
    # 切った直後の位置（x = 0）が吸着の位置。x = 0 は何も確定しない（画面も x ≠ 0 のときだけ反映する）
    assert j["snap_x"] == 0.0
    TM.apply_plan(plain, plan, -0.04)
    assert ("n005", "n006") not in {(t.a, t.b) for t in pitch_model(plain)[3]}

    # 切り離された端を伸ばす計画: 吸着したときのつなぎ = 確定後のつなぎ
    plan2 = TM.plan_edge(plain, "n005", "end")
    j2 = plan2.to_json()
    assert j2["set_connections"] == []
    snap = j2["snap_transition"]
    assert snap and (snap["a"], snap["b"]) == ("n005", "n006")
    TM.apply_plan(plain, plan2, plan2.snap_x)
    after = {(t.a, t.b): t.to_json() for t in pitch_model(plain)[3]}[("n005", "n006")]
    for k in ("ta", "tb", "hl", "hr", "delta", "value", "auto_sec"):
        assert snap[k] == pytest.approx(after[k], abs=1e-6), k
