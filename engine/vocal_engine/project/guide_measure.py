# -*- coding: utf-8 -*-
"""補正の前後の、ガイドとの残差（ノートごと）— MCP の `measure_against_guide` と `list_deviations` の `onset_timing`。

物差しは「ガイドに合わせる」（`timing.plan_guide`）と同じ:

- **音程**: テイクの音程ノートと、音程で対応するガイドのノート（`timing.note_correspondence`）の中心
  （ガイドのノートの `pitch_midi`）との差（セント。+ がテイクの方が高い）。中心は有声のフレームの MIDI の中央値
  （解析のノートの `pitch_midi` と同じ定義）。
- **タイミング**: 発音の頭の組（`analysis/guide_timing.py` の 1 対 1 の組。計画の `timing`）の、
  テイクの頭と合わせる先（`target_sec` = タイムライン上のガイドの頭。置き場所の違う素材は + 全体のずれ）の差
  （ms。+ がテイクの方が遅い）。ノートの頭（音程の変わり目）とガイドのノートの頭の差ではない。

値は 3 つ:

- `before`: 元の音（編集前）。
- `edited`: 編集の中身から求めた値（再合成しない。音程は shift_pitch・曲線・鉛筆を足した中心、
  頭は時間の写像で移した位置）。
- `after`: 編集後の音を再合成して測り直した値（`render=True`。F0 は解析と同じ方式、頭は `onsets.detect`
  で拾い直して、移した位置から `ONSET_MATCH_SEC` 以内のもの）。
"""
from __future__ import annotations

import numpy as np

from . import timing as TM

ONSET_MATCH_SEC = 0.05       # 編集後の音で、移した位置からこの範囲の発音の頭を「同じ頭」とみなす
RENDER_PAD_SEC = 0.5         # 再合成して測るときに前後へ広げる秒
MIN_VOICED = 3               # ノートの中心を測るのに要る有声のフレームの数


def _median_midi(f0, voiced, times, a, b):
    m = (times >= a) & (times < b) & np.asarray(voiced, dtype=bool) & (np.asarray(f0) > 0)
    if int(m.sum()) < MIN_VOICED:
        return None, int(m.sum())
    midi = 69.0 + 12.0 * np.log2(np.asarray(f0, dtype="float64")[m] / 440.0)
    return float(np.median(midi)), int(m.sum())


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def guide_timing(project):
    from ..analysis import guide_timing as GT
    return GT.of_project(project)


def onset_rows(project, t0=None, t1=None, gt=None, tm=None):
    """発音の頭の組ごとの before / edited（ms）。gt が無ければ []。"""
    gt = guide_timing(project) if gt is None else gt
    if gt is None:
        return []
    tm = TM.current_map(project) if tm is None else tm
    out = []
    for pr in gt.pairs:
        if t0 is not None and pr.take_sec < t0:
            continue
        if t1 is not None and pr.take_sec >= t1:
            continue
        cur = tm.at(pr.take_sec, "right")
        out.append({"take_sec": round(pr.take_sec, 4), "edited_sec": round(cur, 4),
                    "target_sec": round(pr.target_sec, 4),
                    "before_ms": _r((pr.take_sec - pr.target_sec) * 1000.0),
                    "edited_ms": _r((cur - pr.target_sec) * 1000.0)})
    return out


def _abs_stats(vals):
    v = np.abs(np.asarray([x for x in vals if x is not None], dtype="float64"))
    if not len(v):
        return {"n": 0, "abs_median": None, "abs_p90": None, "abs_max": None}
    return {"n": int(len(v)), "abs_median": _r(np.median(v)), "abs_p90": _r(np.percentile(v, 90)),
            "abs_max": _r(np.max(v))}


def onset_summary(rows, threshold_ms=None):
    s = {"pairs": len(rows),
         "before_ms": _abs_stats([r["before_ms"] for r in rows]),
         "edited_ms": _abs_stats([r["edited_ms"] for r in rows])}
    if threshold_ms is not None:
        s["over_threshold_before"] = sum(1 for r in rows if abs(r["before_ms"]) >= threshold_ms)
        s["over_threshold_edited"] = sum(1 for r in rows if abs(r["edited_ms"]) >= threshold_ms)
    return s


