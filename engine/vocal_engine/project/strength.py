# -*- coding: utf-8 -*-
"""「ガイドに合わせる」の割合を後から変えるための記録（recipe。2026-10-11 承認。音程の割合を変えるのはここ、
タイミングの割合を変えるのは project/timing_ratio.py）。

ガイドの changeset に、出した鉛筆（`pitch_draw`）とは別に次の op を入れる。編集リスト（`_live_edits`）は
`add` / `remove` しか見ないので、前の版のエンジンはこの op を読み飛ばし、同じ編集・同じ音のまま開く。

    {"op": "recipe", "kind": "guide", "version": 1, "sig": {"take", "guide"}, "hop": 0.01,
     "notes": {ノート: {"pitch": {"center": 0〜1, "shape": 0〜1}, "edits": [鉛筆の id（区切りの順）],
                        "src": 材料を持つ changeset,                 # ← 音程の部（作り直したノートだけ）
                        "timing": 0〜1,                              # ← タイミングの部（今かかっている割合）
                        "span": [ノートの頭, 尻]}},
     "material": [区切り, ...],                      # 新しく合わせたノートがあるときだけ
     "timing_nodes": [節, ...]}                      # タイミングの部のあるノートの節（同じ op の中）
    {"op": "recipe_void", "notes": [ノート, ...], "parts": ["pitch"]}
                     # 割合 0% で音程の線を外した。parts が無ければ両方（「オリジナルに戻す」）

ノートの記録は**部ごと**に引き当てる: 音程は "pitch" のある最後の項、タイミングは "timing" のある最後の項
（同じ op に両方あることも、片方だけのこともある）。

**節**（timing_nodes の 1 つ）= `{"note": 持ち主のノート, "src": 素材の秒, "side": "l" | "r",
"role": "anchor" | "interp" | "end", "d": 100% の動き（秒）}`。d は `_repair` と全体の縮め（reach）の後の
`plan.d`（補間の値も凍結）。節は `src` と `side` で引き当てる（骨組みの節の番号は毎回変わる）。持ち主は
ノートの頭（アタック・近くの頭を含む組）→ そのノート、接続された境目（前の尻 = 次の頭）→ 次のノート、
切り離した尻 → そのノート、休みの手前の音節の終わり → そのノート。ノートの中の音素の境目は持たない
（`timing._timing_nodes`）。位置は「0% の位置 + timing × d + y」（0% の位置 = 今の位置 − timing × d − y）。
y（無ければ 0）= 割合のとおりの位置から外れている量（隣にゆずった・届かなかった）。hold: 1 = 前のノートを
「オリジナルに戻す」で戻した境目なので、もう動かさない（project/timing_ratio.py）。

timing は確定したときのタイミングの強度（x）。x = 0 でも節は残す（後から割合を上げられる）。x = 0 の確定は、
生きているタイミングの記録（ok）のあるノートには書かない（前の割合と節のまま）。

版（version）は 1 のまま: 足したのは項と op の中の新しい欄だけで、前の版（記録を読まない）は今までどおり
読み飛ばす。timing の無い記録（音程だけを残した、この形より前の記録）は、タイミングの状態を
`unsupported`（タイミングの割合は変えられない）として読む。

**区切り**（seg）= 1 つの連続した対応区間（run）の中の 1 ノートぶんのフレーム:
`{"n": ノート, "i0": 最初のフレーム, "m0": 補正前の MIDI（1/1000 半音。先頭の値と、後は前との差）,
"e": 100% の目標 − 補正前（1/1000 半音）, "run": [区間の最初と最後のフレームの秒], "s": [区切りの頭, 尻],
"p" / "x": 同じ区間で前 / 次に続くノート（無ければ null）}`。

## 割合から線を作る式（report §2-4）

フレームの ε = e（半音）、E = 2^(ε/12) − 1、ノートの C = そのノートの ε の平均、K = 2^(C/12) − 1。
音程の割合 ac・線の形の割合 as で D = as·E + (ac − as)·K、MIDI = m0 + 12·log2(1 + w·D)
（w = 区間の端の 40 ms のなだらかさ。ac = as = p なら今までの「Hz で p だけ寄せる」と同じ）。
隣のノートと割合が違う境目は、両側 20 ms（ノートの長さの 1/3 まで）で D を混ぜてなだらかにつなぐ。
隣が変えられないノート（手で直した・別の補正・外した）なら、その今の値へ 40 ms でつなぐ（lock）。

## 変えられない（stale）とき

解析し直した（テイク・ガイドの指紋 sig が違う）・ノートを分割・結合した・補正の線が外された（描き直し・
オリジナルに戻す）・材料が無い・新しい版の記録。画面は理由を 1 行出し、「今の状態で合わせ直す」を勧める。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

VERSION = 1
EDGE = 0.04       # 対応区間の端のなだらかさ（`timing._guide_pitch_shape` と同じ）
HALF = 0.02       # 割合の違う隣との混ぜ（片側。ノートの長さの 1/3 まで）
LOCK = 0.04       # 変えられない隣へつなぐ長さ（ノートの長さの 1/3 まで）
SCALE = 1000.0    # 材料の整数の単位（1/1000 半音）
F_MIN = 0.05      # 1 + w·D がこれ未満なら半音で線形に（log が発散しない）

REASONS = {
    "version": "新しい版で付けた補正",
    "material": "補正の材料が無い",
    "take": "テイクを解析し直した",
    "guide": "ガイドを差し替えた・解析し直した",
    "notes": "ノートが分割・結合された",
    "edits": "補正の線が外された（描き直し・オリジナルに戻す）",
    "unsupported": "前の版で付けた補正（タイミングの動きを残していない）",
}


def smooth(u):
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


# ================================================================ 指紋
def _hash(*parts):
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, np.ndarray):
            h.update(np.ascontiguousarray(p).tobytes())
        else:
            h.update(json.dumps(p, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:16]


def project_sig(project):
    """材料を作った解析の指紋 {"take", "guide"}。ガイドが無ければ None。"""
    take, guide = project.take_f0, project.guide_f0
    if take is None or guide is None or project.guide is None:
        return None
    try:
        ident = project._clip_cache_identity(project.guide)
    except (KeyError, TypeError, ValueError):
        ident = None
    gn = [[g.id, round(float(g.start_sec), 4), round(float(g.end_sec), 4),
           None if g.pitch_midi is None else round(float(g.pitch_midi), 3)]
          for g in project.guide_notes if g.kind == "note"]
    return {"take": _hash(np.asarray(take.f0, dtype="float64"), np.asarray(take.voiced, dtype=bool),
                          float(take.hop_s)),
            "guide": _hash(np.asarray(guide.f0, dtype="float64"), np.asarray(guide.voiced, dtype=bool),
                           float(guide.hop_s), ident, project.align_method, gn)}


# ================================================================ 材料
def make_segs(runs, hop, notes):
    """`_guide_pitch_shape` の連続した対応区間 [[(フレーム, 補正前 MIDI, 目標 MIDI, ノート), ...], ...] → 区切りの列。
    目標が補正前と同じ（1/1000 半音で）区間は除く。notes = {id: Note}（境目の秒に使う）。"""
    out = []
    for run in runs:
        if len(run) < 2:
            continue
        e_all = [int(round((m1 - m0) * SCALE)) for _, m0, m1, _ in run]
        if not any(e_all):
            continue
        t0, t1 = run[0][0] * hop, run[-1][0] * hop
        groups = []
        for k, (i, m0, m1, nid) in enumerate(run):
            if not groups or groups[-1][0] != nid:
                groups.append((nid, []))
            groups[-1][1].append((i, m0, e_all[k]))
        bounds = []
        for g in range(len(groups) - 1):
            a, b = groups[g], groups[g + 1]
            ta, tb = a[1][-1][0] * hop, b[1][0][0] * hop
            n = notes.get(a[0])
            s = float(n.end_sec) if n is not None else 0.5 * (ta + tb)
            bounds.append(min(max(s, ta), tb))
        for g, (nid, frs) in enumerate(groups):
            q = [int(round(m0 * SCALE)) for _, m0, _ in frs]
            s0 = bounds[g - 1] if g > 0 else frs[0][0] * hop
            s1 = bounds[g] if g + 1 < len(groups) else frs[-1][0] * hop
            out.append({"n": nid, "i0": int(frs[0][0]),
                        "m0": [q[0]] + [q[k] - q[k - 1] for k in range(1, len(q))],
                        "e": [e for _, _, e in frs],
                        "run": [round(t0, 6), round(t1, 6)], "s": [round(s0, 6), round(s1, 6)],
                        "p": groups[g - 1][0] if g > 0 else None,
                        "x": groups[g + 1][0] if g + 1 < len(groups) else None})
    return out


class Seg:
    def __init__(self, d, hop):
        self.d = d
        self.n = d["n"]
        self.p, self.x = d.get("p"), d.get("x")
        self.m0 = np.cumsum(np.asarray(d["m0"], dtype="float64")) / SCALE
        self.e = np.asarray(d["e"], dtype="float64") / SCALE
        self.t = (int(d["i0"]) + np.arange(len(self.m0))) * float(hop)
        self.run = (float(d["run"][0]), float(d["run"][1]))
        self.s0, self.s1 = float(d["s"][0]), float(d["s"][1])
        r0, r1 = self.run
        w = np.minimum(1.0, np.minimum(np.maximum(0.0, (self.t - r0) / EDGE),
                                       np.maximum(0.0, (r1 - self.t) / EDGE)))
        self.w = w * w * (3.0 - 2.0 * w)
        self.E = 2.0 ** (self.e / 12.0) - 1.0
        self.vp, self.vn = d.get("vp"), d.get("vn")

    @property
    def dur(self):
        return max(self.s1 - self.s0, 0.0)


class Material:
    """1 つの changeset の材料（または計画の新しい材料 "new"）。"""

    def __init__(self, key, segs, hop):
        self.key = key
        self.hop = float(hop)
        self.raw = list(segs)
        self.segs = [Seg(d, hop) for d in self.raw]
        self.by_note = {}
        for k, s in enumerate(self.segs):
            self.by_note.setdefault(s.n, []).append(k)
        self.C = {n: float(np.mean(np.concatenate([self.segs[k].e for k in ks])))
                  for n, ks in self.by_note.items()}

    def has(self, nid):
        return nid in self.by_note

    def prev(self, k):
        s = self.segs[k]
        if s.p is None or k == 0:
            return None
        o = self.segs[k - 1]
        return k - 1 if o.n == s.p and o.run == s.run else None

    def next(self, k):
        s = self.segs[k]
        if s.x is None or k + 1 >= len(self.segs):
            return None
        o = self.segs[k + 1]
        return k + 1 if o.n == s.x and o.run == s.run else None

    def neighbours(self, nid):
        out = []
        for k in self.by_note.get(nid, []):
            s = self.segs[k]
            out += [m for m in (s.p, s.x) if m is not None]
        return out


# ================================================================ 式
def _D(E, ac, as_, C):
    return as_ * E + (ac - as_) * (2.0 ** (C / 12.0) - 1.0)


def _lin(eps, ac, as_, C):
    return as_ * eps + (ac - as_) * C


def _h(a, b):
    return min(HALF, a.dur / 3.0, b.dur / 3.0)


def base_values(sg, own, prev=None, nxt=None, hp=0.0, hn=0.0):
    """区切りのフレームの MIDI。own / prev / nxt = (ac, as, C)。prev / nxt は混ぜる隣（無ければ None）。"""
    t = sg.t
    bp = smooth(((sg.s0 + hp) - t) / (2.0 * hp)) if prev is not None and hp > 0 else np.zeros_like(t)
    bn = smooth((t - (sg.s1 - hn)) / (2.0 * hn)) if nxt is not None and hn > 0 else np.zeros_like(t)
    bo = 1.0 - bp - bn
    D = bo * _D(sg.E, *own)
    L = bo * _lin(sg.e, *own)
    if prev is not None:
        D = D + bp * _D(sg.E, *prev)
        L = L + bp * _lin(sg.e, *prev)
    if nxt is not None:
        D = D + bn * _D(sg.E, *nxt)
        L = L + bn * _lin(sg.e, *nxt)
    F = 1.0 + sg.w * D
    return np.where(F >= F_MIN, sg.m0 + 12.0 * np.log2(np.maximum(F, F_MIN)), sg.m0 + sg.w * L)


def split_value(mat, ka, kb, ra, rb):
    """続く 2 つの区切り（ka の次が kb）の境目の値。ra / rb = (ac, as, C)。境目に向いた側の混ぜだけで決める
    （片方だけ作り直しても、もう片方の線と同じ点になる）。"""
    a, b = mat.segs[ka], mat.segs[kb]
    h = _h(a, b)
    va = float(base_values(a, ra, nxt=rb, hn=h)[-1])
    vb = float(base_values(b, rb, prev=ra, hp=h)[0])
    ta, tb, s = float(a.t[-1]), float(b.t[0]), a.s1
    if tb - ta <= 1e-12:
        return vb
    u = min(1.0, max(0.0, (s - ta) / (tb - ta)))
    return va + (vb - va) * u


def seg_points(mat, k, role):
    """区切り k の鉛筆の点 [[秒, MIDI], ...]。role(ノート) = (ac, as)（混ぜる）か None（その今の値へつなぐ）。"""
    sg = mat.segs[k]
    ac, as_ = role(sg.n)
    own = (ac, as_, mat.C[sg.n])
    kp = mat.prev(k) if sg.p is not None else None
    kn = mat.next(k) if sg.x is not None else None
    rp = role(sg.p) if kp is not None else None
    rn = role(sg.x) if kn is not None else None
    prev = (rp[0], rp[1], mat.C[sg.p]) if rp is not None else None
    nxt = (rn[0], rn[1], mat.C[sg.x]) if rn is not None else None
    hp = _h(mat.segs[kp], sg) if prev is not None else 0.0
    hn = _h(sg, mat.segs[kn]) if nxt is not None else 0.0
    v = base_values(sg, own, prev, nxt, hp, hn)
    t = sg.t
    out = v.copy()
    lock_l = min(LOCK, sg.dur / 3.0)
    head = tail = None
    if sg.p is not None:
        if prev is not None:
            head = split_value(mat, kp, k, prev, own)
        elif sg.vp is not None:
            head = float(sg.vp)
            out += _ramp((sg.s0 + lock_l) - t, (sg.s0 + lock_l) - t[0]) * (head - v[0])
    if sg.x is not None:
        if nxt is not None:
            tail = split_value(mat, k, kn, own, nxt)
        elif sg.vn is not None:
            tail = float(sg.vn)
            out += _ramp(t - (sg.s1 - lock_l), t[-1] - (sg.s1 - lock_l)) * (tail - v[-1])
    pts = [[float(a), float(b)] for a, b in zip(t, out)]
    if head is not None:
        pts = [p for p in pts if abs(p[0] - sg.s0) >= 1e-9]
        pts.insert(0, [sg.s0, head])
    if tail is not None:
        pts = [p for p in pts if abs(p[0] - sg.s1) >= 1e-9]
        pts.append([sg.s1, tail])
    return [[round(a, 6), round(b, 6)] for a, b in pts]


def _ramp(num, den):
    """0（窓の外）→ 1（端のフレーム）のなだらかさ。den = 端のフレームでの num。"""
    if den <= 1e-12:
        return (num >= den - 1e-12).astype(float) * (num > 0)
    return smooth(num / den) * (num > 0)


# ================================================================ 状態
def _ratio(ent):
    p = (ent or {}).get("pitch") or {}
    return float(p.get("center", 0.0)), float(p.get("shape", 0.0))


def _voids(op, part):
    return part in (op.get("parts") or ("pitch", "timing"))


def recipes(project, part="pitch"):
    """(材料 {changeset: op}, ノート → 生きている最後の記録 (op, entry) | None（外した）)。
    part = "pitch"（音程の部）か "timing"（タイミングの部）。"""
    mats, latest = {}, {}
    for cs in project.changesets:
        for op in cs.ops:
            k = op.get("op")
            if k == "recipe" and op.get("kind", "guide") == "guide":
                if op.get("material") is not None:
                    mats[cs.id] = op
                if cs.undone:
                    continue
                for nid, ent in (op.get("notes") or {}).items():
                    if part in ent:
                        latest[nid] = (op, ent)
            elif k == "recipe_void" and not cs.undone and _voids(op, part):
                for nid in op.get("notes") or []:
                    latest[nid] = None
    return mats, latest


def states(project, note_ids=None, sig=None):
    """{ノート: {"state": "ok" | "stale" | "none", "center", "shape", "manual", "reason", "src", "edits",
    "timing_state", "timing", "timing_manual", "timing_reason"}}。
    none = 割合の記録が無い（0% として扱い、動かすと今の状態から合わせる）。

    タイミング: timing_state = ok（記録の節がある）/ stale（変えられない。timing_reason）/ none（記録が無い。
    timing は 0.0）/ unsupported（音程の記録だけがある前の形。タイミングの割合は分からず、timing は None）。
    timing_manual = 合わせた後にタイミングを手で直した（`correction.origins`）。"""
    from .correction import origins
    sig = project_sig(project) if sig is None else sig
    mats, latest = recipes(project)
    _, tlatest = recipes(project, "timing")
    live = {e.id for e in project.edits}
    tn = {n.id: n for n in project.take_notes if n.kind == "note"}
    tim, pit = origins(project)
    ids = list(tn) if note_ids is None else [i for i in note_ids if i in tn]
    out = {}
    for nid in ids:
        row = {"state": "none", "center": 0.0, "shape": 0.0, "manual": pit.get(nid) == "manual",
               "reason": None, "src": None, "edits": [],
               "timing_state": "none", "timing": 0.0, "timing_manual": tim.get(nid) == "manual",
               "timing_reason": None}
        out[nid] = row
        trec = tlatest.get(nid)
        if trec is not None:
            top, tent = trec
            row["timing"] = float(tent.get("timing") or 0.0)
            why = _timing_stale(top, tent, sig, tn.get(nid), nid)
            row["timing_state"] = "stale" if why else "ok"
            row["timing_reason"] = REASONS.get(why) if why else None
        rec = latest.get(nid)
        if rec is None:
            continue
        op, ent = rec
        if trec is None:
            row["timing_state"], row["timing"] = "unsupported", None
            row["timing_reason"] = REASONS["unsupported"]
        row["center"], row["shape"] = _ratio(ent)
        row["src"] = ent.get("src")
        row["edits"] = list(ent.get("edits") or [])
        why = _stale(op, ent, mats, sig, tn.get(nid), live, nid)
        row["state"] = "stale" if why else "ok"
        row["reason"] = REASONS.get(why) if why else None
    return out


def _stale(op, ent, mats, sig, note, live, nid):
    if int(op.get("version", 1)) > VERSION:
        return "version"
    mop = mats.get(ent.get("src"))
    if mop is None or not isinstance(mop.get("material"), list):
        return "material"
    msig = mop.get("sig") or {}
    if sig is None or msig.get("take") != sig.get("take"):
        return "take"
    if msig.get("guide") != sig.get("guide"):
        return "guide"
    span = ent.get("span") or [None, None]
    if note is None or span[0] is None or abs(float(span[0]) - note.start_sec) > 1e-6 \
            or abs(float(span[1]) - note.end_sec) > 1e-6:
        return "notes"
    eds = ent.get("edits") or []
    if not eds or any(i not in live for i in eds):
        return "edits"
    if len(eds) != sum(1 for s in mop["material"] if s.get("n") == nid):
        return "material"
    return None


def _timing_stale(op, ent, sig, note, nid):
    """タイミングの記録が使えない理由（使えれば None）。節は同じ op の timing_nodes にある。"""
    if int(op.get("version", 1)) > VERSION:
        return "version"
    if not any(n.get("note") == nid for n in op.get("timing_nodes") or []):
        return "material"
    osig = op.get("sig") or {}
    if sig is None or osig.get("take") != sig.get("take"):
        return "take"
    if osig.get("guide") != sig.get("guide"):
        return "guide"
    span = ent.get("span") or [None, None]
    if note is None or span[0] is None or abs(float(span[0]) - note.start_sec) > 1e-6 \
            or abs(float(span[1]) - note.end_sec) > 1e-6:
        return "notes"
    return None


def timing_nodes(project, nid):
    """ノートの生きているタイミングの記録の節（無ければ []）。"""
    rec = recipes(project, "timing")[1].get(nid)
    if rec is None:
        return []
    return [dict(n) for n in rec[0].get("timing_nodes") or [] if n.get("note") == nid]


# ================================================================ 計画で使う
def plan_strength(project, sel, fresh, fresh_segs, cur, later=None, sig=None, st=None):
    """計画に載せる割合の情報（画面のプレビューと `realize` が同じものを使う）。

    sel = 選んだノート、fresh = 今の状態から合わせ直せるノート（記録が無い・変えられない）、
    fresh_segs = その新しい材料、cur(t) = 今の MIDI（隣へつなぐ値）、
    later(編集, t) = その鉛筆より後のピッチ編集のずらし量（セント。鉛筆の点はこれを足す前の値）。"""
    sig = project_sig(project) if sig is None else sig
    st = states(project, sig=sig) if st is None else st
    hop = float(project.take_f0.hop_s)
    mats, _ = recipes(project)
    materials = {}
    if fresh_segs:
        materials["new"] = [dict(s) for s in fresh_segs]
    # 選んだ ok のノートの区間と、そこに出てくるノートの区間を全部（隣を作り直すときに区切りが揃うように）
    want = {}
    for nid in sel:
        r = st.get(nid)
        if r and r["state"] == "ok":
            want.setdefault(r["src"], set()).add(nid)
    for src, ns in want.items():
        segs = mats[src]["material"]
        while True:
            rs = {tuple(s["run"]) for s in segs if s["n"] in ns}
            more = {s["n"] for s in segs if tuple(s["run"]) in rs} - ns
            if not more:
                break
            ns |= more
        materials[src] = [dict(s) for s in segs if s["n"] in ns]
    notes = {}
    for key, segs in materials.items():
        nth = {}
        for s in segs:
            j = nth[s["n"]] = nth.get(s["n"], -1) + 1
            eds = (st.get(s["n"]) or {}).get("edits") or []
            eid = eds[j] if key != "new" and j < len(eds) else None

            def local(t):
                v = float(cur(t))
                if eid is not None and later is not None:
                    v -= later(eid, t) / 100.0
                return round(v, 6)
            if s.get("p") is not None:
                s["vp"] = local(float(s["s"][0]) - 1e-6)
            if s.get("x") is not None:
                s["vn"] = local(float(s["s"][1]) + 1e-6)
            for m in (s["n"], s.get("p"), s.get("x")):
                if m is not None and m in st:
                    notes[m] = st[m]
    for nid in sel:
        if nid in st:
            notes[nid] = st[nid]
    out = {}
    for nid, r in notes.items():
        out[nid] = {"state": r["state"], "center": r["center"], "shape": r["shape"],
                    "manual": r["manual"], "reason": r["reason"], "src": r["src"],
                    "edits": r["edits"], "sel": nid in sel, "fresh": nid in fresh}
    return {"version": VERSION, "hop": hop, "sig": sig, "notes": out, "materials": materials}


def parse_materials(strength):
    hop = strength["hop"]
    return {k: Material(k, v, hop) for k, v in (strength.get("materials") or {}).items()}


def decide(notes, mats, center=None, shape=None, include_manual=False, restart=()):
    """作り直すノート {ノート: (材料, ac, as)}。center / shape = None はその割合を変えない。

    - 選んだノートのうち、手で直したノートは include_manual のときだけ
    - 記録のあるノート（ok）: 変えない側は記録の割合のまま。割合が同じなら作り直さない
    - 記録の無いノート・restart の変えられないノート: 変えない側は 0%。新しい材料から作る
    - ok のノートを作り直すときは、同じ材料で続く隣（手で直していない ok）も今の割合で作り直す
      （境目の混ぜが変わるので）。割合 (0, 0) のノートは線を外す（`realize`）"""
    if center is None and shape is None:
        return {}
    restart = set(restart or ())
    made = {}
    for nid, r in notes.items():
        if not r.get("sel"):
            continue
        if r["manual"] and not include_manual:
            continue
        if r["state"] == "ok":
            c = r["center"] if center is None else float(center)
            s = r["shape"] if shape is None else float(shape)
            if abs(c - r["center"]) > 1e-9 or abs(s - r["shape"]) > 1e-9:
                made[nid] = (r["src"], c, s)
        elif r["state"] == "none" or (r["state"] == "stale" and nid in restart):
            c = 0.0 if center is None else float(center)
            s = 0.0 if shape is None else float(shape)
            if (abs(c) > 1e-9 or abs(s) > 1e-9) and "new" in mats and mats["new"].has(nid):
                made[nid] = ("new", c, s)
    for nid, (key, _, _) in list(made.items()):
        if key == "new" or key not in mats:
            continue
        for m in mats[key].neighbours(nid):
            r = notes.get(m)
            if m in made or r is None:
                continue
            if r["state"] == "ok" and r["src"] == key and not r["manual"]:
                made[m] = (key, r["center"], r["shape"])
    return made


def roles(notes, made, key):
    """材料 key の線を作るときの、ノート → (ac, as)（混ぜる）| None（その今の値へつなぐ）。"""
    def role(nid):
        m = made.get(nid)
        if m is not None:
            return (m[1], m[2]) if m[0] == key else None
        r = notes.get(nid)
        if r and r["state"] == "ok" and r["src"] == key and not r["manual"]:
            return (r["center"], r["shape"])
        return None
    return role


def realize(project, strength, center=None, shape=None, include_manual=False, restart=()):
    """割合 → (外す編集, 入れる鉛筆の spec, 記録の op を作る関数)。何も変わらなければ ([], [], None)。"""
    from .model import Target
    notes = strength["notes"]
    mats = parse_materials(strength)
    made = decide(notes, mats, center, shape, include_manual, restart)
    if not made:
        return [], [], None
    removes, specs, tags, void = [], [], [], []
    order = sorted(made, key=lambda n: min(mats[made[n][0]].segs[k].t[0]
                                           for k in mats[made[n][0]].by_note[n]))
    for nid in order:
        key, ac, as_ = made[nid]
        mat = mats[key]
        old = list(notes[nid].get("edits") or []) if key != "new" else []
        if key != "new" and abs(ac) < 1e-9 and abs(as_) < 1e-9:
            removes += old
            void.append(nid)
            continue
        role = roles(notes, made, key)
        for j, k in enumerate(mat.by_note[nid]):
            pts = seg_points(mat, k, role)
            spec = {"kind": "pitch_draw", "target": Target.range(pts[0][0], pts[-1][0]),
                    "params": {"points": pts, "ramp_sec": 0.0}}
            if key != "new" and j < len(old):
                spec["in_place_of"] = old[j]
                removes.append(old[j])
            specs.append(spec)
            tags.append((nid, key, ac, as_))
    spans = {n.id: [n.start_sec, n.end_sec] for n in project.take_notes}
    fresh_made = {n for n, m in made.items() if m[0] == "new"}

    def ops(cs_id, edits):
        """apply_changes が呼ぶ: edits = 入れた編集（spec の順）。鉛筆はその末尾 len(tags) 件。"""
        out = []
        mine = edits[len(edits) - len(tags):] if tags else []
        ents = {}
        for (nid, key, ac, as_), e in zip(tags, mine):
            ent = ents.setdefault(nid, {"pitch": {"center": round(ac, 6), "shape": round(as_, 6)},
                                        "edits": [], "src": cs_id if key == "new" else key,
                                        "span": spans.get(nid)})
            ent["edits"].append(e.id)
        if ents:
            op = {"op": "recipe", "kind": "guide", "version": VERSION, "sig": strength["sig"],
                  "hop": strength["hop"], "notes": ents}
            if fresh_made:
                op["material"] = [_stored(s) for s in strength["materials"]["new"]
                                  if s["n"] in fresh_made]
            out.append(op)
        if void:
            out.append({"op": "recipe_void", "notes": void, "parts": ["pitch"]})
        return out

    return removes, specs, ops


def with_timing(project, base, rec, x):
    """記録の op を作る関数 base（音程。None もある）に、タイミングの部を足した関数を返す。

    rec = `plan.recipe_timing`（{"sig", "hop", "nodes": {ノート: [節]}}）、x = 確定したタイミングの強度。
    x > 0 なら選んだノート全部に割合 x と節を書く。x = 0 なら、生きているタイミングの記録（ok）が無いノートだけに
    0.0 と節を書く（記録のあるノートは前の割合と節のまま）。書くノートが無ければ base をそのまま返す。"""
    x = float(x or 0.0)
    nodes = rec.get("nodes") or {}
    if x > 1e-12:
        write = sorted(nodes)
    else:
        st = states(project, list(nodes), sig=rec.get("sig"))
        write = sorted(n for n in nodes if (st.get(n) or {}).get("timing_state") != "ok")
    if not write:
        return base
    spans = {n.id: [n.start_sec, n.end_sec] for n in project.take_notes}
    ratio = round(min(max(x, 0.0), 1.0), 6)

    def ops(cs_id, edits):
        out = list(base(cs_id, edits) or []) if base is not None else []
        op = next((o for o in out if o.get("op") == "recipe"), None)
        if op is None:
            op = {"op": "recipe", "kind": "guide", "version": VERSION, "sig": rec["sig"],
                  "hop": rec["hop"], "notes": {}}
            out.insert(0, op)
        for nid in write:
            ent = op["notes"].setdefault(nid, {})
            ent["timing"] = ratio
            ent.setdefault("span", spans.get(nid))
        op["timing_nodes"] = sorted((dict(n) for nid in write for n in nodes[nid]),
                                    key=lambda n: (n["src"], n["side"], n["note"]))
        return out

    return ops


def with_timing_ratio(project, base, writes, sig, hop):
    """記録の op を作る関数 base に、ノートごとのタイミングの割合を足した関数を返す（project/timing_ratio.py）。

    writes = {ノート: {"timing": 割合, "nodes": [節]}}。書くノートの節は、この op の timing_nodes に入る
    （前の記録の節は、そのノートについては読まれなくなる）。書くノートが無ければ base をそのまま返す。"""
    if not writes:
        return base
    spans = {n.id: [n.start_sec, n.end_sec] for n in project.take_notes}

    def ops(cs_id, edits):
        out = list(base(cs_id, edits) or []) if base is not None else []
        op = next((o for o in out if o.get("op") == "recipe"), None)
        if op is None:
            op = {"op": "recipe", "kind": "guide", "version": VERSION, "sig": sig, "hop": hop, "notes": {}}
            out.insert(0, op)
        for nid, w in sorted(writes.items()):
            ent = op["notes"].setdefault(nid, {})
            ent["timing"] = round(min(max(float(w["timing"]), 0.0), 1.0), 6)
            ent.setdefault("span", spans.get(nid))
        op["timing_nodes"] = sorted((dict(n) for w in writes.values() for n in w["nodes"]),
                                    key=lambda n: (n["src"], n["side"], n["note"]))
        return out

    return ops


def _stored(s):
    return {k: s[k] for k in ("n", "i0", "m0", "e", "run", "s", "p", "x")}


def preview_values(strength, mats, center, shape, include_manual, restart):
    """作り直すノートのフレーム → MIDI（試験とプレビューの確かめ用）。"""
    notes = strength["notes"]
    made = decide(notes, mats, center, shape, include_manual, restart)
    out = {}
    for nid, (key, ac, as_) in made.items():
        mat = mats[key]
        role = roles(notes, made, key)
        for k in mat.by_note[nid]:
            sg = mat.segs[k]
            if key != "new" and abs(ac) < 1e-9 and abs(as_) < 1e-9:     # 線を外す → 補正前
                for t, v in zip(sg.t, sg.m0):
                    out[int(round(t / mat.hop))] = float(v)
                continue
            pts = dict((round(a, 6), b) for a, b in seg_points(mat, k, role))
            for t in sg.t:
                v = pts.get(round(float(t), 6))
                if v is not None:
                    out[int(round(t / mat.hop))] = v
    return made, out
