# -*- coding: utf-8 -*-
"""`render_view` — 波形・テイク F0・ガイド F0・ノートの帯を 1 枚の PNG にする。

- matplotlib の Agg（画面なし）。**MCP の結果に画像を入れない**（研究 01 §1）ので、
  ファイルに書いてパスだけ返す。
- **毎回ユニークなファイル名**にする（同名で上書きすると、エージェントが過去に見た図まで
  差し替わってしまう。研究 01 §2）。
- 歌詞・音素はまだ無いので、横軸は時刻だけ。
"""
import os
import time
import uuid

import numpy as np

from ..analysis.f0 import hz_to_midi, midi_to_name

KIND_COLOR = {
    "note": "#3b6ea5",
    "unvoiced": "#b0b0b0",
    "breath": "#cbb26a",
    "silence": "#e8e8e8",
}


_FONT_CANDIDATES = ["Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP",
                    "Hiragino Sans", "DejaVu Sans"]


def _setup():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    # 日本語が豆腐にならないように、入っている和文フォントを先頭に据える
    have = {f.name for f in font_manager.fontManager.ttflist}
    fonts = [f for f in _FONT_CANDIDATES if f in have] or ["DejaVu Sans"]
    plt.rcParams["font.family"] = fonts
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def render_view(project, start_sec=None, end_sec=None, out_dir=None, width=1400, height=700,
                show_guide=True, show_edits=True, title=None, dpi=100):
    """PNG を書いてパスを返す。"""
    plt = _setup()
    project.ensure_analyzed()
    dur = project.duration_sec
    t0 = 0.0 if start_sec is None else max(0.0, float(start_sec))
    t1 = dur if end_sec is None else min(dur, float(end_sec))
    if t1 <= t0:
        t1 = min(dur, t0 + 1.0)

    x, sr = project.audio("take")
    f0r = project.take_f0
    notes = project.take_notes
    guide_f0 = project.guide_f0 if (show_guide and project.guide) else None
    al = project.alignment if (show_guide and project.guide) else None

    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    gs = fig.add_gridspec(3, 1, height_ratios=[1.0, 3.0, 0.45], hspace=0.12)
    ax_w = fig.add_subplot(gs[0])
    ax_p = fig.add_subplot(gs[1], sharex=ax_w)
    ax_n = fig.add_subplot(gs[2], sharex=ax_w)

    # ---- 波形
    a, b = int(t0 * sr), int(min(len(x), t1 * sr))
    seg = x[a:b]
    step = max(1, len(seg) // 4000)
    tt = (np.arange(a, b, step)) / sr
    ax_w.plot(tt, seg[::step], lw=0.5, color="#4b4b4b")
    ax_w.set_ylabel("波形", fontsize=9)
    ax_w.set_ylim(-1.05 * max(1e-3, float(np.max(np.abs(seg)) if seg.size else 1e-3)),
                  1.05 * max(1e-3, float(np.max(np.abs(seg)) if seg.size else 1e-3)))
    ax_w.tick_params(labelbottom=False, labelsize=8)
    ax_w.grid(alpha=0.2)

    # ---- F0（半音軸）
    ft = f0r.times
    m = hz_to_midi(f0r.f0)
    sel = (ft >= t0) & (ft <= t1)
    ax_p.plot(ft[sel], np.where(f0r.voiced[sel], m[sel], np.nan), lw=1.8,
              color="#1f4e8c", label="テイク F0")
    if guide_f0 is not None and al is not None:
        gm = hz_to_midi(guide_f0.f0)
        g2t, basis = project.guide_to_take()
        gt_take = np.asarray(g2t(guide_f0.times))   # ガイドの時刻 + 全体のずれ（組が無ければ DTW）
        gsel = (gt_take >= t0) & (gt_take <= t1)
        ax_p.plot(gt_take[gsel], np.where(guide_f0.voiced[gsel], gm[gsel], np.nan),
                  lw=1.4, color="#c0392b", alpha=0.85, label=("ガイド F0（ガイドの時刻 + 全体のずれ）" if basis["basis"] != "dtw"
                         else "ガイド F0（DTW でテイク時間に写像）"))

    # ---- ノートの帯
    vis = [n for n in notes if n.end_sec > t0 and n.start_sec < t1]
    for n in vis:
        s, e = max(t0, n.start_sec), min(t1, n.end_sec)
        if n.kind == "note" and n.pitch_midi is not None:
            ax_p.add_patch(plt.Rectangle((s, n.pitch_midi - 0.4), e - s, 0.8,
                                         color=KIND_COLOR["note"], alpha=0.22, lw=0))
            ax_p.text(s, n.pitch_midi + 0.55, "%s %s" % (n.id, n.note_name or ""),
                      fontsize=6.5, color="#1f4e8c")
        ax_n.add_patch(plt.Rectangle((s, 0.15), e - s, 0.7,
                                     color=KIND_COLOR.get(n.kind, "#999999"), alpha=0.75, lw=0))
        if e - s > (t1 - t0) * 0.02:
            ax_n.text((s + e) / 2, 0.5, n.id, fontsize=6, ha="center", va="center", color="white")

    # ---- 編集済み区間
    if show_edits and project.edits:
        for ed in project.edits:
            try:
                s, e = project.edit_span(ed)
            except Exception:
                continue
            if e <= t0 or s >= t1:
                continue
            ax_p.axvspan(max(t0, s), min(t1, e), color="#f2a516", alpha=0.13, lw=0)

    mv = m[sel][f0r.voiced[sel]] if sel.any() else np.array([])
    if mv.size:
        lo, hi = float(np.nanmin(mv)) - 3, float(np.nanmax(mv)) + 3
    else:
        lo, hi = 48, 72
    ax_p.set_ylim(lo, hi)
    ticks = np.arange(int(np.ceil(lo)), int(np.floor(hi)) + 1)
    ticks = ticks[(ticks % 12 == 0) | (ticks % 12 == 7)] if len(ticks) > 24 else ticks
    ax_p.set_yticks(ticks)
    ax_p.set_yticklabels([midi_to_name(t) for t in ticks], fontsize=7)
    ax_p.set_ylabel("音程", fontsize=9)
    ax_p.grid(alpha=0.25)
    ax_p.tick_params(labelbottom=False, labelsize=8)
    leg = ax_p.legend(loc="upper right", fontsize=7, framealpha=0.85)
    leg.get_frame().set_linewidth(0.3)

    ax_n.set_ylim(0, 1)
    ax_n.set_yticks([])
    ax_n.set_xlabel("時刻（秒）", fontsize=9)
    ax_n.tick_params(labelsize=8)
    ax_n.set_xlim(t0, t1)

    head = title or "%s  %.2f–%.2f s" % (os.path.basename(project.take["path"]), t0, t1)
    if project.guide:
        head += "  /  ガイド: %s" % os.path.basename(project.guide["path"])
    head += "  /  編集 %d 件" % len(project.edits)
    fig.suptitle(head, fontsize=10)

    out_dir = out_dir or project.sub("views")
    name = "view-%s-%s.png" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    path = os.path.join(out_dir, name)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return {
        "path": os.path.abspath(path),
        "range_sec": [round(t0, 3), round(t1, 3)],
        "notes_shown": len(vis),
        "guide_overlaid": bool(guide_f0 is not None),
        "edits_marked": len(project.edits) if show_edits else 0,
        "hint": "画像は MCP の結果に入れていない。Read（または view_image）でこのパスを開くこと。",
    }
