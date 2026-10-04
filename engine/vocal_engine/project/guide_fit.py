# -*- coding: utf-8 -*-
"""「ガイドに合わせる」の計画を当てる前と後の、ガイドとの当てはまり（補正が悪化させた所を見つける）。

計画が合わせる物差し（発音の頭の組・音程の対応）で測ると、組を取り違えた所も「合った」と出る（密なラップで
隣の音節と組んだ・1 対多の組で別の音へ寄せた。補正の担当の報告）。そこで、計画と別の物差しで前後を比べる:

- **タイミング**: フレーズ（`timing.PHRASE_GAP_SEC` の隙間で分ける）ごとに、テイクの発音の強さの包絡（onset strength。
  30 ms でならす）とガイドの包絡の相関。後は、計画を当てた後の時間写像でテイクの包絡を移して測る（音は作らない）
- **音程**: ノートごとに、テイクの F0 とガイドの F0（画面に描くガイドの位置で引いたフレーム）の差の絶対値の中央値（セント）。
  後は、計画の音程（ノートの一定量か、鉛筆の線）を当てた値で測る

どちらも予測（再合成した音は測らない。確かめるのは `remeasure`）。悪化した所（相関が `WORSE_CORR` 以上下がった
フレーズ・残差が `WORSE_CENTS` 以上増えたノート）を一覧で返す。
"""
import numpy as np

SMOOTH_SEC = 0.03
WORSE_CORR = 0.05
WORSE_CENTS = 30.0
PAD_SEC = 0.1


def _envelope(project, role):
    """発音の強さの包絡（10 ms）。プロジェクトに覚えておく（音声の鍵が変われば取り直す）。"""
    from ..analysis.align import ENV_HOP, MFCC_SR, _envelopes
    info = project.take if role == "take" else project.guide
    key = (info["path"], int(info.get("offset_frames") or 0), int(info["frames"]))
    memo = getattr(project, "_fit_env", None) or {}
    if memo.get(role, (None,))[0] != key:
        x, sr = project.audio(role)
        on, _ = _envelopes(x, sr)
        k = max(1, int(round(SMOOTH_SEC * MFCC_SR / ENV_HOP)))
        on = np.convolve(on, np.ones(k) / k, mode="same")
        memo[role] = (key, on)
        project._fit_env = memo
    return memo[role][1], ENV_HOP / float(MFCC_SR)


def _corr(a, b):
    if len(a) < 5 or np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _after_map(plan, x, src_before, out_before):
    """編集前の秒 → 計画を強さ x で当てた後の秒（節の間は線形。節の外は今の位置）。"""
    ks = plan.st.knots
    if not ks or not x:
        return lambda s: np.interp(s, src_before, out_before)
    order = np.argsort([k.src for k in ks], kind="stable")
    ksrc = np.array([ks[i].src for i in order])
    kout = np.array([ks[i].cur + plan.d[i] * float(x) for i in order])
    a, b = ksrc[0], ksrc[-1]

    def f(s):
        s = np.asarray(s, dtype="float64")
        out = np.interp(s, src_before, out_before)
        m = (s >= a) & (s <= b)
        out[m] = np.interp(s[m], ksrc, kout)
        return np.maximum.accumulate(out) if out.ndim else out
    return f


