# -*- coding: utf-8 -*-
"""「ガイドに合わせる」の音程の**区間カーブ**（`correct_to_guide(pitch_mode="contour")`）。

ノートの対応（`timing.note_correspondence`）は確かな発音の頭の組で裏付けられた所にしか効かず、
音のつながったハモリ（legato）では約半分のノートしか動かなかった。テイクのノートが細かく切れる・
1 つのテイクのノートに高さの違うガイドのノートが 2 つ以上重なる所でも、対応がずれる。

区間カーブはノートの対応を使わない:

1. **ガイドの平らな区間**（ガイドの音程ノート）を、タイムライン上のガイドの位置（`guide_to_take`）に置く。
2. 区間の中央部（両端 `TRIM` を除く）で、ガイドの音程の中央値と、同じ時刻に鳴るテイクの**今の（編集後の）音程**
   （鉛筆・なだらかさ込み。タイミングの編集は時間の写像で戻す）の中央値の差を測る。
3. 差を打ち消す量（× 強度）を区間ごとに一定にし、隣の区間との間を `RAMP_SEC` 以上かけて直線でつないだ
   **ずらし量カーブ**を、フレーズ（区間の間が `PHRASE_GAP_SEC` 以下で続く所）ごとに 1 本の `pitch_curve`
   （範囲の編集）にする。両端は 0 からなだらかに入るので、範囲の端に段差（自動のなだらかさの対象）を作らない。

ビブラート・しゃくりなど区間の中の揺れは残る（区間ごとに一定量をずらすだけ）。
テイクが区間の半分も歌っていない区間、差が `max_shift_cents` を超える区間（別の音・オクターブ違い）は 0 のまま。
同じ範囲に前からある `pitch_curve` は後勝ちで上書きされるので、その値をカーブに足し込んで残す。
"""
from __future__ import annotations

import numpy as np

from .model import Target

TRIM = 0.2                 # 区間の両端のこの割合は測らない（音程の移り変わり）
MIN_SEG_SEC = 0.06         # これより短いガイドの区間は使わない
MIN_COVER = 0.5            # 中央部のうちテイクが有声のフレームの割合の下限
MIN_FRAMES = 3
PHRASE_GAP_SEC = 0.25      # 区間の間がこれを超えたら別のフレーズ（カーブを 0 に戻す）
RAMP_SEC = 0.05            # 隣の区間との間をつなぐ長さの下限
EDGE_PAD_SEC = 0.06        # フレーズの頭・尻の外へ一定量を延ばす長さ（テイクが早く・遅く歌っている分）
EDGE_RAMP_SEC = 0.02       # そこから 0 へ戻す長さ
GRID_SEC = 0.01            # 前からある曲線を足し込むときの点の間隔
DEFAULT_MAX_SHIFT = 600.0      # 半オクターブ。オクターブ違いの F0・別の音域のテイクを寄せない


def _midi(hz):
    hz = np.asarray(hz, dtype="float64")
    out = np.full(hz.shape, np.nan)
    v = hz > 0
    out[v] = 69.0 + 12.0 * np.log2(hz[v] / 440.0)
    return out


def edited_midi(project):
    """(テイクのフレームの秒, 今の編集後の音程の MIDI（無声は NaN）)。なだらかさ・鉛筆込み。"""
    from .pitch import pitch_model
    f0r = project.take_f0
    t = np.asarray(f0r.times, dtype="float64")
    m = _midi(np.where(np.asarray(f0r.voiced, dtype=bool), f0r.f0, 0.0))
    _, _, lay, _ = pitch_model(project)
    off = np.array([lay.at(float(x), "right") if np.isfinite(v) else 0.0 for x, v in zip(t, m)])
    return t, m + off / 100.0


def original_midi(project):
    f0r = project.take_f0
    t = np.asarray(f0r.times, dtype="float64")
    return t, _midi(np.where(np.asarray(f0r.voiced, dtype=bool), f0r.f0, 0.0))


