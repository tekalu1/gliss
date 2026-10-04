# -*- coding: utf-8 -*-
"""「ガイドに合わせる」のピッチは、今の（編集後の）音程から寄せる。

鉛筆（set_pitch_curve(mode="draw")）・shift_pitch で直したノートに correct_to_guide を掛けると、
前は元の音程からの差を足していたので、鉛筆の線の上に差が二重に乗った（直した分が消えた）。
`edited_notes="skip"` は直し済みのノートの音程を動かさない。
"""
import json
import shutil

import numpy as np
import pytest

from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


@pytest.fixture
def plain(tmp_path):
    from vocal_engine import mcp_server as S
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "plain"))
    p.analyze()
    S._state["project"] = p
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def _edited_curve(p):
    from vocal_engine.view.export_data import export_view_data
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        d = json.load(f)
    return d["f0"]["t0_sec"], d["f0"]["hop_sec"], d["f0"]["take_edited_midi"]


def _center(p, n):
    """音として鳴る（なだらかさ・鉛筆込み）ノートの音程の中央値（MIDI）。"""
    t0, hop, m = _edited_curve(p)
    a, b = int(np.ceil((n.start_sec - t0) / hop)), int(np.floor((n.end_sec - t0) / hop))
    v = [x for x in m[a:b] if x is not None]
    return float(np.median(v))


def _target(p, many=False):
    """ガイドと対応する、いちばん長い音程ノートとガイドの音程。

    many=True は、ノートの中心を一定量ずらす寄せ方（match_pitch_shape=False）の寄せ先: テイクの 1 ノートに
    高さの違うガイドのノートが重なる所（1 対多）は、組の 1 つのノートの音ではなく、フレームごとの差から出した高さ。"""
    from vocal_engine.project import timing as TM
    _, pitch_target, gspan = TM.note_correspondence(p)
    ns = {n.id: n for n in TM.pitched_notes(p)}
    nid = max((i for i, g in pitch_target.items() if g.pitch_midi is not None and i in ns),
              key=lambda i: ns[i].end_sec - ns[i].start_sec)
    g = float(pitch_target[nid].pitch_midi)
    if many:
        m = TM._one_to_many(p, ns, {nid}, gspan)
        if nid in m:
            g = float(m[nid]["pitch_midi"])
    return ns[nid], g


def _draw_to(p, n, midi):
    from vocal_engine import mcp_server as S
    r = S.set_pitch_curve(points=[[n.start_sec, midi], [n.end_sec, midi]], mode="draw",
                          author="human")
    assert r["ok"], r


@pytest.mark.parametrize("shape", [False, True])
def test_guide_pitch_keeps_pencil_fix(plain, shape):
    """鉛筆でガイドの音程に直したノートは、ピッチ 100% を掛けてもガイドの音程のまま（二重にずらさない）。"""
    from vocal_engine import mcp_server as S
    n, g = _target(plain, many=not shape)
    orig = _center(plain, n)
    off = g + 0.6 if abs(orig - (g + 0.6)) > 0.3 else g - 0.6
    _draw_to(plain, n, off)                                # 鉛筆で 60 セントずらした線を描く
    drawn = _center(plain, n)
    assert abs(drawn - off) < 0.1
    r = S.correct_to_guide(note_ids=[n.id], pitch_strength=1.0, timing_strength=0.0,
                           match_pitch_shape=shape)
    assert r["ok"] and r["changeset"], r
    after = _center(plain, n)
    # 前の挙動: 鉛筆の線に「元の中心 → ガイド」の差を足していた（= off + (g - orig)）
    assert abs(after - g) < 0.15, "編集後の音程からガイドへ: after=%.3f guide=%.3f orig=%.3f drawn=%.3f" % (
        after, g, orig, drawn)


def test_guide_pitch_half_strength_from_edited(plain):
    """強度 50% は、今の（編集後の）音程とガイドの中間へ。"""
    from vocal_engine import mcp_server as S
    n, g = _target(plain, many=True)
    S.shift_pitch(80.0, note_id=n.id, author="human")
    cur = _center(plain, n)
    r = S.correct_to_guide(note_ids=[n.id], pitch_strength=0.5, timing_strength=0.0,
                           match_pitch_shape=False)
    assert r["ok"] and r["changeset"]
    after = _center(plain, n)
    assert abs((after - cur) - 0.5 * (g - cur)) < 0.1, (cur, after, g)


def test_guide_pitch_skip_edited_notes(plain):
    """edited_notes="skip": 音程を直し済みのノートは動かさず、ほかのノートだけ寄せる。"""
    from vocal_engine import mcp_server as S
    from vocal_engine.project import timing as TM
    n, g = _target(plain)
    _draw_to(plain, n, g + 0.4)
    drawn = _center(plain, n)
    r = S.correct_to_guide(pitch_strength=1.0, timing_strength=0.0, match_pitch_shape=False,
                           edited_notes="skip")
    assert r["ok"] and r["changeset"], r
    assert n.id in r["skipped_edited_notes"]
    assert abs(_center(plain, n) - drawn) < 0.02
    assert any(e.kind == "pitch_shift" and e.target.note_id != n.id for e in plain.edits)
    with pytest.raises(Exception):
        TM.plan_guide(plain, edited_notes="nope")