def _render_measure(project, a, b, backend, renderer):
    """[a, b)（プロジェクトの秒）を編集を当てて再合成し、(F0 の結果, 発音の頭の秒の列)。時刻はプロジェクトの秒。"""
    from ..analysis import f0 as F0
    from ..analysis import onsets as ON
    from ..render.region import render_region
    y, info = render_region(project, a, b, backend=backend, channels="mono", renderer=renderer)
    y = np.asarray(y, dtype="float64")
    if y.ndim > 1:
        y = y.mean(axis=1)
    sr = int(info["sr"])
    f0r = F0.estimate_f0(x=y, sr=sr, estimator=project.take_f0.estimator)
    ons = np.asarray(ON.detect(y, sr), dtype="float64") + a
    return f0r, ons, info


def _within(vals, lim):
    v = [abs(x) for x in vals if x is not None]
    return None if not v else round(100.0 * sum(1 for x in v if x <= lim) / len(v), 1)


def _segment_summary(project, tm, t0, t1, rendered=None):
    """ガイドの平らな区間（ガイドの音程ノート）ごとの、中央部の音程の差（`guide_contour`）の要約。
    ノートの切れ方に左右されない（細かく切れたノート・遷移の途中のノートが残差の大半になることが無い）。"""
    from . import guide_contour as GC
    lo, hi = tm.at(t0, "right"), tm.at(t1, "left")
    segs = GC.guide_segments(project, lo, hi)
    if not segs:
        return {"n": 0}
    ident = (np.array([0.0, 1e9]), np.array([0.0, 1e9]))
    cur = (tm.src, tm.out)
    vals = {"before": GC.residuals(segs, ident, *GC.original_midi(project)),
            "edited": GC.residuals(segs, cur, *GC.edited_midi(project))}
    if rendered is not None:
        rt, rf = rendered
        rm = GC._midi(np.where(np.asarray(rf.voiced, dtype=bool), rf.f0, 0.0))
        vals["after"] = GC.residuals(segs, ident, rt, rm)
    out = {"n": len(segs), "note": "ガイドの平らな区間ごとの、中央部の音程の差（セント）。correct_to_guide(pitch_mode='contour') と同じ物差し"}
    for k, v in vals.items():
        out[k] = dict(_abs_stats(v), within_10=_within(v, 10.0), within_25=_within(v, 25.0))
    return out