def guide_segments(project, out_lo=None, out_hi=None):
    """ガイドの平らな区間をタイムライン（編集後の時間）に置いたもの。

    [{id, note, a, b（区間）, ca, cb（中央部）, center（ガイドの中央部の音程の中央値 MIDI）}]"""
    if project.guide is None or project.alignment is None:
        return []
    g2t, _ = project.guide_to_take()
    gf = project.guide_f0
    gt = np.asarray(gf.times, dtype="float64")
    gm = _midi(np.where(np.asarray(gf.voiced, dtype=bool), gf.f0, 0.0))
    out = []
    for g in project.guide_notes:
        if g.kind != "note" or g.end_sec - g.start_sec < MIN_SEG_SEC:
            continue
        a, b = float(g2t(g.start_sec)), float(g2t(g.end_sec))
        if b - a < MIN_SEG_SEC:
            continue
        if out_lo is not None and b <= out_lo:
            continue
        if out_hi is not None and a >= out_hi:
            continue
        w = g.end_sec - g.start_sec
        ga, gb = g.start_sec + TRIM * w, g.end_sec - TRIM * w
        sel = gm[(gt >= ga) & (gt < gb)]
        sel = sel[np.isfinite(sel)]
        center = float(np.median(sel)) if len(sel) >= MIN_FRAMES else (
            None if g.pitch_midi is None else float(g.pitch_midi))
        if center is None:
            continue
        out.append({"id": g.id, "note": g.note_name, "a": a, "b": b,
                    "ca": a + TRIM * (b - a), "cb": b - TRIM * (b - a), "center": center})
    out.sort(key=lambda s: s["a"])
    return out


def _to_src(tmap, t):
    """編集後の秒 → 編集前の秒（時間の写像の逆。無音の挿入の所は手前の秒）。"""
    src, out = tmap
    return float(np.interp(t, out, src))


def take_center(seg, tmap, times, midi):
    """区間の中央部に鳴るテイクの音程の中央値（MIDI）と、有声の割合。"""
    sa, sb = _to_src(tmap, seg["ca"]), _to_src(tmap, seg["cb"])
    m = (times >= sa) & (times < sb)
    n = int(m.sum())
    if n < MIN_FRAMES:
        return None, 0.0
    v = midi[m]
    v = v[np.isfinite(v)]
    cover = len(v) / n
    if len(v) < MIN_FRAMES:
        return None, cover
    return float(np.median(v)), cover


def residuals(segs, tmap, times, midi):
    """区間ごとの (テイク − ガイド) のセント（測れない区間は None）。"""
    out = []
    for s in segs:
        c, cover = take_center(s, tmap, times, midi)
        out.append(None if c is None or cover < MIN_COVER else (c - s["center"]) * 100.0)
    return out


def _old_curve(project):
    """前からある pitch_curve の、時刻 → 曲線の値（セント。shift_pitch は含まない）。無ければ None。"""
    from ..render.pipeline import edits_to_segments
    from .pitch import _SegIndex, seg_value
    eds = [e for e in project.edits if e.kind == "pitch_curve"]
    if not eds:
        return None
    idx = _SegIndex(edits_to_segments(eds, project.edit_span))

    def at(t):
        s = idx.seg_at(t, "right")
        return 0.0 if s is None or not s.curve_points else seg_value(s, t) - float(s.cents)
    return idx, at


