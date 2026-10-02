# -*- coding: utf-8 -*-
"""編集リストの適用・取り消し・保存。"""
import json
import os

import numpy as np
import pytest

from conftest import needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


def test_open_records_source(project):
    assert project.take["sha256"] and len(project.take["sha256"]) == 64
    assert project.guide["sha256"]
    assert os.path.exists(project.json_path)
    assert os.path.exists(project.log_path)


def test_apply_and_undo(project):
    from vocal_engine.project.model import Target
    project.analyze()
    nid = [n.id for n in project.take_notes if n.kind == "note"][0]

    cs1, e1 = project.apply_edits(
        [{"kind": "pitch_shift", "target": Target.note(nid), "params": {"cents": 120}}],
        author="ai", label="テスト")
    assert len(project.edits) == 1
    assert project.edits[0].changeset == cs1.id
    assert project.edits[0].author == "ai"

    cs2, e2 = project.apply_edits(
        [{"kind": "stretch", "target": Target.range(0.2, 0.6), "params": {"ratio": 1.2}},
         {"kind": "move", "target": Target.note(nid), "params": {"ms": -20}}],
        author="human")
    assert len(project.edits) == 3

    project.undo()                       # cs2 を取り消し
    assert len(project.edits) == 1
    assert project.edits[0].id == e1[0].id

    project.undo(cs1.id)
    assert project.edits == []

    with pytest.raises(Exception):
        project.undo()                   # もう戻せない


def test_undo_specific_changeset_keeps_order(project):
    from vocal_engine.project.model import Target
    project.analyze()
    a, _ = project.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.0, 0.5),
                                 "params": {"cents": 100}}])
    b, _ = project.apply_edits([{"kind": "pitch_shift", "target": Target.range(1.0, 1.5),
                                 "params": {"cents": -50}}])
    c, _ = project.apply_edits([{"kind": "stretch", "target": Target.range(2.0, 2.5),
                                 "params": {"ratio": 1.1}}])
    project.undo(b.id)                   # 真ん中だけ取り消す
    kinds = [(e.kind, e.target.start_sec) for e in project.edits]
    assert kinds == [("pitch_shift", 0.0), ("stretch", 2.0)]


def test_reset_to_original(project):
    from vocal_engine.project.model import Target
    project.analyze()
    nid = [n.id for n in project.take_notes if n.kind == "note"][0]
    project.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid),
                          "params": {"cents": 50}}])
    project.apply_edits([{"kind": "stretch", "target": Target.note(nid),
                          "params": {"ratio": 1.1}}])
    assert len(project.edits) == 2
    ids = [e.id for e in project.edits]
    cs, removed = project.remove_edits(ids)
    assert project.edits == []
    project.undo(cs.id)                  # 「戻す」も取り消せる
    assert len(project.edits) == 2


def test_save_load_roundtrip(project):
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    project.analyze()
    project.apply_edits([{"kind": "pitch_curve", "target": Target.range(0.5, 1.0),
                          "params": {"points": [[0.0, 0], [0.5, 120]]}}], author="human")
    p2 = Project(project.dir).load()
    assert len(p2.edits) == 1
    assert p2.edits[0].kind == "pitch_curve"
    assert p2.edits[0].author == "human"
    assert p2.take["sha256"] == project.take["sha256"]
    with open(project.json_path, encoding="utf-8") as f:
        d = json.load(f)
    assert d["schema_version"] == 2          # 2: ソース ID・ソース内オフセット（DAW 連携 段階 0）
    assert d["take"]["offset_frames"] == 0 and d["take"]["source_id"].startswith("sha256:")
    assert d["edits"][0]["params"]["points"] == [[0.0, 0.0], [0.5, 120.0]]


def test_edit_validation():
    from vocal_engine.project.model import Edit, Target
    with pytest.raises(ValueError):
        Edit(id="e1", kind="unknown", target=Target.range(0, 1), params={})
    with pytest.raises(ValueError):
        Edit(id="e1", kind="stretch", target=Target.range(0, 1), params={"ratio": 100.0})
    with pytest.raises(ValueError):
        Edit(id="e1", kind="pitch_curve", target=Target.range(0, 1), params={"points": [[0, 0]]})
    e = Edit(id="e1", kind="pitch_shift", target=Target.range(0, 1), params={"cents": 12})
    assert "セント" in e.describe()


