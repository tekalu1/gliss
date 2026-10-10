# -*- coding: utf-8 -*-
"""ノートのフェードイン／アウト（issue #20。`proposal/v3.html` §5）。

DAW のクリップフェードと同じく、ノートの頭（イン）・尻（アウト）から内側へ、**音量だけ**を 0 から／0 へ
なだらかに変える。ピッチ（なだらかさ = 境目のピッチのつなぎ）とは別。隣のノートは変えない。

## 編集リスト

| kind | target | params | 意味 |
|---|---|---|---|
| `fade` | 範囲（ノートの頭〜尻。編集前の秒） | `side`（"in" / "out"）, `sec`（編集後の秒） | ノートの頭（in）か尻（out）から `sec` 秒のフェード |

対象は音程のあるノート・子音・息（`FADE_KINDS`。2026-10-10 承認。子音・息を音程のあるノートと揃える）。無音は区間ではない。

- **錨はノートの端の時刻**（in は target の頭、out は尻）。同じ錨・同じ向きのものは後勝ち。分割するとイン は左の片、
  アウトは右の片に残る（端の時刻が同じなので）。結合で消えた境目の錨はどのノートの端にも当たらないので効かない
  （結合を取り消すと戻る）。
- 長さ `sec` は**編集後の秒**（聞こえる長さ）。ノートの長さを変えてもフェードの秒は保つ。ノートより長くはならない
  （イン＋アウトがノートの長さを超えたら、比を保って縮める）。
- 形は 1 種類（等パワー: イン sin、アウト cos）。

## 再合成

`fade_segments` が、フェードの区間ごとに**印の Segment**（`Segment.fade = (編集後の頭, 尻, "in"/"out")`）を返す。
`layered_segments` がこれを末尾に足すので、書き出し・再生の窓（`render/export.py: _windows`）にフェードの区間が入り、
`Renderer.render_range` は印を再合成には使わず、つないだ後の出力に音量の包絡を掛ける（出力の秒 = 編集後の秒）。
フェードの外の音は変わらない（窓の中でも、再合成しない区間は元のサンプルそのまま）。
"""
import numpy as np

MATCH_TOL_SEC = 5e-4           # 錨とノートの端を同じとみなす差
SIDES = ("in", "out")
FADE_KINDS = ("note", "unvoiced", "breath")   # フェードを付けられる区間（音程のあるノート・子音・息。無音は区間ではない）
MAX_SEC = 30.0


def fade_edits(project):
    return [e for e in project.edits if e.kind == "fade"]


def anchor_of(e):
    """フェードの錨（編集前の秒）: in はノートの頭、out は尻。"""
    return float(e.target.start_sec if e.params.get("side") == "in" else e.target.end_sec)


def _edges(notes):
    starts = {}
    ends = {}
    for n in notes:
        if n.kind not in FADE_KINDS:
            continue
        starts[n.id] = float(n.start_sec)
        ends[n.id] = float(n.end_sec)
    return starts, ends


def _match(t, table):
    best, bd = None, MATCH_TOL_SEC
    for nid, v in table.items():
        d = abs(v - t)
        if d <= bd:
            best, bd = nid, d
    return best


def raw_fades(project, notes=None):
    """{note_id: {"in": (秒, edit), "out": (秒, edit)}}（ノートの端に錨がある最後の編集。長さはまだ縮めない）。"""
    fes = fade_edits(project)
    if not fes:
        return {}
    notes = project.take_notes if notes is None else notes
    starts, ends = _edges(notes)
    out = {}
    for e in fes:                                  # 編集リストの順 = 後勝ち
        side = e.params.get("side")
        nid = _match(anchor_of(e), starts if side == "in" else ends)
        if nid is None:
            continue
        out.setdefault(nid, {})[side] = (float(e.params["sec"]), e)
    return out


def _map(src, out, t, side="right"):
    from ..view.export_data import _map_time
    return float(_map_time(src, out, t, side))


def note_fades(project, notes=None, time_map=None):
    """{note_id: (イン, アウト, [edit id…])}（編集後の秒。0 は無し）。ノートの長さに収めたもの。"""
    raw = raw_fades(project, notes)
    if not raw:
        return {}
    notes = project.take_notes if notes is None else notes
    by = {n.id: n for n in notes}
    src, out = time_map if time_map is not None else project.time_map()
    res = {}
    for nid, d in raw.items():
        n = by.get(nid)
        if n is None:
            continue
        fi = d.get("in", (0.0, None))[0]
        fo = d.get("out", (0.0, None))[0]
        if fi <= 0 and fo <= 0:
            continue
        L = max(0.0, _map(src, out, n.end_sec, "left") - _map(src, out, n.start_sec))
        if fi + fo > L > 0:
            k = L / (fi + fo)
            fi, fo = fi * k, fo * k
        elif L <= 0:
            continue
        ids = [x[1].id for x in d.values() if x[1] is not None]
        res[nid] = (fi, fo, ids)
    return res