def contour_specs(project, tmap, out_lo, out_hi, strength=1.0, threshold_cents=0.0,
                  max_shift_cents=DEFAULT_MAX_SHIFT, src_spans=None):
    """区間カーブの編集（pitch_curve の spec の列）と info。

    tmap = (src, out) 当てた後の時間の写像（タイミングも一緒に当てるなら、その後のもの）。
    [out_lo, out_hi] = 対象の範囲（編集後の秒）。src_spans = ノートで選んだときの、その編集前の区間。"""
    segs = guide_segments(project, out_lo, out_hi)
    times, midi = edited_midi(project)
    res = residuals(segs, tmap, times, midi)
    rows, skipped = [], []
    for s, r in zip(segs, res):
        sa, sb = _to_src(tmap, s["a"]), _to_src(tmap, s["b"])
        if src_spans is not None and not any(b > sa and a < sb for a, b in src_spans):
            continue
        v = 0.0
        if r is None:
            skipped.append({"guide": s["id"], "reason": "テイクがこの区間をほとんど歌っていない（有声が半分未満）"})
        elif abs(r) > max_shift_cents:
            skipped.append({"guide": s["id"], "cents": round(r, 1),
                            "reason": "ずれが %.0f セントを超える（別の音・オクターブ違いの可能性）" % max_shift_cents})
        elif abs(r) >= threshold_cents:
            v = -float(strength) * r
        rows.append({"seg": s, "sa": sa, "sb": sb, "v": v, "r": r})
    # フレーズに分ける（区間の間が空いた所で切る）
    phrases = []
    for row in rows:
        if phrases and row["seg"]["a"] - phrases[-1][-1]["seg"]["b"] <= PHRASE_GAP_SEC:
            phrases[-1].append(row)
        else:
            phrases.append([row])
    old = _old_curve(project)
    dur = float(project.duration_sec)
    specs = []
    moved = 0
    for ph in phrases:
        if not any(abs(r["v"]) > 1e-6 for r in ph):
            continue
        pts = []
        first, last = ph[0], ph[-1]
        a0 = max(0.0, first["sa"] - EDGE_PAD_SEC)
        pts.append((max(0.0, a0 - EDGE_RAMP_SEC), 0.0))
        pts.append((a0, first["v"]))
        for r0, r1 in zip(ph[:-1], ph[1:]):
            c = 0.5 * (r0["sb"] + r1["sa"])
            h = max(0.5 * RAMP_SEC, 0.5 * (r1["sa"] - r0["sb"]))
            # つなぎは各区間の中へ長さの 4 割までしか入れない（中央部の一定量を保つ）
            h = max(0.005, min(h, c - r0["sb"] + 0.4 * (r0["sb"] - r0["sa"]),
                               r1["sa"] - c + 0.4 * (r1["sb"] - r1["sa"])))
            pts.append((c - h, r0["v"]))
            pts.append((c + h, r1["v"]))
        b1 = min(dur, last["sb"] + EDGE_PAD_SEC)
        pts.append((b1, last["v"]))
        pts.append((min(dur, b1 + EDGE_RAMP_SEC), 0.0))
        # 時刻を単調に（重なる区間で前後した点は手前の点の直後へ）
        clean = []
        for t, v in pts:
            if clean and t <= clean[-1][0] + 1e-4:
                t = clean[-1][0] + 1e-4
            clean.append((t, v))
        P0, P1 = clean[0][0], clean[-1][0]
        if P1 - P0 < 0.02:
            continue
        if old is not None and any(abs(old[1](float(t))) > 1e-9
                                   for t in np.arange(P0, P1 + GRID_SEC, GRID_SEC)):
            # 同じ範囲の前の曲線は後勝ちで消えるので、その値を足し込む
            tt = np.unique(np.concatenate([np.arange(P0, P1, GRID_SEC), [P1],
                                           [t for t, _ in clean]]))
            vv = np.interp(tt, [t for t, _ in clean], [v for _, v in clean])
            vv = vv + np.array([old[1](float(t)) for t in tt])
            clean = list(zip(tt.tolist(), vv.tolist()))
        specs.append({"kind": "pitch_curve", "target": Target.range(round(P0, 6), round(P1, 6)),
                      "params": {"points": [[round(t - P0, 6), round(v, 3)] for t, v in clean]},
                      "note": "ガイドの区間カーブ %.0f%%" % (float(strength) * 100)})
        moved += sum(1 for r in ph if abs(r["v"]) > 1e-6)
    measured = [r["r"] for r in rows if r["r"] is not None]
    info = {"guide_segments": len(rows), "moved_segments": moved, "phrases": len(specs),
            "skipped_segments": skipped,
            "abs_cents_median_before": (round(float(np.median(np.abs(measured))), 1) if measured else None)}
    return specs, info


def apply_contour(project, plan, x, strength, out_range, threshold_cents=0.0,
                  max_shift_cents=DEFAULT_MAX_SHIFT, src_spans=None, author="ai", label=None):
    """タイミング（計画を x で）と区間カーブの音程を **1 つの changeset** で当てる。(changeset, info)"""
    from ..render.pipeline import edits_to_segments
    from ..view.export_data import build_time_map
    from . import timing as TM
    from .model import Edit
    removes, tspecs, info = ([], [], {"x": 0.0})
    if x is not None and abs(float(x)) > 0:
        removes, tspecs, info = TM.realize(project, plan, x)
    sim = [e for e in project.edits if e.id not in set(removes)]
    for i, s in enumerate(tspecs):
        tgt = s["target"] if isinstance(s["target"], Target) else Target.from_json(s["target"])
        sim.append(Edit(id="sim%04d" % i, kind=s["kind"], target=tgt, params=dict(s["params"])))
    src, out = build_time_map(edits_to_segments(sim, project.edit_span), 0.0,
                              max(float(project.duration_sec), 1e-6))
    tmap = (np.asarray(src, dtype="float64"), np.asarray(out, dtype="float64"))
    lo, hi = out_range
    lo = float(np.interp(lo, tmap[0], tmap[1]))
    hi = float(np.interp(hi, tmap[0], tmap[1]))
    cspecs, cinfo = ([], {}) if not strength else contour_specs(
        project, tmap, lo, hi, strength=strength, threshold_cents=threshold_cents,
        max_shift_cents=max_shift_cents, src_spans=src_spans)
    info.update(contour=cinfo)
    specs = list(tspecs) + cspecs
    if not specs and not removes:
        return None, info
    cs = project.apply_changes(removes, specs, author=author, label=label, origin="auto")
    info["changeset"] = cs.id
    return cs, info
