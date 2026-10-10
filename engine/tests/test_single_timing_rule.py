# -*- coding: utf-8 -*-
"""タイミングの規則は 1 つ: 接している隣とは境目を共有、離れていれば隙間が変わる、Alt で自分だけ（`project/timing.py` の冒頭）。

音程のあるノート・子音（無声）・息は、種類によらず同じ扱い（素材なし。合成のノート列）。
- 隣り合う区間の組は、接していれば（隙間 1e-6 秒以下）接続、離れていれば切り離し。種類によらない
- 端を動かす: 接続なら境目を共有して両側が動く、切り離しなら動かした区間だけ（隙間が吸収）、Alt なら動かした区間だけ
- 区間の本体を動かす: 接した隣は伸び縮み、離れた隣は動かない（隙間が吸収）
- アタック（子音が次のノートと一緒に動く）・境目の子音が長さを保って滑る・挟んでいる音程ノートの組の接続の引き継ぎ、は無い
- 無音は区間ではなく隙間
- 組ごとの上書き（`connection`）は今後の編集の動き方だけを決める。確定済みの時間の編集の音は変わらない
"""
import numpy as np
import pytest
import soundfile as sf

from vocal_engine.analysis.notes import Note

SR = 22050


def _note(i, a, b, kind):
    return Note(id="n%02d" % i, start_sec=a, end_sec=b, kind=kind,
                label="sung" if kind == "note" else kind, source="take", text=None,
                pitch_hz=220.0 if kind == "note" else None, pitch_midi=57.0 if kind == "note" else None,
                note_name="A3" if kind == "note" else None, iqr_semitones=0.1 if kind == "note" else None,
                rms_peak_db=-20.0, confidence=1.0, start_frame=int(a * 100), end_frame=int(b * 100))


# ケース H（beta.11）: ノート 0.29 s・子音 0.14 s・ノート 0.07 s・子音 0.08 s・ノート。すべて接している
TOUCHING = [(0.10, 0.39, "note"), (0.39, 0.53, "unvoiced"), (0.53, 0.60, "note"),
            (0.60, 0.68, "unvoiced"), (0.68, 1.00, "note")]
# 離れている: ノート・隙間 0.10・子音・隙間 0.05・ノート・隙間 0.05・息・隙間 0.05・ノート
APART = [(0.10, 0.40, "note"), (0.50, 0.60, "unvoiced"), (0.65, 1.00, "note"),
         (1.05, 1.20, "breath"), (1.25, 1.60, "note")]


@pytest.fixture
def make(tmp_path):
    from vocal_engine.analysis import f0 as F
    from vocal_engine.project import Project
    F.set_preferred_estimator("praat")
    made = []

    def build(spans, extra_notes=()):
        t = np.arange(int(2.0 * SR)) / SR
        path = tmp_path / ("v%d.wav" % len(made))
        sf.write(str(path), (0.2 * np.sin(2 * np.pi * 220 * t)).astype("float32"), SR)
        p = Project.open(str(path), None, project_dir=str(tmp_path / ("p%d" % len(made))))
        p.analyze(auto_lyrics=False)
        notes = [_note(i, a, b, k) for i, (a, b, k) in enumerate(spans)]
        notes += [_note(90 + i, a, b, k) for i, (a, b, k) in enumerate(extra_notes)]
        p._take_notes = sorted(notes, key=lambda n: n.start_sec)
        p._notes_cache = None
        made.append(p)
        return p

    yield build
    F.set_preferred_estimator(None)


def _edges(p):
    from vocal_engine.project import timing as TM
    tm = TM.current_map(p)
    return {n.id: (tm.at(n.start_sec, "right"), tm.at(n.end_sec, "left")) for n in p.take_notes}


def _drag_edge(p, nid, side, x, detach=False):
    from vocal_engine.project import timing as TM
    plan = TM.plan_edge(p, nid, side, detach=detach)
    TM.apply_plan(p, plan, x=x)
    return plan


def _same(a, b):
    return a == pytest.approx(b, abs=1e-9)


def test_adjacent_blocks_of_every_kind_are_connected_only_when_they_touch(make):
    from vocal_engine.project import timing as TM
    p = make(TOUCHING)
    rows = {(a.id, b.id): (c, d) for a, b, c, d in TM.block_connections(p)}
    assert rows == {("n00", "n01"): (True, True), ("n01", "n02"): (True, True),
                    ("n02", "n03"): (True, True), ("n03", "n04"): (True, True)}
    assert ("n00", "n02") not in rows                           # 間に子音が挟まった音程ノートの組は、隣り合う組ではない
    q = make(APART)
    rows = {(a.id, b.id): (c, d) for a, b, c, d in TM.block_connections(q)}
    assert set(rows.values()) == {(False, False)} and len(rows) == 4          # 離れていれば、種類によらず切り離し
    assert TM.connection_map(q) == {k: False for k in rows}