def fade_segments(project, base_segs=None):
    """再合成の印の Segment（フェードの区間ごと。`Segment.fade` = (編集後の頭, 尻, 向き)）。"""
    if not fade_edits(project):
        return []
    from ..render.pipeline import Segment
    from ..view.export_data import build_time_map
    if base_segs is None:
        from ..render.pipeline import edits_to_segments
        base_segs = edits_to_segments(project.edits, project.edit_span)
    dur = max(project.duration_sec, 1e-6)
    src, out = build_time_map([s for s in base_segs if getattr(s, "fade", None) is None], 0.0, dur)
    fades = note_fades(project, time_map=(src, out))
    if not fades:
        return []
    by = {n.id: n for n in project.take_notes}
    segs = []
    for nid, (fi, fo, ids) in sorted(fades.items(), key=lambda kv: by[kv[0]].start_sec):
        n = by[nid]
        es = _map(src, out, n.start_sec)
        ee = _map(src, out, n.end_sec, "left")
        for side, ln in (("in", fi), ("out", fo)):
            if ln <= 1e-6:
                continue
            e0, e1 = (es, es + ln) if side == "in" else (ee - ln, ee)
            # 窓（書き出し・再生で差し替える範囲）を決めるための編集前の範囲
            a = float(np.interp(e0, out, src)) if side == "out" else float(n.start_sec)
            b = float(np.interp(e1, out, src)) if side == "in" else float(n.end_sec)
            a, b = max(float(n.start_sec), min(a, b)), min(float(n.end_sec), max(a, b))
            if b - a < 1e-4:
                b = min(float(n.end_sec), a + 1e-4)
            segs.append(Segment(start_sec=a, end_sec=b, fade=(round(e0, 6), round(e1, 6), side),
                                edit_ids=list(ids)))
    return segs


def apply_fades(y, sr, t0, fades):
    """出力 y（頭が編集後の秒 t0）に、フェードの音量の包絡を掛ける（その場で書き換えて返す）。

    fades = [(編集後の頭, 尻, "in"/"out"), ...]。区間の外は 1（触らない）。"""
    if not fades or y is None or not len(y):
        return y
    n = len(y)
    for e0, e1, side in fades:
        i0 = int(round((float(e0) - t0) * sr))
        i1 = int(round((float(e1) - t0) * sr))
        m = i1 - i0
        if m <= 0 or i1 <= 0 or i0 >= n:
            continue
        u = (np.arange(m) + 0.5) / m
        g = np.sin(0.5 * np.pi * u) if side == "in" else np.cos(0.5 * np.pi * u)
        a, b = max(0, i0), min(n, i1)
        g = g[a - i0:b - i0]
        if y.ndim == 1:
            y[a:b] = y[a:b] * g
        else:
            y[a:b] = y[a:b] * g[:, None]
    return y


def fade_specs(project, note_ids, fade_in=None, fade_out=None):
    """(外す id, 入れる spec, 変えたノート)。fade_in / fade_out: None = そのまま、0 = 消す、> 0 = その秒。

    ノートより長くはしない: 片側だけ変えたときはもう片側を残して、その側を「ノートの長さ − もう片側」まで。
    両方を渡してノートより長いときは比を保って縮める（編集後の長さで）。"""
    from .model import Target
    for v in (fade_in, fade_out):
        if v is not None and (not np.isfinite(float(v)) or float(v) < 0 or float(v) > MAX_SEC):
            raise ValueError("フェードの長さは 0〜%g 秒" % MAX_SEC)
    by = {n.id: n for n in project.take_notes}
    raw = raw_fades(project)
    src, out = project.time_map()
    rm, add, done = [], [], []
    for nid in note_ids:
        n = by.get(nid)
        if n is None or n.kind not in FADE_KINDS:
            continue
        cur = raw.get(nid, {})
        L = max(0.0, _map(src, out, n.end_sec, "left") - _map(src, out, n.start_sec))
        old_in = cur.get("in", (0.0, None))[0]
        old_out = cur.get("out", (0.0, None))[0]
        new_in = old_in if fade_in is None else float(fade_in)
        new_out = old_out if fade_out is None else float(fade_out)
        if new_in + new_out > L:
            if fade_in is not None and fade_out is not None:
                k = L / (new_in + new_out) if new_in + new_out > 0 else 0.0
                new_in, new_out = new_in * k, new_out * k
            elif fade_in is not None:
                new_in = max(0.0, L - new_out)
            else:
                new_out = max(0.0, L - new_in)
        changed = False
        for side, want, old, given in (("in", new_in, old_in, fade_in), ("out", new_out, old_out, fade_out)):
            if given is None or abs(old - want) < 1e-6:
                continue
            # 同じ錨・同じ向きの編集はすべて外す（後勝ちで隠れていたものも）
            anchor = float(n.start_sec if side == "in" else n.end_sec)
            for e in fade_edits(project):
                if e.params.get("side") == side and abs(anchor_of(e) - anchor) <= MATCH_TOL_SEC                         and e.id not in rm:
                    rm.append(e.id)
            if want > 1e-6:
                add.append({"kind": "fade", "target": Target.range(n.start_sec, n.end_sec),
                            "params": {"side": side, "sec": round(want, 6), "note_id": nid}})
            changed = True
        if changed:
            done.append(nid)
    return rm, add, done


def trim_fades(project, spans):
    """オリジナルに戻す: spans（ノートの [頭, 尻]。編集前の秒）のノートのフェード（頭のイン・尻のアウト）を外す id。
    接している隣のノートのフェード（同じ時刻の反対向き）は外さない。"""
    out = []
    for e in fade_edits(project):
        t = anchor_of(e)
        k = 0 if e.params.get("side") == "in" else 1
        if any(abs(t - sp[k]) <= MATCH_TOL_SEC for sp in spans):
            out.append(e.id)
    return out
