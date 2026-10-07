# -*- coding: utf-8 -*-
"""接続の既定（`timing.default_connected`）: 本当に接しているときだけ接続。素材なし（合成のノート列）。

ノートの端を動かしても、子音（無声）・息・無音を挟んで離れた次のノートは動かない（隙間の中の piece が吸収する）。
利用者の曲で、無声を挟む 0.30 秒未満の組（隣り合う組の約 4 分の 1）が接続になっていて、ノート 1 の端を動かすと
無声が動き、ノート 3 が伸び縮みした。ユーザーが明示した接続・切り離し（`connection` 編集）と Alt のドラッグは今までどおり。
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


# ノート 0.29 s・無声 0.14 s・ノート 0.07 s・無声 0.08 s・ノート（すべて接している）
SPANS = [(0.10, 0.39, "note"), (0.39, 0.53, "unvoiced"), (0.53, 0.60, "note"),
         (0.60, 0.68, "unvoiced"), (0.68, 1.00, "note")]


@pytest.fixture
def proj(tmp_path):
    from vocal_engine.analysis import f0 as F
    from vocal_engine.project import Project
    F.set_preferred_estimator("praat")
    t = np.arange(int(1.2 * SR)) / SR
    path = tmp_path / "v.wav"
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 220 * t)).astype("float32"), SR)
    p = Project.open(str(path), None, project_dir=str(tmp_path / "p"))
    p.analyze(auto_lyrics=False)
    p._take_notes = [_note(i, a, b, k) for i, (a, b, k) in enumerate(SPANS)]
    p._notes_cache = None
    yield p
    F.set_preferred_estimator(None)


def _edges(p):
    from vocal_engine.project import timing as TM
    tm = TM.current_map(p)
    return {n.id: (tm.at(n.start_sec, "right"), tm.at(n.end_sec, "left")) for n in p.take_notes}


def test_notes_separated_by_an_unvoiced_piece_are_detached_by_default(proj):
    from vocal_engine.project import timing as TM
    rows = {(a.id, b.id): (c, d) for a, b, c, d in TM.connections(proj)}
    assert rows[("n00", "n02")] == (False, False)      # 無声を挟む（隙間 0.14 s）
    assert rows[("n02", "n04")] == (False, False)      # 無声を挟む（隙間 0.08 s）
    # 本当に接していれば接続
    proj._take_notes = [_note(0, 0.1, 0.4, "note"), _note(1, 0.4, 0.7, "note")]
    proj._notes_cache = None
    assert [(c, d) for *_, c, d in TM.connections(proj)] == [(True, True)]
    # ピッチのつなぎだけは、これまでの曲の音を変えないよう beta.10 までの既定（無声だけを挟む 0.30 秒未満も）
    proj._take_notes = [_note(i, a, b, k) for i, (a, b, k) in enumerate(SPANS)]
    proj._notes_cache = None
    legacy = {(a.id, b.id): c for a, b, c, _ in TM.connections(proj, legacy=True)}
    assert legacy == {("n00", "n02"): True, ("n02", "n04"): True}


@pytest.mark.parametrize("x", [-0.04, -0.20])
def test_dragging_the_end_of_note_1_changes_only_note_1(proj, x):
    from vocal_engine.project import timing as TM
    before = _edges(proj)
    plan = TM.plan_edge(proj, "n00", "end")
    assert plan.info["neighbour"] == "n02" and plan.info["connected"] is False
    # 子音（無声）は次のノートの頭に付いたアタックなので、ノート 1 の端は子音の方へは伸ばせない（切り離しの隙間が 0）
    assert plan.x_hi == 0.0 and plan.x_lo < x
    TM.apply_plan(proj, plan, x=x)
    after = _edges(proj)
    assert abs(after["n00"][1] - (before["n00"][1] + x)) < 1e-6          # ドラッグしたノートの尻だけ動く
    assert abs(after["n00"][0] - before["n00"][0]) < 1e-9
    for nid in ("n01", "n02", "n03", "n04"):                              # 子音・次のノート・その先は 1 サンプルも動かない
        assert after[nid] == pytest.approx(before[nid], abs=1e-9), nid


def test_the_same_drag_used_to_move_the_next_note(proj):
    """beta.10 までの既定（legacy）の接続を明示すると、ノート 3 が一緒に動く（この試験が直した症状の再現）。"""
    from vocal_engine.project import timing as TM
    rm, add = TM.connection_specs(proj, [("n00", "n02", True)])
    proj.apply_edits(add, author="human")
    before = _edges(proj)
    plan = TM.plan_edge(proj, "n00", "end")
    assert plan.info["connected"] is True
    TM.apply_plan(proj, plan, x=-0.04)
    after = _edges(proj)
    assert after["n02"] != pytest.approx(before["n02"], abs=1e-6)


def test_explicit_connect_detach_and_alt_still_work(proj):
    from vocal_engine.project import timing as TM
    # 明示的につなぐ（connection 編集。既定と違うので編集リストに入る）→ 次のノートが一緒に動く
    rm, add = TM.connection_specs(proj, [("n00", "n02", True)])
    assert len(add) == 1 and add[0]["params"]["connected"] is True
    proj.apply_edits(add, author="human")
    assert {(a.id, b.id): c for a, b, c, _ in TM.connections(proj)}[("n00", "n02")] is True
    plan = TM.plan_edge(proj, "n00", "end")
    assert plan.info["connected"] is True
    before = _edges(proj)
    TM.apply_plan(proj, plan, x=0.03)
    after = _edges(proj)
    assert after["n02"][0] != pytest.approx(before["n02"][0], abs=1e-6)  # 接続: 次のノートの頭が動く
    # Alt（detach）: つないだ組を切り離して自分だけ動く（連続した接続の変更は計画が持つ）
    alt = TM.plan_edge(proj, "n00", "end", detach=True)
    assert alt.set_connections == [("n00", "n02", False)]
    # 切り離しの既定のままの組を「切り離す」と指定しても、編集リストには何も増えない（既定と同じ）
    rm2, add2 = TM.connection_specs(proj, [("n02", "n04", False)])
    assert add2 == []