def test_segments_split_at_every_edge():
    """重なる編集は**端で切って**区間ごとに足し合わせる。

    段階1 は「重なったら 1 つの区間に畳む」だったが、それだと
    ノート単位のピッチ編集（範囲が広い）と音素単位の伸縮（範囲が狭い）が
    同居したときに、狭い方の伸縮比が広い方の区間全体に掛かってしまう（段階2）。
    """
    from vocal_engine.project.model import Edit, Target
    from vocal_engine.render.pipeline import edits_to_segments
    edits = [
        Edit(id="e1", kind="pitch_shift", target=Target.range(0.0, 1.0), params={"cents": 100}),
        Edit(id="e2", kind="pitch_shift", target=Target.range(0.5, 1.5), params={"cents": 50}),
        Edit(id="e3", kind="stretch", target=Target.range(2.0, 3.0), params={"ratio": 1.2}),
    ]
    segs = edits_to_segments(edits, lambda e: (e.target.start_sec, e.target.end_sec))
    assert [(s.start_sec, s.end_sec, s.cents) for s in segs[:3]] == [
        (0.0, 0.5, 100.0), (0.5, 1.0, 150.0), (1.0, 1.5, 50.0)]   # 重なりだけ足される
    assert len(segs) == 4
    assert segs[3].ratio == pytest.approx(1.2)


def test_segments_keep_narrow_stretch_inside_a_wide_pitch_edit():
    """広いピッチ編集の中にある狭い伸縮が、広い方に漏れないこと。"""
    from vocal_engine.project.model import Edit, Target
    from vocal_engine.render.pipeline import edits_to_segments
    edits = [
        Edit(id="e1", kind="pitch_shift", target=Target.range(0.0, 1.0), params={"cents": 200}),
        Edit(id="e2", kind="stretch", target=Target.range(0.4, 0.6), params={"ratio": 1.5}),
    ]
    segs = edits_to_segments(edits, lambda e: (e.target.start_sec, e.target.end_sec))
    stretched = [s for s in segs if abs(s.ratio - 1.0) > 1e-9]
    assert len(stretched) == 1
    assert (stretched[0].start_sec, stretched[0].end_sec) == (0.4, 0.6)
    assert all(abs(s.cents - 200.0) < 1e-9 for s in segs)
    out_len = sum((s.end_sec - s.start_sec) * s.ratio for s in segs)
    assert out_len == pytest.approx(1.0 + 0.2 * 0.5)     # 伸びるのは 0.2 秒ぶんだけ


def test_render_unedited_is_bitwise_original(project):
    """編集していない区間は原音のサンプルそのままであること。"""
    from vocal_engine.project.model import Target
    from vocal_engine.render.pipeline import Renderer, edits_to_segments
    project.analyze()
    x, sr = project.audio("take")
    f0r = project.take_f0
    r = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    project.apply_edits([{"kind": "pitch_shift", "target": Target.range(1.0, 1.4),
                          "params": {"cents": 300}}])
    segs = edits_to_segments(project.edits, project.edit_span)
    y, info = r.render_range(0.0, project.duration_sec, segs)
    assert len(y) == pytest.approx(len(x), abs=3)
    head = int(0.9 * sr)                 # 1.0 s の 10 ms 手前まで（クロスフェードの外）
    assert np.allclose(y[:head], x[:head], atol=1e-12), "編集していない区間が変わっている"
    tail = int(1.5 * sr)
    assert np.allclose(y[tail:len(x)], x[tail:len(x)], atol=1e-12)
    mid = slice(int(1.05 * sr), int(1.35 * sr))
    assert not np.allclose(y[mid], x[mid]), "編集区間が変わっていない"
    assert info["edited_chunks"] == 1


def test_render_move_changes_position_not_length(project):
    from vocal_engine.project.model import Target
    from vocal_engine.render.pipeline import Renderer, edits_to_segments
    project.analyze()
    x, sr = project.audio("take")
    f0r = project.take_f0
    r = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    project.apply_edits([{"kind": "move", "target": Target.range(1.0, 1.4),
                          "params": {"ms": 40}}])
    segs = edits_to_segments(project.edits, project.edit_span)
    y, info = r.render_range(0.0, project.duration_sec, segs)
    assert len(y) == pytest.approx(len(x), abs=3), "移動で全体長が変わってはいけない"
    assert info["warnings"] == []