@pytest.mark.parametrize("which", ["note1_end", "consonant_start"])
@pytest.mark.parametrize("x", [0.05, -0.04])
def test_example_note1_end_or_consonant_start_changes_only_the_consonant_and_note1(make, which, x):
    p = make(TOUCHING)
    before = _edges(p)
    plan = _drag_edge(p, "n00", "end", x) if which == "note1_end" else _drag_edge(p, "n01", "start", x)
    assert plan.info["connected"] is True
    after = _edges(p)
    assert _same(after["n00"][1], before["n00"][1] + x)           # ノート 1 の尻が動く
    assert _same(after["n01"][0], before["n01"][0] + x)           # 子音の頭が同じだけ動く（境目を共有）
    assert _same(after["n01"][1], before["n01"][1])               # 子音の尻は動かない（子音が縮む／伸びる）
    for nid in ("n02", "n03", "n04"):
        assert after[nid] == pytest.approx(before[nid], abs=1e-9), nid        # ノート 3 とその先は動かない
    assert _same(after["n00"][0], before["n00"][0])


def test_example_consonant_end_changes_the_consonant_and_note3(make):
    p = make(TOUCHING)
    before = _edges(p)
    _drag_edge(p, "n01", "end", 0.02)
    after = _edges(p)
    assert _same(after["n01"][1], before["n01"][1] + 0.02)
    assert _same(after["n02"][0], before["n02"][0] + 0.02)        # ノート 3 の頭が一緒に動く（ノート 3 が縮む）
    assert _same(after["n02"][1], before["n02"][1])
    assert after["n00"] == pytest.approx(before["n00"], abs=1e-9)
    assert _same(after["n01"][0], before["n01"][0])
    for nid in ("n03", "n04"):
        assert after[nid] == pytest.approx(before[nid], abs=1e-9)


@pytest.mark.parametrize("nid,side", [("n00", "end"), ("n01", "start"), ("n01", "end"), ("n02", "start")])
def test_alt_changes_only_the_dragged_block(make, nid, side):
    p = make(TOUCHING)
    before = _edges(p)
    plan = _drag_edge(p, nid, side, -0.02 if side == "end" else 0.02, detach=True)   # 縮める向き（隙間ができる）
    after = _edges(p)
    assert plan.set_connections and plan.set_connections[0][2] is False
    changed = {k for k in before if after[k] != pytest.approx(before[k], abs=1e-9)}
    assert changed == {nid}
    assert _same(after[nid][1] - after[nid][0], before[nid][1] - before[nid][0] - 0.02)


def test_dragging_into_a_detached_touching_neighbour_is_blocked_and_the_snap_reconnects(make):
    from vocal_engine.project import timing as TM
    p = make(TOUCHING)
    plan = _drag_edge(p, "n00", "end", -0.02, detach=True)
    assert plan.snap_x is not None                                # 切り離した端を隣にぶつかるまで伸ばすと吸着する
    again = TM.plan_edge(p, "n00", "end")
    assert again.info["connected"] is False                       # 上書きが組ごとに残っている
    assert again.x_hi == pytest.approx(0.02, abs=1e-6)            # 隙間の分だけ伸ばせる。それ以上は隣を追い越す


def test_apart_blocks_change_alone_and_the_gap_absorbs(make):
    p = make(APART)
    before = _edges(p)
    _drag_edge(p, "n00", "end", 0.03)                             # 隙間 0.10 へ伸ばす
    after = _edges(p)
    assert _same(after["n00"][1], before["n00"][1] + 0.03)
    for nid in ("n01", "n02", "n03", "n04"):
        assert after[nid] == pytest.approx(before[nid], abs=1e-9), nid
    q = make(APART)
    before = _edges(q)
    _drag_edge(q, "n01", "start", -0.02)                          # 子音の頭を前へ（隙間 0.10 の中）
    after = _edges(q)
    assert _same(after["n01"][0], before["n01"][0] - 0.02)
    for nid in ("n00", "n02", "n03", "n04"):
        assert after[nid] == pytest.approx(before[nid], abs=1e-9), nid


@pytest.mark.parametrize("nid", ["n01", "n03"])
def test_moving_a_consonant_or_breath_leaves_apart_neighbours_alone(make, nid):
    from vocal_engine.project import timing as TM
    p = make(APART)
    before = _edges(p)
    plan = TM.plan_move(p, [nid])
    TM.apply_plan(p, plan, x=0.02)
    after = _edges(p)
    assert _same(after[nid][0], before[nid][0] + 0.02) and _same(after[nid][1], before[nid][1] + 0.02)
    for other in before:
        if other != nid:
            assert after[other] == pytest.approx(before[other], abs=1e-9), other


