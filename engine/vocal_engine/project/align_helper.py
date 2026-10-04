# -*- coding: utf-8 -*-
"""Project から DTW を呼ぶための薄い橋（循環 import を避けるため別ファイル）。"""
import numpy as np

from ..analysis.align import DEFAULT_METHOD, Alignment, align_seconds

REFINE_MAX = 24                 # 取り直す区間の数の上限
REFINE_GAP_SEC = 1.0            # これより近い発声は 1 つの区間にまとめる


def refine_ranges(project):
    """10 ms で取り直す区間 = 歌詞を付けたところ。無ければ発声のかたまり。"""
    ent = project.lyrics_entries("take")
    if ent:
        from ..phoneme import lyrics as LY
        return LY.spans(ent, project.duration_sec)
    spans = [(n.start_sec, n.end_sec) for n in project.take_notes if n.kind == "note"]
    if not spans:
        return []
    out = [list(spans[0])]
    for a, b in spans[1:]:
        if a - out[-1][1] <= REFINE_GAP_SEC:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    out.sort(key=lambda r: r[1] - r[0], reverse=True)
    return [tuple(r) for r in sorted(out[:REFINE_MAX])]


def fallback_alignment(project, err, method=DEFAULT_METHOD):
    """対応付け（DTW）が失敗したときの代わり: **タイムライン上の位置のまま**（テイクの t 秒 ↔ ガイドの t 秒）。

    ガイドはテイクの頭のタイムライン上の位置から切り出して渡しているので（`session.guide_clip_for`）、
    位置のままでも大きくは外れない。`info.stage = "fallback"` と理由を残す（analyze_take の返り値の
    alignment.dtw）。"""
    d = max(0.01, min(float(project.duration_sec), float(project.guide["duration_sec"])))
    al = Alignment.from_seconds(np.array([0.0, d]), np.array([0.0, d]), method=method)
    al.info = {"stage": "fallback", "error": str(err)[:300],
               "note": "ガイドとの対応付けに失敗したので、タイムライン上の位置のまま重ねている"}
    return al


def compute_alignment(project, method=DEFAULT_METHOD):
    tx, tsr = project.audio("take")
    gx, gsr = project.audio("guide")
    # 譜面ガイド（タイムラインに置いた合成音）は同じ時間軸と分かっている: ずれ 0 の帯に限る
    # （声と合成音では発音の強さの包絡が似ず、全体のずれの推定が当てにならない）
    timeline = 0.0 if project.score_guide() is not None else True
    ta, ga, fr, info = align_seconds(tx, tsr, gx, gsr, method=method,
                                     refine_ranges=refine_ranges(project), timeline=timeline)
    al = Alignment.from_seconds(ta, ga, method=method, feature_rate=fr)
    al.info = dict(info)
    return al