def plan_fit(project, plan, timing=1.0, pitch=1.0, note_ids=None):
    """計画（`timing.plan_guide`）を timing / pitch の強さで当てる前と後の当てはまり（予測）。"""
    from . import timing as TM
    tn = {n.id: n for n in TM.pitched_notes(project)}
    sel = [tn[i] for i in (note_ids or tn) if i in tn]
    if not sel or project.guide is None:
        return None
    sel.sort(key=lambda n: n.start_sec)
    g2t_inv = TM._guide_inverse(project)
    out = {"timing": None, "pitch": None}
    # ---- タイミング: フレーズごとの包絡の相関
    if timing:
        te, hop = _envelope(project, "take")
        ge, _ = _envelope(project, "guide")
        src_b, out_b = project.time_map()
        after = _after_map(plan, timing, np.asarray(src_b), np.asarray(out_b))
        src_grid = np.arange(len(te)) * hop
        ob = np.maximum.accumulate(np.interp(src_grid, src_b, out_b))
        oa = after(src_grid)
        phrases, cur = [], [sel[0]]
        for n in sel[1:]:
            if n.start_sec - cur[-1].end_sec >= TM.PHRASE_GAP_SEC:
                phrases.append(cur)
                cur = [n]
            else:
                cur.append(n)
        phrases.append(cur)
        rows, wsum, sb, sa = [], 0.0, 0.0, 0.0
        for ph in phrases:
            a = float(np.interp(ph[0].start_sec, src_grid, ob)) - PAD_SEC
            b = float(np.interp(ph[-1].end_sec, src_grid, ob)) + PAD_SEC
            t = np.arange(max(0.0, a), b, hop)
            gi = np.clip(np.round(np.asarray(g2t_inv(t)) / hop).astype(int), 0, len(ge) - 1)
            g = ge[gi]
            cb = _corr(np.interp(t, ob, te), g)
            ca = _corr(np.interp(t, oa, te), g)
            if cb is None or ca is None:
                continue
            w = b - a
            wsum += w
            sb += cb * w
            sa += ca * w
            rows.append({"start_sec": round(ph[0].start_sec, 3), "end_sec": round(ph[-1].end_sec, 3),
                         "notes": len(ph), "before": round(cb, 3), "after": round(ca, 3)})
        if rows:
            out["timing"] = {"phrases": len(rows), "corr_before": round(sb / wsum, 3),
                             "corr_after": round(sa / wsum, 3),
                             "worse": [r for r in rows if r["after"] < r["before"] - WORSE_CORR]}
    # ---- 音程: ノートごとのフレームの残差
    if pitch:
        from ..view.export_data import current_note_pitches
        tf, gf = project.take_f0, project.guide_f0
        curp = current_note_pitches(project)
        curve = {}
        for t, h0, h1, w in plan.pitch_curve:
            curve.setdefault(round(t, 6), (h0, h1, w))
        ct = np.array(sorted(curve)) if curve else np.array([])
        rows = []
        for n in sel:
            a, b = int(round(n.start_sec / tf.hop_s)), int(round(n.end_sec / tf.hop_s))
            t = np.arange(a, b) * tf.hop_s
            f = np.asarray(tf.f0, dtype="float64")[a:b]
            v = np.asarray(tf.voiced)[a:b].astype(bool) & (f > 0)
            gi = np.clip(np.round(np.asarray(g2t_inv(t)) / gf.hop_s).astype(int), 0, len(gf.f0) - 1)
            g = np.asarray(gf.f0, dtype="float64")[gi]
            ok = v & np.asarray(gf.voiced).astype(bool)[gi] & (g > 0)
            if ok.sum() < 3 or n.pitch_midi is None:
                continue
            gm = 69.0 + 12.0 * np.log2(g[ok] / 440.0)
            tm = 69.0 + 12.0 * np.log2(f[ok] / 440.0) + (curp.get(n.id, n.pitch_midi) - n.pitch_midi)
            before = np.abs(tm - gm) * 100.0
            am = tm.copy()
            if plan.pitch_draws:
                tt = t[ok]
                if len(ct):
                    k = np.clip(np.searchsorted(ct, tt), 0, len(ct) - 1)
                    near = np.abs(ct[k] - tt) <= tf.hop_s * 0.6
                    for i in np.nonzero(near)[0]:
                        h0, h1, w = curve[ct[k[i]]]
                        am[i] = TM._guide_blend_midi(h0, h1, w, float(pitch))
            elif n.id in plan.pitch:
                am = tm + plan.pitch[n.id] * float(pitch) / 100.0
            after = np.abs(am - gm) * 100.0
            rows.append({"note": n.id, "start_sec": round(n.start_sec, 3),
                         "before_cents": round(float(np.median(before)), 1),
                         "after_cents": round(float(np.median(after)), 1)})
        if rows:
            out["pitch"] = {"notes": len(rows),
                            "abs_cents_median_before": round(float(np.median([r["before_cents"] for r in rows])), 1),
                            "abs_cents_median_after": round(float(np.median([r["after_cents"] for r in rows])), 1),
                            "worse": [r for r in rows if r["after_cents"] > r["before_cents"] + WORSE_CENTS]}
    return out