def test_moving_a_block_stretches_touching_neighbours_whatever_their_kind(make):
    from vocal_engine.project import timing as TM
    p = make(TOUCHING)
    before = _edges(p)
    TM.apply_plan(p, TM.plan_move(p, ["n01"]), x=0.01)           # 子音を動かす: 両側の接した隣が伸び縮み
    after = _edges(p)
    assert _same(after["n01"][0], before["n01"][0] + 0.01) and _same(after["n01"][1], before["n01"][1] + 0.01)
    assert _same(after["n00"][1], before["n00"][1] + 0.01)         # ノート 1 が伸びる
    assert _same(after["n02"][0], before["n02"][0] + 0.01)         # ノート 3 が縮む
    assert _same(after["n00"][0], before["n00"][0]) and _same(after["n02"][1], before["n02"][1])
    for nid in ("n03", "n04"):
        assert after[nid] == pytest.approx(before[nid], abs=1e-9)
    # ノートを動かしても同じ規則（接した子音が伸び縮みして、その先は動かない）
    q = make(TOUCHING)
    before = _edges(q)
    TM.apply_plan(q, TM.plan_move(q, ["n02"]), x=0.01)
    after = _edges(q)
    assert _same(after["n02"][0], before["n02"][0] + 0.01) and _same(after["n02"][1], before["n02"][1] + 0.01)
    assert _same(after["n01"][1], before["n01"][1] + 0.01)         # 前の子音の尻が動く（子音が伸びる）
    assert _same(after["n03"][0], before["n03"][0] + 0.01)         # 後ろの子音の頭が動く（縮む）
    for nid in ("n00", "n04"):
        assert after[nid] == pytest.approx(before[nid], abs=1e-9)
    assert _same(after["n01"][0], before["n01"][0]) and _same(after["n03"][1], before["n03"][1])


def test_a_pair_override_applies_to_the_pair_being_dragged_whatever_the_kinds(make):
    from vocal_engine.project import timing as TM
    p = make(TOUCHING)
    rm, add = TM.connection_specs(p, [("n01", "n02", False)])      # 子音とノート 3 を切り離す（右クリックの「切り離す」）
    assert len(add) == 1 and add[0]["params"] == {"a": "n01", "b": "n02", "connected": False}
    p.apply_edits(add, author="human")
    assert {(a.id, b.id): c for a, b, c, _ in TM.block_connections(p)}[("n01", "n02")] is False
    before = _edges(p)
    _drag_edge(p, "n01", "end", -0.03)                             # 子音の尻を縮める: 子音だけ（隙間ができる）
    after = _edges(p)
    assert _same(after["n01"][1], before["n01"][1] - 0.03)
    assert after["n02"] == pytest.approx(before["n02"], abs=1e-9)
    # 子音の頭（ノート 1 とは接続のまま）は、ノート 1 と境目を共有
    _drag_edge(p, "n01", "start", 0.02)
    after2 = _edges(p)
    assert _same(after2["n00"][1], after["n00"][1] + 0.02)
    # 隣り合わない組・知らない組は覚えない
    assert TM.connection_specs(p, [("n00", "n02", False)]) == ([], [])


def test_an_old_override_on_a_pitched_pair_with_a_consonant_between_is_ignored_for_timing(make):
    from vocal_engine.project import timing as TM
    from vocal_engine.project.model import Target
    p = make(TOUCHING)
    stored = {"kind": "connection", "target": Target.range(0.39, 0.53),
              "params": {"a": "n00", "b": "n02", "connected": False}}      # beta.10 以前に付いた古い切り離し
    p.apply_edits([stored], author="human")
    assert TM.connection_overrides(p)[("n00", "n02")] is False             # 保存はされている
    assert ("n00", "n02") not in TM.connection_map(p)                      # 骨組みの組には無い
    before = _edges(p)
    _drag_edge(p, "n00", "end", 0.04)                                      # 接した子音が縮むだけ（古い上書きの影響なし）
    after = _edges(p)
    assert _same(after["n01"][0], before["n01"][0] + 0.04)
    assert after["n02"] == pytest.approx(before["n02"], abs=1e-9)


def test_silence_is_a_gap_not_a_block(make):
    from vocal_engine.project import timing as TM
    p = make([(0.10, 0.40, "note"), (0.60, 1.00, "note")], extra_notes=[(0.40, 0.60, "silence")])
    assert [n.id for n in TM.blocks(p)] == ["n00", "n01"]
    rows = TM.block_connections(p)
    assert len(rows) == 1 and rows[0][2] is False                         # 無音を挟む組は隣り合う区間: 離れている = 切り離し
    before = _edges(p)
    _drag_edge(p, "n00", "end", 0.05)
    after = _edges(p)
    assert _same(after["n00"][1], before["n00"][1] + 0.05)
    assert after["n01"] == pytest.approx(before["n01"], abs=1e-9)
    with pytest.raises(TM.TimingError):
        TM.plan_edge(p, "n90", "end")                                      # 無音そのものは動かせない（区間ではない）


def test_pitch_transitions_still_use_the_beta10_defaults(make):
    """ピッチのつなぎ（pitch.transitions）は、これまでの曲の音を変えないため、beta.10 までの既定のまま。"""
    from vocal_engine.project import timing as TM
    p = make(TOUCHING)
    legacy = {(a.id, b.id): c for a, b, c, _ in TM.connections(p, legacy=True)}
    assert legacy == {("n00", "n02"): True, ("n02", "n04"): True}
    assert {(a.id, b.id): c for a, b, c, _ in TM.connections(p)} == {("n00", "n02"): False, ("n02", "n04"): False}
