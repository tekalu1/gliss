# -*- coding: utf-8 -*-
"""音素を単位にしたタイミング編集（段階2）。

2 本立て:

  - **手動**: `move_boundary(boundary_id, ms)` で**すべての境界**（子音｜母音も含む）を動かせる。
    1 本動かすと隣り合う 2 つの音素の長さが同時に変わる。どちらも 20 ms を下回らない。
  - **ガイドへ寄せる**は `project/timing.py: plan_guide()` に移した（画面のプレビューと
    確定が同じ計画を使うため）。母音（と息・無音）だけ伸縮し、子音の長さは保つ規則はそのまま。

どちらも**後ろをずらさない**（move_boundary は 2 つの音素の長さの和を保つ）。
"""
from __future__ import annotations

import numpy as np

from .model import MIN_PHONEME_MS


class BoundaryEditError(RuntimeError):
    pass


def _edited_len(src_pts, out_pts, a, b):
    ea, eb = np.interp([float(a), float(b)], src_pts, out_pts)
    return float(eb - ea)


def move_boundary_spec(project, boundary_id, ms, source="take"):
    """`move_boundary` の編集 1 件を組み立てる。(spec, info) を返す。

    ms は**編集後の時間軸での移動量**（画面でつまんだ距離そのもの）。
    左右の音素の「今の長さ」に対する伸縮比に直して params に入れるので、
    同じ境界を何度つまんでも積み上がる。
    """
    res = project.phonemes(source)
    if res is None:
        raise BoundaryEditError("音素がまだ無い（set_lyrics → analyze_take）")
    b = res.boundary(boundary_id)
    by = {p.index: p for p in res.phonemes}
    left = by.get(b.before_index)
    right = by.get(b.after_index)
    if left is None or right is None:
        raise BoundaryEditError(
            "%s は素材の端の境界なので動かせない（片側に音素が無い）" % boundary_id)

    src_pts, out_pts = project.time_map()
    Le = _edited_len(src_pts, out_pts, left.start_sec, b.time_sec)
    Re = _edited_len(src_pts, out_pts, b.time_sec, right.end_sec)
    lo = MIN_PHONEME_MS / 1000.0
    if Le <= lo * 0.5 or Re <= lo * 0.5:
        raise BoundaryEditError("%s の隣の音素が短すぎて動かせない（%.0f / %.0f ms）"
                                % (boundary_id, Le * 1000, Re * 1000))
    dt = float(ms) / 1000.0
    want = dt
    dt = float(np.clip(dt, lo - Le, Re - lo))
    clamped = abs(dt - want) > 1e-9

    spec = {
        "kind": "move_boundary",
        "target": None,                      # 呼び出し側で Target.boundary を入れる
        "params": {
            "boundary_id": boundary_id, "ms": round(dt * 1000.0, 2),
            "left_sec": left.start_sec, "boundary_sec": b.time_sec,
            "right_sec": right.end_sec,
            "left_ratio": (Le + dt) / Le, "right_ratio": (Re - dt) / Re,
            "left_text": left.text, "right_text": right.text,
        },
    }
    info = {
        "boundary_id": boundary_id, "kind": b.kind,
        "ms": round(dt * 1000.0, 1), "requested_ms": round(float(ms), 1),
        "clamped": clamped,
        "left": {"id": left.id, "text": left.text, "label": left.label,
                 "ms_before": round(Le * 1000, 1), "ms_after": round((Le + dt) * 1000, 1)},
        "right": {"id": right.id, "text": right.text, "label": right.label,
                  "ms_before": round(Re * 1000, 1), "ms_after": round((Re - dt) * 1000, 1)},
        "min_phoneme_ms": MIN_PHONEME_MS,
    }
    if clamped:
        info["note"] = "20 ms の下限に当たったので %.0f ms に制限した" % (dt * 1000)
    return spec, info