def measure(project, start_sec=None, end_sec=None, note_ids=None, render=True, backend=None,
            renderer=None):
    """ノートごとの残差。返り値 {rows, summary, onsets, render}（rows は時刻の順）。"""
    from .pitch import edited_note_centers
    if project.guide is None or project.alignment is None:
        raise TM.TimingError("ガイドが無い（ガイドのトラックを指定して analyze_take）")
    t0 = 0.0 if start_sec is None else float(start_sec)
    t1 = float(project.duration_sec) if end_sec is None else float(end_sec)
    notes = [n for n in TM.pitched_notes(project) if n.end_sec > t0 and n.start_sec < t1]
    if note_ids:
        want = set(note_ids)
        notes = [n for n in notes if n.id in want] if (start_sec is not None or end_sec is not None) \
            else [n for n in TM.pitched_notes(project) if n.id in want]
        if notes:
            t0 = min(t0 if start_sec is not None else 1e18, min(n.start_sec for n in notes))
            t1 = max(t1 if end_sec is not None else -1e18, max(n.end_sec for n in notes))
    _, pitch_target, _ = TM.note_correspondence(project)
    gt = guide_timing(project)
    tm = TM.current_map(project)
    ons = onset_rows(project, t0 - TM.HEAD_SNAP_SEC, t1, gt=gt, tm=tm)
    edited_center = edited_note_centers(project, notes)
    take = project.take_f0
    guide = project.guide_f0
    times = np.asarray(take.times, dtype="float64")

    rf0 = rons = None
    rinfo = None
    if render and notes:
        a = max(0.0, tm.at(t0, "right") - RENDER_PAD_SEC)
        b = min(float(project.duration_sec), tm.at(t1, "left") + RENDER_PAD_SEC)
        rf0, rons, rinfo = _render_measure(project, a, b, backend, renderer)
        rtimes = np.asarray(rf0.times, dtype="float64") + a

    # ガイドの F0 をテイクの時間（タイムライン上のガイドの位置）に置く（音程の形の残差用）
    gpos = gmidi = None
    if guide is not None:
        gtimes = np.asarray(guide.times, dtype="float64")
        gpos = np.asarray(gt.to_take(gtimes), dtype="float64") if gt is not None else gtimes
        gf = np.asarray(guide.f0, dtype="float64")
        gv = np.asarray(guide.voiced, dtype=bool) & (gf > 0)
        gmidi = np.where(gv, 69.0 + 12.0 * np.log2(np.where(gf > 0, gf, 1.0) / 440.0), np.nan)

    rows = []
    used = set()
    for n in notes:
        g = pitch_target.get(n.id)
        gm = None if g is None or g.pitch_midi is None else float(g.pitch_midi)
        b_mid, b_n = _median_midi(take.f0, take.voiced, times, n.start_sec, n.end_sec)
        oa, ob = tm.at(n.start_sec, "right"), tm.at(n.end_sec, "left")
        row = {"note_id": n.id, "start_sec": round(n.start_sec, 4), "end_sec": round(n.end_sec, 4),
               "edited_start_sec": round(oa, 4), "edited_end_sec": round(ob, 4),
               "guide": None if g is None else g.id,
               "guide_note": None if g is None else g.note_name,
               "pitch_cents": {"before": None, "edited": None, "after": None},
               "timing_ms": {"before": None, "edited": None, "after": None},
               "onset_paired": False, "confidence": round(float(n.confidence or 0.0), 3)}
        if gm is not None:
            if b_mid is not None:
                row["pitch_cents"]["before"] = _r((b_mid - gm) * 100.0)
            ec = edited_center.get(n.id)
            if ec is not None:
                row["pitch_cents"]["edited"] = _r((ec - gm) * 100.0)
            if rf0 is not None:
                a_mid, _ = _median_midi(rf0.f0, rf0.voiced, rtimes, oa, ob)
                if a_mid is not None:
                    row["pitch_cents"]["after"] = _r((a_mid - gm) * 100.0)
                # 形の残差: 編集後の音のフレームごとに、その時刻のガイドの音程との差
                if gpos is not None:
                    m = (rtimes >= oa) & (rtimes < ob) & np.asarray(rf0.voiced, dtype=bool) & (rf0.f0 > 0)
                    if int(m.sum()) >= MIN_VOICED:
                        tm_ = rtimes[m]
                        am = 69.0 + 12.0 * np.log2(np.asarray(rf0.f0)[m] / 440.0)
                        gi = np.interp(tm_, gpos, gmidi, left=np.nan, right=np.nan)
                        ok = np.isfinite(gi)
                        if int(ok.sum()) >= MIN_VOICED:
                            row["shape_abs_cents_median"] = _r(np.median(np.abs(am[ok] - gi[ok])) * 100.0)
        else:
            row["reason"] = "音程で対応するガイドのノートが無い（時間の重なるガイドのノートが無い・短い遷移）"
        # 発音の頭: ノートの頭の近く（頭の HEAD_SNAP 前〜尻）にある最初の組
        for k, o in enumerate(ons):
            if k in used:
                continue
            if n.start_sec - TM.HEAD_SNAP_SEC <= o["take_sec"] < n.end_sec:
                used.add(k)
                row["onset_paired"] = True
                row["onset_take_sec"] = o["take_sec"]
                row["onset_target_sec"] = o["target_sec"]
                row["timing_ms"]["before"] = o["before_ms"]
                row["timing_ms"]["edited"] = o["edited_ms"]
                if rons is not None and len(rons):
                    j = int(np.argmin(np.abs(rons - o["edited_sec"])))
                    if abs(rons[j] - o["edited_sec"]) <= ONSET_MATCH_SEC:
                        row["timing_ms"]["after"] = _r((rons[j] - o["target_sec"]) * 1000.0)
                        o["after_ms"] = row["timing_ms"]["after"]
                break
        if g is not None and not row["onset_paired"]:
            row["timing_reason"] = "このノートの頭に、ガイドと 1 対 1 に対応する発音の頭が無い（タイミングは測らない）"
        rows.append(row)

    def col(key, which):
        return [r[key][which] for r in rows]

    summary = {"notes": len(rows),
               "matched_notes": sum(1 for r in rows if r["guide"] is not None),
               "onset_paired_notes": sum(1 for r in rows if r["onset_paired"]),
               "pitch_cents": {w: _abs_stats(col("pitch_cents", w)) for w in ("before", "edited", "after")},
               "timing_ms": {w: _abs_stats(col("timing_ms", w)) for w in ("before", "edited", "after")}}
    summary["guide_segments"] = _segment_summary(project, tm, t0, t1,
                                                 None if rf0 is None else (rtimes, rf0))
    out = {"range_sec": [round(t0, 4), round(t1, 4)], "rows": rows, "summary": summary,
           "onsets": onset_summary(ons)}
    if rinfo is not None:
        out["render"] = {"backend": rinfo.get("backend"), "range_sec": [round(a, 3), round(b, 3)]}
    return out
