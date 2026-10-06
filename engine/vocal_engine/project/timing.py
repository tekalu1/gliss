# -*- coding: utf-8 -*-
"""後ろをずらさないタイミング編集 — **接続 / 切り離し** と **計画（plan）**。

## 考え方（Melodyne と同じ）

ノートを短くしても、**そのノートと隣以外は 1 サンプルも動かさない**。
隣り合う 2 つのノートの境目ごとに「接続 / 切り離し」の状態を持つ:

- **接続**: 境目を 2 つのノートで共有する。片方が短くなった分、隣がそのまま長くなる。
  境目に挟まった子音（無声の短い区間）は長さを保ったまま一緒に動く。
- **切り離し**: 自分だけ伸び縮みし、**隙間**が増減する。縮めてできた隙間は無音
  （`silence`）、隙間へ伸ばしたぶんは隙間の音を切り取る（`crop`）。隙間の中身は
  **元の位置のまま**（隙間の端が動くだけ）。次のノートの頭の子音（アタック）は
  そのノートと一緒に動く。

既定の判定（`default_connected`）: 間に何も無い（ノート分割で隣接）か、
間が無声（`unvoiced`）だけで 0.30 秒未満なら接続。息・無音を挟めば切り離し。
状態を変えたものだけ `connection` 編集として編集リストに入る（undo で戻る）。

## 音程の無い区間の幅とタイミング（issues #35, #44）

無声（`unvoiced`。歌詞のある区間の子音など）・息（`breath`）・無音（`silence`）も、**それ自身を操作するとき**
（`plan_edge` / `plan_move` / `plan_reset_timing` の対象にしたとき）だけ、音程のあるノートと同じく骨組みの
ノートにする（`timing_notes`）。分割した片は同じ計画へ入れる。ほかの操作の骨組みは今までどおり音程のあるノートだけ（境目の子音は
接続なら長さを保って一緒に動き、切り離しなら隙間の中身として元の位置のまま）。

子音・息を含む組 x → y の接続（`extra_default`）は、音程ノートどうしと同じ既定（`default_connected`:
接していれば接続。子音の端を動かすと、接した隣のノートが伸び縮みする）。ただし挟んでいる音程ノートの組 (a, b)
の接続をユーザーが変えていれば（`connection` 編集。Alt の切り離し・右クリックの「切り離す」「つなぐ」）それに従う:
- (a, b) を**つないだ**なら、子音・息の両側も接続
- (a, b) を**切り離した**なら、両側とも切り離し。ただし次のノートの頭に接した短い無声（0.30 秒以下 = アタックの
  子音）は、そのノートと接続（切り離しの組の骨組みと同じく、アタックは次のノートと一緒に動く）
子音・息の組そのものの Alt の切り離し・吸着の接続は、音程ノートどうしと同じく `connection` 編集（既定と違う
ものだけ）に入り、それが最優先。

## 時間の骨組み（structure）

音程のあるノートの頭・尻、ノートの中の音素境界、アタックの頭を **knot**（節）にして、
節と節の間を **piece** に分ける。piece の種類:

| mode | 中身 | 伸び縮み |
|---|---|---|
| `stretch` | ノートの中の母音・息・無音（歌詞が無ければノート全体） | 同じ比で伸縮 |
| `keep` | ノートの中の子音・接続の境目の子音・アタック | 長さを保つ（位置だけ動く） |
| `gap` | 切り離された隙間 | 端が動くだけ（無音を足す／切り取る） |

## 計画（plan）

どの操作も「節を x に比例して動かす」形にする: 節 k の編集後の秒 = `cur_k + d_k * x`。
- ノート端のドラッグ・ノートの移動: x = ずらす秒、d は 0〜1 の重み
- ガイドに合わせる: x = 強度（0〜1）、d = 100% での移動量

**画面のプレビューも確定も同じ計画に x を入れるだけ**なので、ドラッグ中の見た目と
離した後の結果が一致する（`app/renderer/state.js: planWarp` が同じ式で描く）。
確定（`realize`）は、変わった範囲のタイミング編集を外して、節の新しい位置から
`stretch` / `crop` / `silence` を組み直す（範囲の外は編集が変わらないので元のまま）。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field

import numpy as np

from .. import log
from .model import Target

MIN_BODY_SEC = 0.02          # ノートの最短（画面の MIN_SEG と同じ）
F_MIN, F_MAX = 0.05, 20.0    # 伸縮の比（元の長さに対して）の範囲
CONNECT_MAX_GAP_SEC = 0.30   # 無声だけを挟む隣同士を「接続」とみなす隙間の上限
ATTACK_MAX_SEC = 0.30        # ノートの頭に付く子音（アタック）の上限
ATTACK_TOL_SEC = 0.02        # 子音と母音・ノート頭の食い違いの許容
EPS = 1e-7                   # これより小さい長さの差は「変わっていない」
TIMING_KINDS = ("stretch", "move", "move_boundary", "crop", "silence")


class TimingError(RuntimeError):
    pass


# ================================================================ 時間写像の読み方
class TimeMap:
    """編集前の秒 → 編集後の秒（無音の挿入で同じ秒に 2 値がある）。"""

    def __init__(self, src, out):
        self.src = np.asarray(src, dtype="float64")
        self.out = np.asarray(out, dtype="float64")

    def at(self, t, side="right"):
        t = float(t)
        v = float(np.interp(t, self.src, self.out))
        if side == "left":
            i = int(np.searchsorted(self.src, t, side="left"))
            if i < len(self.src) and self.src[i] == t:
                v = float(self.out[i])
        return v

    def points(self, a, a_side, b, b_side):
        """[a, b] の折れ点（a / b の極限の取り方つき）。"""
        va, vb = self.at(a, a_side), self.at(b, b_side)
        pts = [(float(a), va)]
        if b <= a:
            if vb != va:
                pts.append((float(b), vb))
            return pts
        m = (self.src >= a) & (self.src <= b)
        for s, o in zip(self.src[m], self.out[m]):
            s, o = float(s), float(o)
            if s == a and o <= va:
                continue
            if s == b and o >= vb:
                continue
            pts.append((s, o))
        pts.append((float(b), vb))
        return _dedup(pts)


def _dedup(pts):
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-12 and abs(out[-1][1] - p[1]) < 1e-12:
            continue
        out.append(p)
    return out


def current_map(project):
    src, out = project.time_map()
    return TimeMap(src, out)


# ================================================================ 接続
EXTRA_KINDS = ("unvoiced", "breath", "silence")   # 音程の無い区間も幅・タイミングを動かす


def pitched_notes(project):
    return sorted([n for n in project.take_notes if n.kind == "note"],
                  key=lambda n: n.start_sec)


def timing_notes(project, extra=()):
    """骨組みに入れるノート: 音程のあるノート＋ extra（操作する音程のない区間）。"""
    ids = set(extra or ())
    return sorted([n for n in project.take_notes
                   if n.kind == "note" or (n.id in ids and n.kind in EXTRA_KINDS)],
                  key=lambda n: n.start_sec)


def extra_ids(project, note_ids):
    """音程の無い対象と、同じ区間を分割してできた兄弟の id。"""
    kinds = {n.id: n.kind for n in project.take_notes}
    roots = {i.split("@")[0] for i in note_ids if kinds.get(i) in EXTRA_KINDS}
    return [i for i, kind in kinds.items() if kind in EXTRA_KINDS and i.split("@")[0] in roots]


def extra_default(project, x, y, ov=None):
    """音程のない区間を含む隣り合う組 x → y の既定の接続。ov: `connection_overrides`。"""
    if x.kind == y.kind and x.kind in EXTRA_KINDS and x.id.split("@")[0] == y.id.split("@")[0]:
        return default_connected(project, x, y)  # はさみで分けた同一区間の境目
    ov = connection_overrides(project) if ov is None else ov
    ps = pitched_notes(project)
    a = x if x.kind == "note" else next((n for n in reversed(ps) if n.end_sec <= x.start_sec + 1e-6), None)
    b = y if y.kind == "note" else next((n for n in ps if n.start_sec >= y.end_sec - 1e-6), None)
    c = ov.get((a.id, b.id)) if a is not None and b is not None else None
    if c is None:
        return default_connected(project, x, y)      # 音程ノートどうしと同じ既定
    if c:
        return True
    return (y is b and x.kind == "unvoiced" and b.start_sec - x.end_sec <= 1e-6
            and x.end_sec - x.start_sec <= ATTACK_MAX_SEC)


def timing_connections(project, ns, conn=None):
    """骨組みのノートの並び ns の隣り合う組ごとの接続 {(a, b): bool}（子音・息を含む組は、その組の
    `connection` 編集か extra_default）。"""
    conn = connection_map(project) if conn is None else conn
    ov = connection_overrides(project)
    out = dict(conn)
    for x, y in zip(ns[:-1], ns[1:]):
        if x.kind == "note" and y.kind == "note":
            continue
        k = (x.id, y.id)
        out[k] = ov[k] if k in ov else extra_default(project, x, y, ov)
    return out


def default_connected(project, a, b):
    """隣り合う音程ノート a → b の既定の接続。"""
    gap = b.start_sec - a.end_sec
    if gap <= 1e-6:
        return True
    if gap >= CONNECT_MAX_GAP_SEC:
        return False
    between = [n for n in project.take_notes
               if n.start_sec >= a.end_sec - 1e-6 and n.end_sec <= b.start_sec + 1e-6
               and n.kind != "note"]
    return all(n.kind == "unvoiced" for n in between)


PAIR_TIME_TOL_SEC = 0.06   # 付け替えた connection の境目と、今の解析の組の境目のずれの許し


def connection_pair(project, e, notes=None):
    """`connection` の編集が指す組 (a, b)。F0 の方式を替える前に付け替えたもの（params.by_time）は、記録した境目の
    区間（target = a の終わり〜b の始まり）に両端が最も近い、今の解析の隣り合う組（許し PAIR_TIME_TOL_SEC。
    無ければ None = 当てない）。notes: 引く相手のノートの並び（省くと params.pair で選ぶ）。"""
    if not e.params.get("by_time"):
        return (e.params["a"], e.params["b"])
    t0, t1 = float(e.target.start_sec), float(e.target.end_sec)
    if notes is None:
        notes = (pitched_notes(project) if e.params.get("pair") == "note" else
                 sorted([n for n in project.take_notes if n.kind == "note" or n.kind in EXTRA_KINDS],
                        key=lambda n: n.start_sec))
    best = None
    for x, y in zip(notes[:-1], notes[1:]):
        dx, dy = abs(x.end_sec - t0), abs(y.start_sec - t1)
        if dx <= PAIR_TIME_TOL_SEC and dy <= PAIR_TIME_TOL_SEC and (best is None or dx + dy < best[0]):
            best = (dx + dy, x.id, y.id)
    return (best[1], best[2]) if best is not None else None


def connection_overrides(project):
    """編集リストの `connection`（後勝ち）。{(a, b): bool}"""
    out = {}
    lists = {}
    for e in project.edits:
        if e.kind == "connection":
            notes = None
            if e.params.get("by_time"):
                kind = "note" if e.params.get("pair") == "note" else "any"
                if kind not in lists:
                    lists[kind] = (pitched_notes(project) if kind == "note" else
                                   sorted([n for n in project.take_notes if n.kind == "note" or n.kind in EXTRA_KINDS],
                                          key=lambda n: n.start_sec))
                notes = lists[kind]
            key = connection_pair(project, e, notes)
            if key is not None:
                out[key] = bool(e.params["connected"])
    return out


def connections(project, overrides=None):
    """隣り合う音程ノートの組ごとの状態 [(a, b, connected, default)]。

    overrides: {(a, b): bool} を編集リストの上に重ねる（計画の「確定したらこうなる」を測るため）。"""
    ns = pitched_notes(project)
    ov = connection_overrides(project)
    if overrides:
        ov = {**ov, **overrides}
    rows = []
    for a, b in zip(ns[:-1], ns[1:]):
        d = default_connected(project, a, b)
        rows.append((a, b, ov.get((a.id, b.id), d), d))
    return rows


def connection_map(project):
    return {(a.id, b.id): c for a, b, c, _ in connections(project)}


# ================================================================ 骨組み
@dataclass
class Knot:
    src: float
    side: str                  # "left"（手前の極限）| "right"
    role: str                  # note_start / note_end / attack / inner / edge
    unit: int | None = None    # 一緒に動く組（接続の境目、アタック＋頭）
    note: str | None = None
    cur: float = 0.0


@dataclass
class Piece:
    a: int                     # knot の番号
    b: int
    mode: str                  # stretch / keep / gap
    note: str | None = None    # ノートの中の piece ならそのノート
    rmin: float = 1.0          # piece の中の今の伸縮比（元の長さに対して）の最小・最大
    rmax: float = 1.0


@dataclass
class Structure:
    knots: list = field(default_factory=list)
    pieces: list = field(default_factory=list)
    units: dict = field(default_factory=dict)          # unit -> [knot index]
    fixed_units: set = field(default_factory=set)
    note_knots: dict = field(default_factory=dict)     # note id -> (start knot, end knot)
    pairs: dict = field(default_factory=dict)          # (a, b) -> connected
    gap_of_pair: dict = field(default_factory=dict)    # (a, b) -> piece index（切り離し）

    def unit_of(self, k):
        return self.knots[k].unit


def _phoneme_list(project, confirmed_only=False):
    if not project.has_lyrics("take"):
        return []
    r = project.phonemes("take")
    if r is None:
        return []
    if not confirmed_only:
        return list(r.phonemes)
    from ..phoneme.lyrics import confirmed_syllable_indices
    confirmed = confirmed_syllable_indices(project.lyrics_entries("take"), r.syllables)
    return [p for p in r.phonemes if p.syllable_index in confirmed]


def _attack_start(project, phs, note, lo):
    """ノートの頭に付く子音（アタック）の頭。無ければ note.start_sec。"""
    s = note.start_sec
    near = [p for p in phs if p.end_sec > s - 0.05 and p.start_sec < s + 0.01]
    c = s
    if near:
        before = [p for p in phs if p.start_sec < s - 1e-6]
        j = len(before) - 1
        if j >= 0 and before[j].stretchable and before[j].start_sec >= s - ATTACK_TOL_SEC:
            c = before[j].start_sec          # ノート頭の少し前に始まる自分の母音
            j -= 1
        while (j >= 0 and not before[j].stretchable
               and before[j].end_sec >= c - 1e-3 and s - before[j].start_sec <= ATTACK_MAX_SEC):
            c = before[j].start_sec
            j -= 1
        if c < s and not any(not p.stretchable for p in phs
                             if p.start_sec >= c - 1e-6 and p.end_sec <= s + 1e-6):
            c = s                            # 子音が無ければアタックも無い
    else:
        prev = [n for n in project.take_notes
                if abs(n.end_sec - s) < 1e-6 and n.kind == "unvoiced"
                and n.end_sec - n.start_sec <= ATTACK_MAX_SEC]
        if prev:
            c = prev[0].start_sec
    return max(lo, min(c, s))


def build_structure(project, overrides=None, attacks=None, extra=None, confirmed_only=False):
    """ノートと音素から節と piece を組む。overrides = {(a, b): connected} で状態を上書き。
    extra: 骨組みのノートに入れる音程のない区間の id（それ自身を操作するとき）。

    attacks = {note id: 秒}: 切り離されたノートのアタックの頭をそこまで前に広げる
    （「ガイドに合わせる」: 音程の付く少し前の隙間にある発音の頭を、ノートと一緒に動かす）。"""
    tm = current_map(project)
    ns = timing_notes(project, extra)
    phs = _phoneme_list(project, confirmed_only=confirmed_only)
    dur = project.duration_sec
    conn = connection_map(project)
    if extra:
        conn = timing_connections(project, ns, conn)
    if overrides:
        conn.update(overrides)
    st = Structure(pairs=dict(conn))
    uid = [0]

    def new_unit():
        uid[0] += 1
        st.units[uid[0]] = []
        return uid[0]

    def add_knot(src, side, role, unit=None, note=None):
        k = Knot(src=float(src), side=side, role=role, unit=unit, note=note)
        k.cur = tm.at(k.src, side)
        st.knots.append(k)
        if unit is not None:
            st.units[unit].append(len(st.knots) - 1)
        return len(st.knots) - 1

    def add_piece(mode, note=None):
        n = len(st.knots)
        st.pieces.append(Piece(a=n - 2, b=n - 1, mode=mode, note=note))
        return len(st.pieces) - 1

    u0 = new_unit()
    st.fixed_units.add(u0)
    add_knot(0.0, "right", "edge", u0)
    if not ns:
        u1 = new_unit()
        st.fixed_units.add(u1)
        add_knot(dur, "left", "edge", u1)
        add_piece("gap")
        return st, tm

    prev = None
    carry_unit = None               # 接続の境目: 前のノートの尻と同じ組
    for n in ns:
        if prev is None or not conn.get((prev.id, n.id), True):
            lo = prev.end_sec if prev is not None else 0.0
            # 子音・息のノート自身にはアタックを付けない（頭 = ノートの頭）
            c = _attack_start(project, phs, n, lo) if n.kind == "note" else n.start_sec
            if attacks and n.id in attacks:
                c = max(lo, min(c, float(attacks[n.id])))
            u = new_unit()
            add_knot(c, "right", "attack" if c < n.start_sec else "note_start", u, n.id)
            gp = add_piece("gap")
            if prev is not None:
                st.gap_of_pair[(prev.id, n.id)] = gp
            if c < n.start_sec:
                add_knot(n.start_sec, "right", "note_start", u, n.id)
                add_piece("keep")
            start_k = len(st.knots) - 1
        else:
            add_knot(n.start_sec, "right", "note_start", carry_unit, n.id)
            add_piece("keep")                 # 境目に挟まった子音（長さ 0 もある）
            start_k = len(st.knots) - 1
        # ---- ノートの中（音素境界で切る。子音は keep）
        inner = sorted({round(b, 9) for p in phs for b in (p.start_sec, p.end_sec)
                        if n.start_sec + 1e-5 < b < n.end_sec - 1e-5})
        edges = [n.start_sec] + inner + [n.end_sec]
        modes = []
        for a, b in zip(edges[:-1], edges[1:]):
            mid = 0.5 * (a + b)
            ph = next((p for p in phs if p.start_sec <= mid < p.end_sec), None)
            modes.append("keep" if (ph is not None and not ph.stretchable) else "stretch")
        if all(m == "keep" for m in modes):
            modes = ["stretch"] * len(modes)   # 子音だけのノート: 全体で伸縮
        for b, m in zip(inner, modes[:-1]):
            add_knot(b, "right", "inner", None, n.id)
            add_piece(m, n.id)
        carry_unit = new_unit()
        add_knot(n.end_sec, "left", "note_end", carry_unit, n.id)
        add_piece(modes[-1], n.id)
        st.note_knots[n.id] = (start_k, len(st.knots) - 1)
        prev = n
    u1 = new_unit()
    st.fixed_units.add(u1)
    add_knot(dur, "left", "edge", u1)
    add_piece("gap")
    _piece_ratios(st, tm)
    return st, tm


def _piece_ratios(st, tm):
    """piece の中の今の伸縮比の最小・最大（中に以前の編集の切れ目があるとき）。"""
    for pc in st.pieces:
        if pc.mode == "gap":
            continue
        ka, kb = st.knots[pc.a], st.knots[pc.b]
        rs = []
        pts = tm.points(ka.src, ka.side, kb.src, kb.side)
        for (s0, o0), (s1, o1) in zip(pts[:-1], pts[1:]):
            if s1 - s0 > 1e-9 and o1 - o0 > 1e-12:
                rs.append((o1 - o0) / (s1 - s0))
        if rs:
            pc.rmin, pc.rmax = min(rs), max(rs)


# ================================================================ 解く
@dataclass
class Solved:
    d: np.ndarray                   # 節ごとの重み（x を掛けると秒）
    constraints: list               # [(a, b, why, ka, kb, sgn)]: a + b x >= 0、b = sgn*(d[kb]-d[ka])
    runs: list


def solve(st, anchors):
    """anchors = {knot: d}（組の knot を 1 つ指せば組全体に効く）。

    組（unit）に属する節は、指定が無ければ 0（動かない）。ノートの中の節は
    両側の節から決まる（伸びる piece に長さの比で配る。子音は保つ）。
    piece p はいつも節 p と p+1 の間（`build_structure` がそう組む）。"""
    n = len(st.knots)
    d = np.zeros(n)
    anch = np.zeros(n, dtype=bool)
    for u, ks in st.units.items():
        for kk in ks:
            anch[kk] = True
    for k, v in anchors.items():
        u = st.knots[k].unit
        ks = st.units[u] if u is not None else [k]
        for kk in ks:
            d[kk] = float(v)
            anch[kk] = True
    cur = np.array([k.cur for k in st.knots])
    src = np.array([k.src for k in st.knots])
    modes = [pc.mode for pc in st.pieces]
    cons = []
    runs = []
    idx = np.flatnonzero(anch)
    for ka, kb in zip(idx[:-1], idx[1:]):
        ka, kb = int(ka), int(kb)
        ps = range(ka, kb)
        if any(modes[p] == "gap" for p in ps):
            g0 = next(p for p in ps if modes[p] == "gap")
            for k in range(ka + 1, kb):
                d[k] = d[ka] if k <= g0 else d[kb]
            continue
        lens = {p: cur[p + 1] - cur[p] for p in ps}
        srcs = {p: src[p + 1] - src[p] for p in ps}
        grow = [p for p in ps if modes[p] == "stretch" and lens[p] > 1e-9]
        if not grow:
            grow = [p for p in ps if lens[p] > 1e-9]
        S = float(sum(lens[p] for p in grow))
        delta = d[kb] - d[ka]
        acc = 0.0
        gs = set(grow)
        for p in ps:
            if p in gs:
                acc += lens[p]
            if p + 1 < kb:
                d[p + 1] = d[ka] + (delta * acc / S if S > 0 else 0.0)
        runs.append({"a": ka, "b": kb, "S": S, "delta": delta})
        if abs(delta) > 1e-12:
            if S <= 1e-9:
                cons.append((0.0, delta, "伸び縮みできる長さが無い", ka, kb, 1.0))
                cons.append((0.0, -delta, "伸び縮みできる長さが無い", ka, kb, -1.0))
            else:
                withsrc = [p for p in grow if srcs[p] > 1e-9]
                flo = max([F_MIN * srcs[p] / lens[p] for p in withsrc] or [F_MIN])
                fhi = min([F_MAX * srcs[p] / lens[p] for p in withsrc] or [F_MAX])
                # piece の中の細かい区間（以前の編集）も、編集リストの比の範囲に収める
                from .model import STRETCH_RATIO_RANGE
                rlo, rhi = STRETCH_RATIO_RANGE
                flo = max([flo] + [1.05 * rlo / st.pieces[p].rmin for p in grow])
                fhi = min([fhi] + [0.95 * rhi / st.pieces[p].rmax for p in grow])
                cons.append(((1.0 - flo) * S, delta, "縮めすぎ", ka, kb, 1.0))
                cons.append(((fhi - 1.0) * S, -delta, "伸ばしすぎ", ka, kb, -1.0))
    # ノートの長さの下限と、隙間が負にならないこと
    for nid, (ks, ke) in st.note_knots.items():
        b = d[ke] - d[ks]
        if abs(b) > 1e-12:
            cons.append((cur[ke] - cur[ks] - MIN_BODY_SEC, b,
                         "ノートが短すぎる（%s）" % nid, ks, ke, 1.0))
    for p, m in enumerate(modes):
        if m != "gap":
            continue
        b = d[p + 1] - d[p]
        if abs(b) > 1e-12:
            cons.append((cur[p + 1] - cur[p], b, "隣を追い越す", p, p + 1, 1.0))
    return Solved(d=d, constraints=cons, runs=runs)


def x_range(cons):
    lo, hi = -np.inf, np.inf
    for a, b, *_ in cons:
        if abs(b) < 1e-15:
            continue
        v = -a / b
        if b > 0:
            lo = max(lo, v)
        else:
            hi = min(hi, v)
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    return float(lo), float(hi)


# ================================================================ 計画
@dataclass
class Plan:
    id: str
    kind: str
    params: dict
    st: Structure
    d: np.ndarray
    x_lo: float
    x_hi: float
    snap_x: float | None = None
    snap_pair: tuple | None = None
    set_connections: list = field(default_factory=list)   # 確定で入れる接続の変更
    force_notes: set = field(default_factory=set)          # 中を元どおりに組み直すノート（原音に戻す）
    pitch: dict = field(default_factory=dict)              # note -> 100% でのセント
    pitch_curve: list = field(default_factory=list)        # [[素材の秒, 現在 Hz, ガイド Hz, 端の重み], ...]
    pitch_draws: list = field(default_factory=list)        # 連続した対応・有声区間の鉛筆 spec
    pairs: list = field(default_factory=list)              # ガイドとの対応（ノートの組。音程）
    timing: list = field(default_factory=list)             # ガイドに合わせる頭（1 対 1 の組）
    notes: list = field(default_factory=list)              # ノートごとの対応と理由（ガイドのみ。issue #53）
    info: dict = field(default_factory=dict)
    sig: tuple = ()

    def new_positions(self, x):
        return np.array([k.cur for k in self.st.knots]) + self.d * float(x)

    def to_json(self, compact=True):
        ks = self.st.knots
        moving = [i for i in range(len(ks)) if abs(self.d[i]) > 1e-12]
        if moving and compact:
            a = max(0, moving[0] - 1)
            b = min(len(ks) - 1, moving[-1] + 1)
        else:
            a, b = 0, len(ks) - 1
        return {
            "plan_id": self.id, "kind": self.kind, "params": self.params,
            "x_range": [_r(self.x_lo), _r(self.x_hi)],
            "snap_x": None if self.snap_x is None else _r(self.snap_x),
            "snap_pair": list(self.snap_pair) if self.snap_pair else None,
            "knots": [[_r(ks[i].src), _r(ks[i].cur), _r(self.d[i]),
                       "l" if ks[i].side == "left" else "r"] for i in range(a, b + 1)],
            "pieces": [self.st.pieces[p].mode[0] for p in range(len(self.st.pieces))
                       if self.st.pieces[p].a >= a and self.st.pieces[p].b <= b],
            "pitch": {k: _r(v, 3) for k, v in self.pitch.items()},
            "pitch_curve": [[_r(t, 6), _r(h0, 6), _r(h1, 6), _r(w, 6)]
                            for t, h0, h1, w in self.pitch_curve],
            # 確定で変わる接続（Alt の切り離し）と、吸着で生まれるつなぎ。画面はドラッグ中の
            # 曲線にこれを反映する（離した後になだらかさが現れる／消えることが無いように）
            "set_connections": [[a, b, bool(c)] for a, b, c in self.set_connections],
            "snap_transition": self.info.get("snap_transition"),
            "pairs": self.pairs,
            "timing": self.timing,
            "notes": self.notes,
            "info": self.info,
        }


def _r(v, nd=7):
    v = float(v)
    if not np.isfinite(v):
        return None
    return round(v, nd)


def state_sig(project):
    return tuple(c.id for c in project.changesets if not c.undone)


def _finish(project, kind, params, st, anchors, **kw):
    sol = solve(st, anchors)
    lo, hi = x_range(sol.constraints)
    return Plan(id="p%s" % uuid.uuid4().hex[:8], kind=kind, params=params, st=st,
                d=sol.d, x_lo=lo, x_hi=hi, sig=state_sig(project), **kw)


def _note_or_raise(st, nid):
    if nid not in st.note_knots:
        raise TimingError("動かせる区間ではない: %s"
                          "（list_notes で確認）" % nid)
    return st.note_knots[nid]


def _neighbour(project, nid, side, extra=None):
    ns = timing_notes(project, extra)
    i = next(i for i, n in enumerate(ns) if n.id == nid)
    j = i + (1 if side == "end" else -1)
    return ns[j] if 0 <= j < len(ns) else None


def plan_edge(project, note_id, side="end", detach=False):
    """ノートの端（side = "start" / "end"）を動かす計画。x = 秒（+ が後ろ）。

    接続された端は隣と共有して動く（Alt = detach=True で切り離して自分だけ動く）。
    切り離された端は自分だけ動き、隙間が増減する。隣にぶつかる位置が `snap_x`。"""
    if side not in ("start", "end"):
        raise TimingError("side は start か end")
    extra = extra_ids(project, [note_id])
    if not extra and note_id not in {n.id for n in pitched_notes(project)}:
        _note_or_raise(Structure(), note_id)
    nb = _neighbour(project, note_id, side, extra)
    pair = None
    if nb is not None:
        pair = (note_id, nb.id) if side == "end" else (nb.id, note_id)
    conn = connection_map(project)
    if extra:
        conn = timing_connections(project, timing_notes(project, extra), conn)
    overrides = {}
    set_conn = []
    if detach and pair and conn.get(pair, False):
        overrides[pair] = False
        set_conn.append((pair[0], pair[1], False))
    st, _ = build_structure(project, overrides, extra=extra)
    ks, ke = _note_or_raise(st, note_id)
    k = ke if side == "end" else ks
    plan = _finish(project, "edge", {"note_id": note_id, "side": side, "detach": bool(detach)},
                   st, {k: 1.0}, set_connections=set_conn)
    if pair and pair in st.gap_of_pair:
        p = st.pieces[st.gap_of_pair[pair]]
        L = st.knots[p.b].cur - st.knots[p.a].cur
        b = plan.d[p.b] - plan.d[p.a]
        if abs(b) > 1e-12:
            plan.snap_x = float(-L / b)
            plan.snap_pair = pair
    plan.info = {"pair": list(pair) if pair else None,
                 "connected": bool(st.pairs.get(pair, False)) if pair else None,
                 "neighbour": nb.id if nb is not None else None}
    if plan.snap_pair:
        plan.info["snap_transition"] = _would_be_transition(project, plan.snap_pair)
    return plan


def _would_be_transition(project, pair):
    """切り離された組が吸着で接続になったときのつなぎ（JSON）。無ければ None。

    つなぎの窓は編集前の秒で決まり、端を動かしても変わらないので、今の状態に接続だけ
    重ねて測れば確定後と同じになる。"""
    from .pitch import transitions
    for tr in transitions(project, overrides={tuple(pair): True}):
        if tr.kind == "boundary" and (tr.a, tr.b) == tuple(pair):
            return tr.to_json()
    return None


def plan_move(project, note_ids):
    """ノート（複数可。子音・息・無音も可）を横に動かす計画。x = 秒。接続側の隣は伸び縮み、切り離し側は隙間が吸収。"""
    st, _ = build_structure(project, extra=extra_ids(project, note_ids))
    anchors = {}
    for nid in note_ids:
        ks, ke = _note_or_raise(st, nid)
        anchors[ks] = 1.0
        anchors[ke] = 1.0
    return _finish(project, "move", {"note_ids": list(note_ids)}, st, anchors)


def plan_reset_timing(project, note_ids):
    """ノートのタイミングを元に戻す計画（x = 1 で元どおり）。接続された隣は伸び縮みで合わせる。"""
    st, _ = build_structure(project, extra=extra_ids(project, note_ids))
    anchors = {}
    for nid in note_ids:
        ks, ke = _note_or_raise(st, nid)
        for k in range(ks, ke + 1):
            anchors[k] = st.knots[k].src - st.knots[k].cur
        # アタックも元の位置へ
        u = st.knots[ks].unit
        for k in st.units.get(u, []):
            anchors[k] = st.knots[k].src - st.knots[k].cur
    # 組の中で値が食い違うとき（接続の境目の両側）は平均
    anchors = _merge_units(st, anchors)
    plan = _finish(project, "reset", {"note_ids": list(note_ids)}, st, anchors)
    plan.force_notes = set(note_ids)      # 頭・尻が動かなくても、中の伸縮（音素単位など）を外す
    return plan


IDENTITY_TOL_SEC = 1e-5      # 時間の対応が元どおり（編集後の秒 = 編集前の秒）とみなすずれ


def reset_timing_window(project, start_sec, end_sec):
    """範囲の「オリジナルに戻す」のタイミング: (外す編集の id, [A, B])。

    範囲にかかるタイミングの編集（stretch / crop / silence / move / move_boundary）を、
    時間の対応が元どおりの所（編集後の秒 = 編集前の秒。後ろをずらさない編集の組の切れ目）まで
    [A, B] を広げて丸ごと外す。外した後の [A, B] は原音の時間、その外は今のまま。
    ノートの頭・尻を元の位置へ戻す計画（`plan_reset_timing`）は、今のノートの切れ目と合わない編集
    （解析の方式・版が変わった後の古い編集）を外しきれず、補う伸縮を足してしまう。範囲で戻すときはこちら。"""
    tm = current_map(project)
    src = tm.src
    dur = float(project.duration_sec)

    def delta(t, side):
        return tm.at(t, side) - t

    def span(e):
        a, b = project.edit_span(e)
        return float(a), float(b)

    timing = [e for e in project.edits if e.kind in TIMING_KINDS]
    A, B = float(start_sec), float(end_sec)
    for _ in range(1000):
        lo, hi = A, B
        for e in timing:
            a, b = span(e)
            if (b > A and a < B) or (A - 1e-9 <= a <= B + 1e-9 and b - a <= 1e-9):
                lo, hi = min(lo, a), max(hi, b)
        # 左端は「そこより前の編集の積み上げ」が 0、右端は「そこまでの積み上げ」が 0 の所まで広げる
        if abs(delta(lo, "left")) > IDENTITY_TOL_SEC:
            ok = [s for s in src if s < lo and abs(delta(s, "left")) <= IDENTITY_TOL_SEC]
            lo = float(max(ok)) if ok else 0.0
        if abs(delta(hi, "right")) > IDENTITY_TOL_SEC:
            ok = [s for s in src if s > hi and abs(delta(s, "right")) <= IDENTITY_TOL_SEC]
            hi = float(min(ok)) if ok else dur
        if lo == A and hi == B:
            break
        A, B = lo, hi
    rm = []
    for e in timing:
        a, b = span(e)
        if (b > A and a < B) or (A - 1e-9 <= a <= B + 1e-9 and b - a <= 1e-9):
            rm.append(e.id)
    return rm, [A, B]


def plan_range_stretch(project, start_sec, end_sec, ratio):
    """範囲 [start, end]（1 つのノートの中）を ratio 倍。同じノートの残りが吸収する。"""
    st, tm = build_structure(project)
    owner = None
    for nid, (ks, ke) in st.note_knots.items():
        if st.knots[ks].src - 1e-6 <= start_sec and end_sec <= st.knots[ke].src + 1e-6:
            owner = nid
            break
    if owner is None:
        raise TimingError("範囲での伸縮は 1 つのノートの中で指定する"
                          "（ノート全体なら note_id、音素なら phoneme_id）")
    ks, ke = st.note_knots[owner]
    a = _insert_knot(st, tm, start_sec, owner)
    b = _insert_knot(st, tm, end_sec, owner)
    cur_len = st.knots[b].cur - st.knots[a].cur
    plan = _finish(project, "range", {"start_sec": start_sec, "end_sec": end_sec,
                                      "ratio": ratio}, st, {a: 0.0, b: 1.0})
    plan.info = {"note_id": owner, "cur_len_sec": cur_len, "want_x": (ratio - 1.0) * cur_len}
    return plan


def _insert_knot(st, tm, t, note):
    for i, k in enumerate(st.knots):
        if abs(k.src - t) < 1e-6 and k.note == note:
            return i
    p = next(i for i, pc in enumerate(st.pieces)
             if st.knots[pc.a].src <= t <= st.knots[pc.b].src and pc.note == note)
    pc = st.pieces[p]
    k = Knot(src=float(t), side="right", role="inner", note=note)
    k.cur = tm.at(t, "right")
    ins = pc.b
    st.knots.insert(ins, k)
    # 番号を付け直す
    for q in st.pieces:
        if q.a >= ins:
            q.a += 1
        if q.b >= ins:
            q.b += 1
    for u, ks in st.units.items():
        st.units[u] = [kk + 1 if kk >= ins else kk for kk in ks]
    st.note_knots = {n: (a + (1 if a >= ins else 0), b + (1 if b >= ins else 0))
                     for n, (a, b) in st.note_knots.items()}
    old_b = pc.b
    pc.b = ins
    st.pieces.insert(p + 1, Piece(a=ins, b=old_b, mode=pc.mode, note=pc.note))
    st.gap_of_pair = {k: (v + 1 if v > p else v) for k, v in st.gap_of_pair.items()}
    return ins


def _merge_units(st, anchors):
    by_unit = {}
    out = {}
    for k, v in anchors.items():
        u = st.knots[k].unit
        if u is None:
            out[k] = v
        else:
            by_unit.setdefault(u, []).append((k, v))
    for u, kv in by_unit.items():
        k0 = kv[0][0]
        out[k0] = float(np.mean([v for _, v in kv]))
    return out


# ---------------------------------------------------------------- ガイドとの対応
def note_correspondence(project):
    """テイクの音程ノート ↔ ガイドの音程ノート（DTW でテイク時間に写した位置で重なりを測る）。

    **音程の対応**（と画面でどのガイドノートを濃くするか）に使う。タイミングの目標には使わない
    （`analysis/guide_timing.py`。画面に描くガイドの位置は「ガイドの時刻 + 全体のずれ」）。

    ノートごとに「重なりが最大の相手」を両方向から取り、つながった組をまとめる。
    交差（i < i' なのに j > j'）と「1 つのガイドノートに複数」は同じ組に入るので、
    組の頭・尻どうしを合わせれば追い越しが起きない（以前のタイミングの規則。今は音程の対応だけ）。"""
    al = project.alignment
    tn = pitched_notes(project)
    gn = [g for g in project.guide_notes if g.kind == "note"]
    if al is None or not tn or not gn:
        return [], {}, {}
    g2t = al.to_take
    try:
        f, basis = project.guide_to_take()
        if basis.get("same_timeline"):
            g2t = f          # 同じ時間軸の素材: 画面に描く位置で重なりを測る（DTW が外れた所で
                             # 別のフレーズのガイドノートに対応させない）
    except Exception:        # noqa: BLE001
        pass
    gspan = [(float(g2t(g.start_sec)), float(g2t(g.end_sec))) for g in gn]

    def ov(n, j):
        return min(n.end_sec, gspan[j][1]) - max(n.start_sec, gspan[j][0])

    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    best_g = {}
    for i, n in enumerate(tn):
        j = max(range(len(gn)), key=lambda j: ov(n, j))
        if ov(n, j) > 1e-6:
            best_g[i] = j
            union(("t", i), ("g", j))
    for j in range(len(gn)):
        i = max(range(len(tn)), key=lambda i: ov(tn[i], j))
        if ov(tn[i], j) > 1e-6:
            union(("t", i), ("g", j))
    comps = {}
    for key in list(parent):
        comps.setdefault(find(key), []).append(key)
    groups = []
    for members in comps.values():
        ti = sorted(i for k, i in members if k == "t")
        gj = sorted(j for k, j in members if k == "g")
        if ti and gj:
            groups.append([ti[0], ti[-1], gj[0], gj[-1]])
    # 範囲が重なる組はまとめる（交差を消す）
    groups.sort()
    merged = []
    for g in groups:
        if merged and (g[0] <= merged[-1][1] or g[2] <= merged[-1][3]):
            m = merged[-1]
            merged[-1] = [min(m[0], g[0]), max(m[1], g[1]), min(m[2], g[2]), max(m[3], g[3])]
        else:
            merged.append(g)
    pairs = []
    for t0, t1, g0, g1 in merged:
        pairs.append({"take": [tn[i].id for i in range(t0, t1 + 1)],
                      "guide": [gn[j].id for j in range(g0, g1 + 1)],
                      "take_span": [tn[t0].start_sec, tn[t1].end_sec],
                      "guide_span": [gspan[g0][0], gspan[g1][1]]})
    pitch_target = {tn[i].id: gn[j] for i, j in best_g.items()}
    return pairs, pitch_target, {g.id: gspan[j] for j, g in enumerate(gn)}


def _guide_blend_midi(current_hz, guide_hz, weight, strength):
    """F0 を Hz で線形補間し、鉛筆が扱う MIDI 値へ変換する。"""
    hz = current_hz + float(strength) * weight * (guide_hz - current_hz)
    return float(69.0 + 12.0 * np.log2(hz / 440.0))


def _guide_pitch_shape(project, pairs, onset_pairs, selected, threshold_cents):
    """対応する有声フレームの Hz 目標と、計画で共有する基準値を作る。"""
    from .pitch import pitch_model, _midi_frames

    take = project.take_f0
    guide = project.guide_f0
    if take is None or guide is None:
        return [], [], {}
    tm = _midi_frames(take)
    gf0 = np.asarray(guide.f0, dtype=float)
    gv = np.asarray(guide.voiced, dtype=bool) & (gf0 > 0)
    gt = np.asarray(guide.times, dtype=float)
    _, _, layer, _ = pitch_model(project)
    tn = {n.id: n for n in pitched_notes(project)}
    gn = {n.id: n for n in project.guide_notes if n.kind == "note"}
    mapped = {}  # take frame -> (current Hz, mapped guide Hz, note id)

    for pair in pairs:
        takes = [tn[i] for i in pair["take"] if i in tn]
        guides = [gn[i] for i in pair["guide"] if i in gn]
        if not takes or not guides:
            continue
        ta, tb = takes[0].start_sec, takes[-1].end_sec
        ga, gb = guides[0].start_sec, guides[-1].end_sec
        if tb <= ta or gb <= ga:
            continue
        local_onsets = [op for op in onset_pairs
                        if ta - HEAD_SNAP_SEC <= op.take_sec <= tb + HEAD_SNAP_SEC
                        and ga - HEAD_SNAP_SEC <= op.guide_sec <= gb + HEAD_SNAP_SEC]
        if not local_onsets:
            continue  # #12 の発音の頭で対応を確かめられない組は写さない
        anchors = [(ta, ga)]
        # #12 の確かな発音の頭を対応させる。組内の残りは比例時間で補間する。
        for op in sorted(local_onsets, key=lambda x: x.take_sec):
            if ta < op.take_sec < tb and ga < op.guide_sec < gb:
                if op.take_sec > anchors[-1][0] and op.guide_sec > anchors[-1][1]:
                    anchors.append((op.take_sec, op.guide_sec))
        anchors.append((tb, gb))
        tx = np.array([a for a, _ in anchors])
        gx = np.array([b for _, b in anchors])
        for n in takes:
            if n.id not in selected:
                continue
            lo = max(0, int(np.ceil(n.start_sec / take.hop_s - 1e-9)))
            hi = min(len(tm), int(np.ceil(n.end_sec / take.hop_s - 1e-9)))
            for i in range(lo, hi):
                if not np.isfinite(tm[i]):
                    continue
                t = float(take.times[i])
                g = float(np.interp(t, tx, gx))
                j = int(np.searchsorted(gt, g))
                if j <= 0 or j >= len(gt):
                    continue
                # 無声の前後を補間して架空のガイド F0 を作らない。
                if not (gv[j - 1] and gv[j]):
                    continue
                current_midi = tm[i] + layer.at(t, "right") / 100.0
                current_hz = float(440.0 * 2.0 ** ((current_midi - 69.0) / 12.0))
                guide_hz = float(np.interp(g, gt[j - 1:j + 1], gf0[j - 1:j + 1]))
                cents = abs(1200.0 * np.log2(guide_hz / current_hz))
                if cents >= threshold_cents:
                    mapped[i] = (current_hz, guide_hz, n.id)

    if not mapped:
        return [], [], {}
    runs = []
    for i in sorted(mapped):
        if not runs or i != runs[-1][-1] + 1:
            runs.append([])
        runs[-1].append(i)
    frames, specs, by_note = [], [], {}
    for run in runs:
        if len(run) < 2:
            continue
        t0, t1 = float(take.times[run[0]]), float(take.times[run[-1]])
        points, local_frames, local_notes = [], [], []
        for i in run:
            t = float(take.times[i])
            # つなぎを対応区間の内側で作る。外側の無声・未対応フレームには触れない。
            w = min(1.0, max(0.0, (t - t0) / 0.04), max(0.0, (t1 - t) / 0.04))
            w = w * w * (3.0 - 2.0 * w)
            current_hz, guide_hz, nid = mapped[i]
            target_midi = _guide_blend_midi(current_hz, guide_hz, w, 1.0)
            current_midi = _guide_blend_midi(current_hz, guide_hz, w, 0.0)
            points.append([round(t, 6), round(target_midi, 6)])
            local_frames.append([t, current_hz, guide_hz, w])
            local_notes.append(nid)
        if not any(abs(_guide_blend_midi(h0, h1, w, 1.0)
                       - _guide_blend_midi(h0, h1, w, 0.0)) > 1e-6
                   for _, h0, h1, w in local_frames):
            continue
        frames.extend(local_frames)
        for (_, h0, h1, w), nid in zip(local_frames, local_notes):
            d = _guide_blend_midi(h0, h1, w, 1.0) - _guide_blend_midi(h0, h1, w, 0.0)
            by_note.setdefault(nid, []).append(d * 100.0)
        specs.append({"kind": "pitch_draw", "target": Target.range(t0, t1),
                      "params": {"points": points, "ramp_sec": 0.0}})
    # 対象ノートの一覧にも使う。線と色のプレビューは各フレームの Hz から計算する。
    return frames, specs, {nid: float(np.mean(vals)) for nid, vals in by_note.items()}


def plan_guide(project, note_ids=None, threshold_cents=0.0, threshold_ms=0.0,
               match_pitch_shape=True, interpolate=True, edited_notes="adjust"):
    """「ガイドに合わせる」の 100% の計画（x = タイミングの強度、ピッチは別の強度で掛ける）。

    - 音程: 既定は対応するガイド F0 を発音の頭どうしで写して、フレームごとの線を近づける。
      match_pitch_shape=False は従来どおりノートの中心を一定量ずらし、揺れの形を保つ。
    - タイミング: テイクの発音の頭（音の立ち上がり）を、1 対 1 に対応するガイドの発音の頭の
      **タイムライン上のガイドの時刻**へ（全体のずれが 150 ms を超える置き場所の違う素材だけ
      + 全体のずれ。`analysis/guide_timing.py`・issue #61。DTW で写した位置ではない:
      写した位置はテイク自身のリズムに沿うので、合わせてもリズムは直らない。issue #12）。
      対応の決まらない頭は動かさない。接続の規則で動かすのでリップルは無い。
    - 基準点（確かな頭の組）の間のノートは、前後の基準点に合わせて比例で動かす（interpolate。
      VocAlign と同じ。issue #53）。基準点が無い区間・基準点の間が 1 秒を超える所は動かさない。
      ノートごとの対応と理由は `plan.notes`（画面の対応線・未対応印、MCP の correspondence）。
    - しきい値（既定 0）はここで掛ける。プレビューも確定もこの計画を使うので、
      しきい値で両者がずれることは無い。
    - 音程は**今の（編集後の）音程**から寄せる（鉛筆・shift_pitch で直したノートも、直した線から
      残りの差だけ）。edited_notes="skip" は音程を直し済みのノートの音程を動かさない（タイミングは動かす）。"""
    if edited_notes not in ("adjust", "skip"):
        raise TimingError("edited_notes は adjust か skip")
    if project.guide is None or project.alignment is None:
        raise TimingError("ガイドが無い（open_project の guide_path → analyze_take）")
    from ..analysis import guide_timing as GT
    gt = GT.of_project(project)
    tn = {n.id: n for n in pitched_notes(project)}
    sel = set(note_ids) if note_ids else set(tn)
    st, tm = build_structure(project, attacks=_gap_attacks(project, gt, sel),
                             confirmed_only=True)
    pairs, pitch_target, gspan = note_correspondence(project)
    many = _one_to_many(project, tn, sel, gspan)
    # ---- 音程（今の編集後の音程から。直し済みを飛ばすときはそのノートを外す）
    from .pitch import edited_note_centers, pitch_edited_notes
    skipped = set()
    if edited_notes == "skip":
        skipped = pitch_edited_notes(project, [tn[i] for i in sel if i in tn])
    psel = sel - skipped
    curve, draws = [], []
    if match_pitch_shape:
        curve, draws, pitch = _guide_pitch_shape(project, pairs, gt.pairs if gt else [],
                                                  psel, threshold_cents)
    else:
        curp = edited_note_centers(project, [tn[i] for i in psel if i in tn])
        pitch = {}
        for nid, g in pitch_target.items():
            if nid not in psel or g.pitch_midi is None:
                continue
            cur = curp.get(nid)
            if cur is None:
                continue
            target = many[nid]["pitch_midi"] if nid in many else float(g.pitch_midi)
            c = (target - cur) * 100.0
            if abs(c) >= threshold_cents and abs(c) > 1e-6:
                pitch[nid] = c
    # ---- タイミング: テイクの発音の頭（ノートの頭）をガイドの頭（基準は gt.basis）へ（1 対 1 の組だけ）
    #      基準点の間のノートは前後の基準点に合わせて比例で（interpolate。issue #53）
    anchors, soft, timing, cover = _guide_anchors(project, st, tm, gt, sel, threshold_ms,
                                                  interpolate=interpolate)
    wanted = dict(anchors)
    anchors, repaired = _repair(st, anchors, soft=soft)
    used_pairs = []
    gnotes = {n.id: n for n in project.guide_notes if n.kind == "note"}
    for pr in pairs:
        if [i for i in pr["take"] if i in tn and i in sel]:
            used_pairs.append({"take": pr["take"], "guide": pr["guide"],
                               "confirmed": _pair_confirmed(pr, gt, tn, gnotes)})
    plan = _finish(project, "guide",
                   {"note_ids": sorted(sel) if note_ids else None,
                    "threshold_cents": threshold_cents, "threshold_ms": threshold_ms,
                    "match_pitch_shape": bool(match_pitch_shape),
                    "edited_notes": edited_notes},
                   st, anchors, pitch=pitch, pitch_curve=curve, pitch_draws=draws,
                   pairs=used_pairs)
    reach = 1.0
    if plan.x_hi < 1.0 - 1e-9:
        # 直しきれない（伸縮の比の上限など）ときは全体を縮めて 100% を届く範囲にする
        reach = max(0.0, plan.x_hi)
        plan.d = plan.d * reach
    plan.x_lo, plan.x_hi = 0.0, 1.0
    matched = {i for pr in used_pairs for i in pr["take"]}
    for e in timing:                     # 100% で目標に届くか（追い越し・短すぎで戻した頭は届かない）
        probe = 1e-6 if e.get("kind") == "end" else 0.0      # 音節の終わりは手前の極限
        e["reached"] = bool(abs(_pos_after(st, tm, plan.d, e["take_sec"] - probe)
                                - e["target_sec"]) < 2e-4 + probe)
    plan.timing = timing
    plan.notes = _note_correspondence_rows(project, st, plan, gt, used_pairs, sel & set(tn),
                                           wanted, soft, cover)
    rows = plan.notes
    plan.info = {"pairs": len(used_pairs), "matched_notes": len(matched & sel),
                 "unmatched_notes": sorted(sel - matched - (sel - set(tn))),
                 "confirmed_notes": sum(1 for r in rows if r["confirmed"]),
                 "timing_onsets": len(timing),
                 "timing_reached": sum(1 for e in timing if e["reached"]),
                 "timing_notes": sum(1 for r in rows if r["timing"]),
                 "timing_anchor_notes": sum(1 for r in rows if r["timing"] == "anchor"),
                 "timing_interp_notes": sum(1 for r in rows if r["timing"] == "interp"),
                 "timing_reached_notes": sum(1 for r in rows if r["reached"]),
                 "timing_possible": bool(gt is not None and gt.pairs),
                 "one_to_many": [dict(v, note=k) for k, v in sorted(many.items())],
                 "offset_ms": None if gt is None else round(gt.offset_sec * 1000.0, 1),
                 "measured_offset_ms": (None if gt is None
                                        else round(gt.measured_offset_sec * 1000.0, 1)),
                 "basis": None if gt is None else gt.basis,
                 "repaired": repaired, "reach": round(reach, 4),
                 "skipped_edited_notes": sorted(skipped),
                 "note": "タイミングは発音の頭（音の立ち上がり）を、タイムライン上のガイドの頭へ"
                         "（画面に描いているガイドの位置。置き場所の違う素材 = 全体のずれが 150 ms を"
                         "超えるときは「ガイドの頭 + 全体のずれ」へ）。1 対 1 に決まらない頭は動かさない。"
                         "基準点（確かな頭の組）の間のノートは前後の基準点に合わせて比例で動かし、"
                         "基準点の無い区間は動かさない"}
    if not plan.info["timing_possible"]:
        plan.info["timing_message"] = "このガイドとはタイミングを合わせられない（確かな発音の頭の組が無い）"
    return plan


MANY_MIN_SEC = 0.04         # 1 対多: テイクのノートとこれ以上重なるガイドのノートが 2 つ以上


def _one_to_many(project, tn, sel, gspan):
    """テイクの 1 ノートに、高さの違うガイドのノートが 2 つ以上重なる所（ガイドが動くのにテイクは 1 つの音）。

    {ノート: {"guide": [id], "pitch_midi": 寄せる先, "source"}}。寄せる先は、ノートの有声フレームでのガイドの F0 と
    テイクの F0 の差の中央値をノートの高さに足したもの（source = frames。両方有声のフレームが 5 未満なら、重なった時間で
    重み付けたガイドのノートの高さの中央値 = notes）。ノートの中心を一定量ずらす寄せ方（match_pitch_shape=False）は、
    組の 1 つのノート（いちばん重なる音）ではなくここへ寄せる（以前は組の最後の低い音へ寄せて −230 セント外した。
    補正の担当の報告）。"""
    out = {}
    gn = [g for g in project.guide_notes if g.kind == "note" and g.pitch_midi is not None and g.id in gspan]
    g2t_inv = _guide_inverse(project)
    for nid in sel:
        n = tn.get(nid)
        if n is None:
            continue
        ov = []
        for g in gn:
            a, b = gspan[g.id]
            o = min(n.end_sec, b) - max(n.start_sec, a)
            if o >= MANY_MIN_SEC:
                ov.append((float(g.pitch_midi), o, g.id))
        if len(ov) < 2 or max(v[0] for v in ov) - min(v[0] for v in ov) < 0.5:
            continue
        d = _frame_diff(project, n, g2t_inv) if n.pitch_midi is not None else None
        if d is not None:
            target = float(n.pitch_midi) + d                 # フレームごとの差（ガイド − テイク）の中央値
        else:
            ov.sort()
            w = np.cumsum([o for _, o, _ in ov])
            target = ov[int(np.searchsorted(w, w[-1] / 2.0))][0]   # 重なった時間で重み付けた高さの中央値
        out[nid] = {"guide": sorted(i for _, _, i in ov), "pitch_midi": round(target, 3),
                    "source": "frames" if d is not None else "notes"}
    return out


def _guide_inverse(project):
    """テイクの秒 → ガイドの秒（画面に描くガイドの位置の逆）。"""
    try:
        f, _ = project.guide_to_take()
    except Exception:        # noqa: BLE001
        al = project.alignment
        return lambda t: np.asarray(al.to_guide(t))
    gd = float(project.guide["frames"]) / float(project.guide["sr"])
    grid = np.arange(0.0, gd + 0.01, 0.01)
    tk = np.maximum.accumulate(np.asarray(f(grid), dtype="float64"))
    return lambda t: np.interp(t, tk, grid)


def _frame_diff(project, n, g2t_inv, min_frames=5):
    """テイクのノートの有声フレームで、ガイドの F0 − テイクの F0（半音）の中央値。両方有声のフレームが少なければ None。"""
    tf, gf = project.take_f0, project.guide_f0
    if tf is None or gf is None:
        return None
    hop = tf.hop_s
    a, b = int(round(n.start_sec / hop)), int(round(n.end_sec / hop))
    t = np.arange(a, b) * hop
    f = np.asarray(tf.f0, dtype="float64")[a:b]
    v = np.asarray(tf.voiced)[a:b].astype(bool) & (f > 0)
    gi = np.round(np.asarray(g2t_inv(t)) / gf.hop_s).astype(int)
    ok = v & (gi >= 0) & (gi < len(gf.f0))
    gi = np.clip(gi, 0, len(gf.f0) - 1)
    g = np.asarray(gf.f0, dtype="float64")[gi]
    ok &= np.asarray(gf.voiced).astype(bool)[gi] & (g > 0)
    if ok.sum() < min_frames:
        return None
    return float(np.median(12.0 * np.log2(g[ok] / f[ok])))


def correspondence_summary(plan):
    """「ガイドに合わせる」の計画の、ノートごとの対応と理由（MCP の結果用。画面は plan JSON の notes）。

    timing: anchor = 頭が基準点（確かな発音の頭の組）/ interp = 前後の基準点に合わせて比例 / None
    = タイミングは動かさない。reason: 対応が無い・タイミングが無い理由（あるときだけ）。"""
    rows = []
    for r in plan.notes:
        row = {"note": r["note"], "guide": r["guide"], "confirmed": r["confirmed"],
               "pitch": r["pitch"], "timing": r["timing"], "reached": r["reached"]}
        why = [x for x in (r["reason"], r["timing_reason"]) if x]
        if why:
            row["reason"] = " / ".join(why)
        rows.append(row)
    return rows


HEAD_SNAP_SEC = 0.06        # 発音の頭のすぐ前後にあるノートの頭（組の節）は一緒に動かす
# ---- 基準点と補間（issue #53。値は docs/guide-coverage.md の検査で決めた）
PHRASE_GAP_SEC = 0.5        # これ以上の隙間で区間（フレーズ）を分ける（基準点の比例はフレーズの中だけ）
INTERP_MAX_SPAN_SEC = 1.0   # 前後の基準点がこれより離れていたら比例で動かさない
SYLLABLE_ENDS = True        # 確定の歌詞の、休みの手前の音節の終わりも基準点に


def _pair_confirmed(pr, gt, tn, gn):
    """ノートの組（`note_correspondence`）が確かな発音の頭の組で裏付けられているか。
    tn / gn = {id: テイク・ガイドの音程ノート}。

    `_guide_pitch_shape` が音程の形を写す条件と同じ（組の範囲の中に 1 対 1 の頭の組がある）。"""
    if gt is None or not gt.pairs:
        return False
    takes = [tn[i] for i in pr["take"] if i in tn]
    guides = [gn[i] for i in pr["guide"] if i in gn]
    if not takes or not guides:
        return False
    ta, tb = takes[0].start_sec, takes[-1].end_sec
    ga, gb = guides[0].start_sec, guides[-1].end_sec
    return any(ta - HEAD_SNAP_SEC <= op.take_sec <= tb + HEAD_SNAP_SEC
               and ga - HEAD_SNAP_SEC <= op.guide_sec <= gb + HEAD_SNAP_SEC for op in gt.pairs)


def _onset_reason(project, gt, note):
    """発音の頭の組が無い理由（ノートの中の立ち上がりを `guide_timing` が捨てた理由）。"""
    if gt is None:
        return "ガイドとの対応付けが無い"
    if not gt.pairs:
        return "このガイドとは確かな発音の頭の組が 1 つも無い（別の演奏・中身の違うガイド）"
    if gt.source == "syllables":
        return "この区間に歌詞の音節の組が無い"
    on = list(project.onsets("take"))
    local = [(abs(x - note.start_sec), "o%04d" % k) for k, x in enumerate(on)
             if note.start_sec - HEAD_SNAP_SEC <= x <= note.end_sec + 0.02]
    if not local:
        return "テイクの発音の頭を検出できない"
    why = [(d, gt.skipped[i]) for d, i in local if i in gt.skipped]
    return min(why)[1] if why else "発音の頭はあるが 1 対 1 の組が無い"


def _note_correspondence_rows(project, st, plan, gt, used_pairs, sel, wanted, soft, cover):
    """選んだ音程ノートごとの対応（画面の対応線・未対応印と MCP の結果）。

    - guide / group: 音程の対応（`note_correspondence` の組。1 対多は同じ組の範囲）
    - confirmed: その組が確かな発音の頭の組で裏付けられている（画面は対応線を引く）
    - pitch: この計画で音程がガイドへ寄る
    - timing: "anchor"（頭が基準点）/ "interp"（前後の基準点に合わせて比例）/ None
    - reached: 100% で頭が目標に届く（追い越し・伸縮の上限で戻した頭は False）
    - reason / timing_reason: 対応が無い・タイミングが無い理由"""
    notes = {n.id: n for n in pitched_notes(project)}
    group_of = {}
    for gi, pr in enumerate(used_pairs):
        for nid in pr["take"]:
            group_of.setdefault(nid, gi)
    hard = {k for k in wanted if k not in soft}
    phrase = _phrases(project)
    phrase_hard = {phrase.get(st.knots[k].note) for k in hard if st.knots[k].note in phrase}
    rows = []
    for nid in sorted(sel, key=lambda i: notes[i].start_sec):
        if nid not in st.note_knots:
            continue
        n = notes[nid]
        ks, ke = st.note_knots[nid]
        u = st.knots[ks].unit
        head = sorted(set(st.units.get(u, []) if u is not None else [ks]) | set(range(ks, ke)))
        gi = group_of.get(nid)
        pr = used_pairs[gi] if gi is not None else None
        row = {"note": nid, "group": gi, "guide": list(pr["guide"]) if pr else [],
               "take_span": [round(n.start_sec, 4), round(n.end_sec, 4)],
               "confirmed": bool(pr and pr["confirmed"]),
               "pitch": nid in plan.pitch, "timing": None, "reached": None,
               "reason": None, "timing_reason": None}
        if pr is None:
            row["reason"] = "時間の重なるガイドのノートが無い（ガイドに声が無い所）"
        elif not pr["confirmed"]:
            row["reason"] = "時間の重なりだけの候補（確かな発音の頭の組で確かめられない）"
        tk = [k for k in head if k in hard]
        if tk:
            row["timing"] = "anchor"
            row["reached"] = all(abs(plan.d[k] - wanted[k]) < 2e-4 for k in tk)
        elif u in cover:
            k = next(k for k in head if k in wanted)
            row["timing"] = "interp"
            row["reached"] = bool(abs(plan.d[k] - wanted[k]) < 2e-4)
        elif phrase.get(nid) in phrase_hard:
            row["timing_reason"] = ("前後の基準点が %.0f 秒より離れている（比例で動かす根拠が弱い）: "
                                    % INTERP_MAX_SPAN_SEC + _onset_reason(project, gt, n))
        else:
            row["timing_reason"] = ("この区間（フレーズ）に確かな発音の頭の組が無い: "
                                    + _onset_reason(project, gt, n))
        rows.append(row)
    return rows


def _gap_attacks(project, gt, sel):
    """隙間（切り離された境目）の中で、次のノートの頭の `HEAD_SNAP_SEC` 以内の手前にある
    発音の頭 → そのノートのアタックの頭（{note id: 秒}）。音程は立ち上がりより少し遅れて
    付くので、ノートの頭の手前の隙間に発音の頭が落ちることがある（隙間の中身は動かないので、
    アタックに入れないと揃えられない）。"""
    if gt is None:
        return {}
    ns = pitched_notes(project)
    conn = connection_map(project)
    out = {}
    for pr in gt.pairs:
        t = pr.take_sec
        for a, b in zip([None] + ns[:-1], ns):
            if b.id not in sel or not (0.0 < b.start_sec - t <= HEAD_SNAP_SEC):
                continue
            if a is not None and (conn.get((a.id, b.id), True) or t <= a.end_sec + 0.01):
                continue
            out[b.id] = min(out.get(b.id, t), t)
    return out


def _pos_after(st, tm, d, t):
    """計画を 100%（x = 1）で当てたときの、編集前の秒 t の行き先（`realize` と同じ規則）。"""
    c = tm.at(t)
    for i, k in enumerate(st.knots):          # 節の上なら節の行き先（右側の極限）
        if abs(k.src - t) < 1e-7 and k.side == "right":
            return k.cur + d[i]
    for pc in st.pieces:
        ka, kb = st.knots[pc.a], st.knots[pc.b]
        if ka.src - 1e-9 <= t < kb.src - 1e-9:
            if pc.mode == "gap":
                return c                             # 隙間の中身は元の位置のまま
            ca, cb = ka.cur, kb.cur
            na, nb = ca + d[pc.a], cb + d[pc.b]
            if cb - ca <= 1e-12:
                return na
            return na + (c - ca) * (nb - na) / (cb - ca)
    return c


def _note_at(st, t):
    """編集前の秒 t を含むノート（アタックの頭 〜 尻）。無ければ None。"""
    for nid, (ks, ke) in st.note_knots.items():
        u = st.knots[ks].unit
        k0 = min(st.units[u]) if u is not None else ks
        if st.knots[k0].src - 1e-9 <= t < st.knots[ke].src:
            return nid
    return None


def _guide_anchors(project, st, tm, gt, sel, threshold_ms=0.0, interpolate=True):
    """ガイドの組（発音の頭どうし）→ 節の目標（編集後の秒での移動量）。

    - テイクの発音の頭 t をガイドの頭（`pr.target_sec`。タイムライン上の位置、置き場所の違う素材は
      + 全体のずれ）へ。t に節が無ければ足して合わせる
    - t のすぐ前後（`HEAD_SNAP_SEC`）にあるノートの頭（アタック・接続の境目＝組の節）は
      同じだけ動かす（頭の子音と立ち上がりを離さない）
    - 選んだノートの中の頭だけ。1 対 1 の組の無い頭には目標を付けない
    - もう揃っている頭（ずれがしきい値未満）は目標 0（その場に留める。隣の頭の移動に引きずられない）
    - 確定の歌詞がテイク・ガイドの両方にある所は、休みの手前の音節の終わりも基準点
      （`_syllable_end_pairs`。推定の読みは使わない）
    - 組の無いノートの頭・尻（音程の変わり目・切り離された尻）は、隙間を挟まない両隣の目標の
      間を線形に（片側だけなら同じノートの目標と同じだけ。`_neighbour_targets`）。interpolate
      なら、残りも同じフレーズの前後の基準点に合わせて比例で（`_interp_targets`。issue #53）。
      これらは**ゆずれる目標**（`soft`）: 追い越し・短すぎになるときは、こちらを先に戻す

    返り値: (目標, ゆずれる節, 基準点の一覧, {組: 補間の目標})。"""
    anchors, soft, timing = {}, set(), []
    if gt is None:
        return anchors, soft, timing, {}
    thr = max(float(threshold_ms or 0.0), 0.0) / 1000.0
    confirmed_starts = []
    if gt.source == "syllables":
        from ..phoneme.lyrics import confirmed_syllable_indices
        result = project.phonemes("take")
        if result is not None:
            confirmed = confirmed_syllable_indices(project.lyrics_entries("take"),
                                                    result.syllables)
            confirmed_starts = [s["start_sec"] for s in result.syllables
                                if s["index"] in confirmed]
    confirmed_notes = {_note_at(st, t) for t in confirmed_starts}

    def own_knots(nid):
        ks, ke = st.note_knots[nid]
        u = st.knots[ks].unit
        return sorted(set(range(ks, ke + 1)) | set(st.units[u] if u is not None else []))

    def exact_knot(nid, t):
        near = [k for k in own_knots(nid) if abs(st.knots[k].src - t) <= 1e-3
                and st.knots[k].unit not in st.fixed_units]
        return min(near, key=lambda k: abs(st.knots[k].src - t)) if near else None

    todo = []
    for pr in sorted(gt.pairs, key=lambda q: q.take_sec):
        nid = _note_at(st, pr.take_sec)
        if nid is not None and nid not in sel:
            # 選んだノートの頭のすぐ手前（前のノートの尻の中）にある発音の頭は、選んだノートの頭
            nxt = [n for n, (ks, _) in st.note_knots.items() if n in sel
                   and 0.0 < st.knots[ks].src - pr.take_sec <= HEAD_SNAP_SEC]
            nid = nxt[0] if nxt else None
        if nid is None:
            continue
        is_confirmed = (gt.source != "syllables" or
                        any(abs(pr.take_sec - t) <= 1e-3 for t in confirmed_starts))
        if not is_confirmed and nid in confirmed_notes:
            continue
        d = pr.target_sec - tm.at(pr.take_sec)
        if abs(d) < thr or abs(d) < 1e-9:
            d = 0.0                                  # もう揃っている頭（しきい値未満）はその場に留める
        t = pr.take_sec if is_confirmed else st.knots[st.note_knots[nid][0]].src
        todo.append((pr, nid, t, d))
    # 確定の歌詞の音素の境目: 休みの手前の音節の終わり（母音の尻）もガイドの音節の終わりへ（issue #53）
    ends = []
    for e in _syllable_end_pairs(project, gt):
        nid = _note_at(st, e["take_sec"] - 1e-4)
        if nid is None or nid not in sel:
            continue
        ks, ke = st.note_knots[nid]
        t = e["take_sec"]
        if not (st.knots[ks].src + MIN_BODY_SEC < t <= st.knots[ke].src + 1e-6):
            continue
        d = e["target_sec"] - tm.at(t, "left")
        if abs(d) < thr or abs(d) < 1e-9:
            d = 0.0
        ends.append((e, nid, t, d))
    # 発音の頭に節を足す（番号が変わるので、目標を決める前に全部足す）
    for pr, nid, t, d in todo + ends:
        ks, ke = st.note_knots[nid]
        if exact_knot(nid, t) is None and st.knots[ks].src < t < st.knots[ke].src:
            _insert_knot(st, tm, t, nid)
    used = {}
    for pr, nid, t, d in todo:
        k = exact_knot(nid, t)
        if k is None:
            k = st.note_knots[nid][0]                # アタックの中（隙間の側）: 頭の組ごと動かす
        key = st.knots[k].unit if st.knots[k].unit is not None else ("k", k)
        if key in used:
            continue                                 # 同じ節に 2 つ目の頭は付けない
        # すぐ前後のノートの頭（アタック・接続の境目 = 組の節）は一緒に同じだけ動かす
        mates = [kk for kk, kn in enumerate(st.knots)
                 if kn.unit is not None and kn.unit not in st.fixed_units and kn.note in sel
                 and kn.role in ("note_start", "attack") and abs(kn.src - t) <= HEAD_SNAP_SEC
                 and kn.unit not in used and kn.unit != st.knots[k].unit]
        used[key] = True
        anchors[k] = d
        timing.append(dict(pr.to_json(), note=nid, take_sec=round(t, 4),
                           target_sec=round(tm.at(t) + d, 4),
                           cur_sec=round(tm.at(t), 4), d_sec=round(d, 4)))
        for kk in mates:
            used[st.knots[kk].unit] = True
            anchors[kk] = d
    for e, nid, t, d in ends:
        k = exact_knot(nid, t)
        if k is None or k in anchors:
            continue
        key = st.knots[k].unit if st.knots[k].unit is not None else ("k", k)
        if key in used:
            continue
        used[key] = True
        anchors[k] = d
        timing.append(dict(e, note=nid, take_sec=round(t, 4), target_sec=round(tm.at(t, "left") + d, 4),
                           cur_sec=round(tm.at(t, "left"), 4), d_sec=round(d, 4)))
    hard = dict(anchors)
    extra, cover = _neighbour_targets(st, hard, sel)
    if interpolate:
        more, cover2 = _interp_targets(project, st, hard, sel, dict(extra))
        extra.update(more)
        cover.update(cover2)
    for k, v in extra.items():
        anchors[k] = v
        soft.add(k)
    return anchors, soft, timing, cover


def _syllable_end_pairs(project, gt):
    """両方に**確定の**歌詞があるとき（`gt.source == "syllables"`）、組になった音節のうち、
    テイク・ガイドとも後ろが休み（無音・息・終わり）の音節の終わりどうしの組。

    音節の頭の組（`build_syllables`）と同じずれ（`offset_sec`）で合わせる。子音と母音の境目は
    使わない（子音は長さを保つので、頭と別の目標を付けると伸び縮みできない）。推定の読み
    （#52）の音節は使わない。"""
    if gt is None or gt.source != "syllables" or not SYLLABLE_ENDS:
        return []
    from ..phoneme.lyrics import confirmed_syllable_indices
    from ..analysis.guide_timing import MAX_SHIFT_SEC
    rt, rg = project.phonemes("take"), project.phonemes("guide")
    if rt is None or rg is None:
        return []
    ok_t = confirmed_syllable_indices(project.lyrics_entries("take"), rt.syllables)
    ok_g = confirmed_syllable_indices(project.lyrics_entries("guide"), rg.syllables)

    def end_before_pause(res, i):
        sy = res.syllables[i]
        nxt = [p for p in res.phonemes if p.start_sec >= sy["end_sec"] - 1e-4]
        return not nxt or min(nxt, key=lambda p: p.start_sec).label in ("silence", "breath")

    out = []
    for pr in gt.pairs:
        if not (pr.take_id.startswith("s") and pr.guide_id.startswith("gs")):
            continue
        i, j = int(pr.take_id[1:]), int(pr.guide_id[2:])
        if not (i < len(rt.syllables) and j < len(rg.syllables)):
            continue
        if i not in ok_t or j not in ok_g:
            continue
        if not (end_before_pause(rt, i) and end_before_pause(rg, j)):
            continue
        te, ge = float(rt.syllables[i]["end_sec"]), float(rg.syllables[j]["end_sec"])
        if abs((te - ge) - pr.offset_sec) > MAX_SHIFT_SEC:
            continue
        out.append({"take": pr.take_id + "-end", "guide": pr.guide_id + "-end",
                    "take_sec": te, "guide_sec": ge, "offset_sec": round(pr.offset_sec, 4),
                    "target_sec": ge + pr.offset_sec, "mode": pr.mode, "kind": "end"})
    return out


def _phrases(project):
    """音程ノート id → 区間（フレーズ）の番号。`PHRASE_GAP_SEC` 以上の隙間で分ける。"""
    out, k, prev = {}, 0, None
    for n in pitched_notes(project):
        if prev is not None and n.start_sec - prev.end_sec >= PHRASE_GAP_SEC:
            k += 1
        out[n.id] = k
        prev = n
    return out


def _free_units(st, sel, done):
    """目標を付けてよい組（選んだノートの頭・尻。固定の端と、もう目標のある組を除く）→ 代表の節。"""
    out = {}
    for k, kn in enumerate(st.knots):
        if (kn.unit is None or kn.unit in st.fixed_units or kn.unit in done
                or kn.note not in sel or kn.unit in out):
            continue
        out[kn.unit] = k
    return out


def _interp_targets(project, st, hard, sel, near=None):
    """基準点の間にある、まだ目標の無い組の頭・尻を、前後の基準点に合わせて比例で動かす（issue #53）。

    `_neighbour_targets`（隙間を挟まない両隣の間の比例・同じノートの尻）の後に呼ぶ。near = その目標。
    VocAlign と同じく、同じ区間（`_phrases`）の基準点（hard と near）を今の時刻で結んだ折れ線の値を
    目標にする（区間を隙間ごと連続して伸び縮みさせる。隙間の中身は元の位置のまま端だけ動く。
    発音の頭が無いノートも一緒に動く）。区間の最初の基準点より前・最後の基準点より後ろは、その
    基準点と同じだけ（長さを保って平行に）。基準点が 1 つも無い区間は動かさない。
    前後の基準点が `INTERP_MAX_SPAN_SEC` より離れている所（端の外なら基準点からそれより遠い所）も
    動かさない: 比例は「基準点の間でずれがなめらかに変わる」という仮定で、1 秒を超えると歌い手の
    ずれは音ごとにばらばらになる（手元の曲で悪化が増えた。docs/guide-coverage.md）。
    返り値: ({節: 目標}, {組: 目標})。"""
    phrase = _phrases(project)
    near = near or {}
    ctrl = {**hard, **near}
    done = {st.knots[k].unit for k in ctrl if st.knots[k].unit is not None}
    extra, cover = {}, {}
    pts = {}
    for k, v in ctrl.items():
        nid = st.knots[k].note
        if nid in phrase:
            pts.setdefault(phrase[nid], []).append((st.knots[k].cur, float(v)))
    for ph in pts:
        pts[ph].sort()
    for u, k in _free_units(st, sel, done).items():
        P = pts.get(phrase.get(st.knots[k].note))
        if not P:
            continue
        c = st.knots[k].cur
        xs = [x for x, _ in P]
        j = int(np.searchsorted(xs, c))
        lo = xs[j - 1] if j > 0 else None
        hi = xs[j] if j < len(xs) else None
        if lo is None or hi is None:
            if abs(c - (hi if lo is None else lo)) > INTERP_MAX_SPAN_SEC:
                continue
        elif hi - lo > INTERP_MAX_SPAN_SEC:
            continue
        extra[k] = float(np.interp(c, xs, [d for _, d in P]))
        cover[u] = extra[k]
    return extra, cover


def _neighbour_targets(st, hard, sel):
    """組の無い頭・尻: 隙間を挟まない両隣の目標の間を線形に、片側だけなら同じノートの中の目標に
    揃える（切り離された尻 = 長さを保つ）。返り値: ({節: 目標}, {組: 目標})。"""
    modes = [pc.mode for pc in st.pieces]
    done = {st.knots[k].unit for k in hard if st.knots[k].unit is not None}

    def side(k, step):
        j = k + step
        while 0 <= j < len(st.knots):
            if modes[min(j, k) if step < 0 else j - 1] == "gap":
                return None
            if j in hard:
                return j
            j += step
        return None

    extra, cover = {}, {}
    for k, kn in enumerate(st.knots):
        if (kn.unit is None or kn.unit in st.fixed_units or kn.unit in done
                or kn.note not in sel):
            continue
        lft, rgt = side(k, -1), side(k, +1)
        if lft is not None and rgt is not None:
            c0, c1, c = st.knots[lft].cur, st.knots[rgt].cur, kn.cur
            w = (c - c0) / (c1 - c0) if c1 - c0 > 1e-9 else 0.0
            extra[k] = hard[lft] + w * (hard[rgt] - hard[lft])
        elif lft is not None and st.knots[lft].note == kn.note:
            extra[k] = hard[lft]
        elif rgt is not None and st.knots[rgt].note == kn.note and kn.role == "note_start":
            extra[k] = hard[rgt]
        else:
            continue
        done.add(kn.unit)
        cover[kn.unit] = extra[k]
    return extra, cover


def _repair(st, anchors, max_iter=400, soft=()):
    """100% で守れない制約（隣を追い越す・短すぎる・伸ばしすぎ）を、関わる節の目標を寄せて直す。

    破っている制約 a + sgn*(d[kb] - d[ka]) >= 0 ごとに、ちょうど守れるところまで
    d[kb] - d[ka] を戻す。両端とも目標のある節なら半分ずつ、片方だけならそちらを動かす。
    片方だけが**ゆずれる目標**（soft）なら、そちらだけを動かす。"""
    anchors = dict(anchors)
    key_of = {}
    for k in anchors:
        u = st.knots[k].unit
        for kk in (st.units[u] if u is not None else [k]):
            key_of[kk] = k
    repaired = 0
    for _ in range(max_iter):
        sol = solve(st, anchors)
        bad = [c for c in sol.constraints if c[0] + c[1] < -1e-9]
        if not bad:
            break
        a, b, why, ka, kb, sgn = min(bad, key=lambda c: c[0] + c[1])
        need = -(a + b) / sgn            # d[kb] - d[ka] をこれだけ変える
        fa, fb = key_of.get(ka), key_of.get(kb)
        if fa is not None and fb is not None and fa != fb and (fa in soft) != (fb in soft):
            if fb in soft:
                anchors[fb] += need
            else:
                anchors[fa] -= need
        elif fa is not None and fb is not None and fa != fb:
            anchors[fb] += need / 2.0
            anchors[fa] -= need / 2.0
        elif fb is not None:
            anchors[fb] += need
        elif fa is not None:
            anchors[fa] -= need
        else:
            break                        # 目標の無い節どうし（直せない）→ 呼び出し側が全体を縮める
        repaired += 1
    return anchors, repaired


# ================================================================ 確定（組み直し）
def realize(project, plan, x):
    """計画を x で確定したときの編集。(remove_ids, add_specs, info) を返す。"""
    x = float(np.clip(x, plan.x_lo, plan.x_hi))
    st = plan.st
    ks = st.knots
    new = plan.new_positions(x)
    moving = [i for i in range(len(ks)) if abs(new[i] - ks[i].cur) > 1e-9
              or (ks[i].note in plan.force_notes)]
    specs = []
    removes = []
    info = {"x": x, "moved_knots": len(moving)}
    if moving:
        tm = current_map(project)
        timing = [(e, project.edit_span(e)) for e in project.edits if e.kind in TIMING_KINDS]
        legacy = any(e.kind == "move" for e in project.edits)
        if legacy:
            wins = [[0.0, project.duration_sec, max(0, moving[0] - 1), min(len(ks) - 1, moving[-1] + 1)]]
        else:
            wins = _windows(ks, moving, timing)
        rm = set()
        for W0, W1, kL, kR in wins:
            rm |= _window_edits(timing, W0, W1)
            pc = [p for p in st.pieces if p.a >= kL and p.b <= kR]
            pts = tm.points(W0, "left", ks[kL].src, ks[kL].side)
            pts += _pieces_points(tm, st, pc, new, plan.force_notes)
            pts += tm.points(ks[kR].src, ks[kR].side, W1, "right")
            specs += _emit(_dedup(pts))
        removes = [e.id for e in project.edits if e.id in rm]
        info.update(window_sec=[round(wins[0][0], 4), round(wins[-1][1], 4)], removed=len(removes),
                    pieces=len(specs))
        if len(wins) > 1:
            info["windows_sec"] = [[round(w[0], 4), round(w[1], 4)] for w in wins]
    return removes, specs, info


def _window_edits(timing, W0, W1):
    """組み直す窓 [W0, W1] の中のタイミング編集の id。窓と重なるもの・窓の端の時刻の無音の挿入（長さ 0）。
    端で**接しているだけ**の編集（窓の外の区間の伸縮）は入れない（入れると、窓が隣の編集を伝って曲の端まで
    広がり、動かしていないノートの編集まで組み直す）。"""
    out = set()
    for e, (a, b) in timing:
        if b - a <= 1e-9:
            if W0 - 1e-9 <= a <= W1 + 1e-9:
                out.add(e.id)
        elif a < W1 - 1e-9 and b > W0 + 1e-9:
            out.add(e.id)
    return out


def _windows(ks, moving, timing):
    """動く節の塊ごとの組み直す窓 [[W0, W1, kL, kR], ...]（時刻順・重ならない）。

    塊 = 動く節の並び。窓はその両隣の動かない節まで（そのノートと隣）を、窓の端にまたがる編集の端まで広げる。
    離れた 2 か所を 1 回で動かしても、間の動かないノートの編集は組み直さない（組み直すと編集の切れ目が
    変わり、別々に再合成してつなぐ所が変わって、動かしていないノートの音が変わる）。窓が重なる・接するなら 1 つにする。"""
    n = len(ks)
    groups = []
    for i in moving:
        kL, kR = max(0, i - 1), min(n - 1, i + 1)
        if groups and kL <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], kR)
        else:
            groups.append([kL, kR])
    wins = [[ks[kL].src, ks[kR].src, kL, kR] for kL, kR in groups]
    while True:
        for w in wins:                                   # 窓の端にまたがる編集の端まで広げる
            while True:
                grow = False
                for _e, (a, b) in timing:
                    if b - a > 1e-9 and a < w[1] - 1e-9 and b > w[0] + 1e-9:
                        if a < w[0]:
                            w[0], grow = float(a), True
                        if b > w[1]:
                            w[1], grow = float(b), True
                if not grow:
                    break
        wins.sort(key=lambda w: w[0])
        merged = [wins[0]]
        for w in wins[1:]:
            last = merged[-1]
            if w[0] <= last[1] + 1e-9:                   # 重なる・接する（端の無音を 2 つの窓で組み直さない）
                last[1] = max(last[1], w[1])
                last[2], last[3] = min(last[2], w[2]), max(last[3], w[3])
            else:
                merged.append(w)
        if len(merged) == len(wins):
            return merged
        wins = merged


def _pieces_points(tm, st, pc, new, force_notes):
    """窓の中の piece の折れ点。動かない piece が続くところは今の時間写像の折れ点をそのまま使う
    （piece ごとに区切ると節の時刻で編集が切れ、別々に再合成する所が増えて音が変わる）。"""
    ks = st.knots
    pts = []
    i = 0
    while i < len(pc):
        p = pc[i]
        if p.mode != "gap" and p.note not in force_notes and _still(tm, ks, p, new):
            j = i
            while (j + 1 < len(pc) and pc[j + 1].mode != "gap" and pc[j + 1].note not in force_notes
                   and _still(tm, ks, pc[j + 1], new)):
                j += 1
            ka, kb = ks[pc[i].a], ks[pc[j].b]
            pts += tm.points(ka.src, ka.side, kb.src, kb.side)
            i = j + 1
            continue
        pts += _piece_points(tm, st, p, new, force=p.note in force_notes)
        i += 1
    return pts


def _still(tm, ks, p, new):
    """piece の両端の節が動かない（今の時間写像の位置のまま）。"""
    ka, kb = ks[p.a], ks[p.b]
    return (abs(float(new[p.a]) - tm.at(ka.src, ka.side)) < 1e-12
            and abs(float(new[p.b]) - tm.at(kb.src, kb.side)) < 1e-12)


def _piece_points(tm, st, p, new, force=False):
    ka, kb = st.knots[p.a], st.knots[p.b]
    na, nb = float(new[p.a]), float(new[p.b])
    if force and p.mode != "gap":
        return [(ka.src, na), (kb.src, nb)]           # 中の細かい伸縮は捨てる（原音に戻す）
    cur = tm.points(ka.src, ka.side, kb.src, kb.side)
    ca, cb = cur[0][1], cur[-1][1]
    if abs(na - ca) < 1e-12 and abs(nb - cb) < 1e-12:
        return cur
    if p.mode != "gap":
        L = cb - ca
        if L <= 1e-12:
            return [(ka.src, na), (kb.src, nb)]
        k = (nb - na) / L
        return [(s, na + (o - ca) * k) for s, o in cur]
    return _gap_points(tm, ka.src, kb.src, na, nb)


def _gap_points(tm, GL, GR, E, S):
    """切り離された隙間: 中身は元の位置のまま、端（E = 前のノートの尻、S = 次の頭）だけ動く。"""
    raw = tm.points(GL, "right", GR, "left")
    core = list(raw)
    while len(core) >= 2 and core[1][0] > core[0][0] and core[1][1] - core[0][1] < 1e-12:
        core.pop(0)                                   # 先頭の切り取り（平ら）
    while len(core) >= 2 and core[-1][0] > core[-2][0] and core[-1][1] - core[-2][1] < 1e-12:
        core.pop()                                    # 末尾の切り取り
    if len(core) < 2 or core[-1][1] - core[0][1] < 1e-12:
        off = 0.0
        core = [(GL, GL + off), (GR, GR + off)]
    cs = np.array([c[0] for c in core])
    co = np.array([c[1] for c in core])

    def a(x):
        if x <= cs[0]:
            return co[0] - (cs[0] - x)
        if x >= cs[-1]:
            return co[-1] + (x - cs[-1])
        return float(np.interp(x, cs, co))

    def ainv(o):
        if o <= co[0]:
            return cs[0] - (co[0] - o)
        if o >= co[-1]:
            return cs[-1] + (o - co[-1])
        return float(np.interp(o, co, cs))

    aL, aR = a(GL), a(GR)
    pts = [(GL, E)]
    if E < aL - EPS:
        pts.append((GL, aL))                          # 手前に無音
        x1 = GL
    elif E > aL + EPS:
        x1 = min(GR, ainv(E))                         # 手前を切り取る
        pts.append((x1, E))
    else:
        x1 = GL
    if S > aR + EPS:
        x2, tail = GR, [(GR, aR), (GR, S)]            # 後ろに無音
    elif S < aR - EPS:
        x2 = max(GL, ainv(S))                         # 後ろを切り取る
        tail = [(x2, S), (GR, S)]
    else:
        x2, tail = GR, [(GR, S)]
    if x1 < x2 - 1e-9:
        pts += [(float(s), float(o)) for s, o in core if x1 + 1e-12 < s < x2 - 1e-12]
        pts += tail
    else:
        pts = [(GL, E), (GR, E)] + ([(GR, S)] if S > E + EPS else [])
    return _dedup(pts)


def _emit(pts):
    """折れ点列 → 正規形の編集（stretch / crop / silence）。原音どおりの区間には何も出さない。"""
    from .model import STRETCH_RATIO_RANGE
    lo, hi = STRETCH_RATIO_RANGE
    specs = []
    for (s0, o0), (s1, o1) in zip(pts[:-1], pts[1:]):
        ds, do = s1 - s0, o1 - o0
        if ds > 1e-9:
            if abs(do - ds) < EPS:
                continue
            r = do / ds
            if do < EPS:
                specs.append({"kind": "crop", "target": Target.range(s0, s1), "params": {}})
            elif lo <= r <= hi:
                specs.append({"kind": "stretch", "target": Target.range(s0, s1),
                              "params": {"ratio": r}})
            else:
                specs.append({"kind": "crop", "target": Target.range(s0, s1), "params": {}})
                specs.append({"kind": "silence", "target": Target.range(s1, s1),
                              "params": {"sec": do}})
        elif do > EPS:
            specs.append({"kind": "silence", "target": Target.range(s0, s0),
                          "params": {"sec": do}})
    return specs


def connection_specs(project, changes):
    """[(a, b, connected)] → (外す connection 編集の id, 入れる connection 編集)。

    編集リストに置くのは**既定と違うものだけ**。既定に戻すときは上書きを外すだけにする。"""
    removes, out = [], []
    ns = {n.id: n for n in project.take_notes if n.kind == "note" or n.kind in EXTRA_KINDS}
    last = {}
    for a, b, c in changes:                  # 同じ組は最後の値だけ（Alt で切って吸着で戻す、など）
        last[(a, b)] = c
    for (a, b), c in last.items():
        if a not in ns or b not in ns:
            continue
        removes += [e.id for e in project.edits if e.kind == "connection"
                    and connection_pair(project, e) == (a, b)]
        x, y = ns[a], ns[b]
        # 子音・息を含む組（issue #35）の既定は、挟んでいる音程ノートの組の接続から
        dflt = (default_connected(project, x, y) if x.kind == "note" and y.kind == "note"
                else extra_default(project, x, y))
        if bool(c) == dflt:
            continue
        out.append({"kind": "connection",
                    "target": Target.range(ns[a].end_sec, ns[b].start_sec),
                    "params": {"a": a, "b": b, "connected": bool(c)}})
    return removes, out


# ================================================================ まとめて使う口
def apply_plan(project, plan, x=None, pitch=None, author="human", label=None):
    """計画を確定する（1 つの changeset）。x = タイミング、pitch = ピッチの強度（ガイドのみ）。"""
    removes, specs, info = ([], [], {"x": 0.0})
    if x is not None and abs(float(x)) > 0:
        removes, specs, info = realize(project, plan, x)
    xs = info.get("x", 0.0)
    conn = []
    if plan.set_connections and abs(xs) > 1e-12:
        conn += plan.set_connections
    if (plan.snap_pair and plan.snap_x is not None and abs(xs) > 1e-9
            and abs(xs - plan.snap_x) < 1e-6):
        conn.append((plan.snap_pair[0], plan.snap_pair[1], True))
        info["snapped"] = True
    c_rm, c_add = connection_specs(project, conn)
    removes = removes + [i for i in c_rm if i not in removes]
    specs = specs + c_add
    if pitch and plan.pitch_draws:
        hz_at = {round(t, 6): (h0, h1, w) for t, h0, h1, w in plan.pitch_curve}
        for spec in plan.pitch_draws:
            points = []
            for t, _ in spec["params"]["points"]:
                h0, h1, w = hz_at[round(t, 6)]
                points.append([t, round(_guide_blend_midi(h0, h1, w, float(pitch)), 6)])
            specs.append({"kind": "pitch_draw", "target": spec["target"],
                          "params": {"points": points, "ramp_sec": 0.0}})
    elif pitch:
        for nid, c in plan.pitch.items():
            v = c * float(pitch)
            if abs(v) > 1e-6:
                specs.append({"kind": "pitch_shift", "target": Target.note(nid),
                              "params": {"cents": v},
                              "note": "ガイドへ %.0f%%" % (float(pitch) * 100)})
    if not specs and not removes:
        return None, info
    # 「ガイドに合わせる」は自動、ほか（端・移動のドラッグ・オリジナルに戻す）は手動（issue #37。project/correction.py）
    cs = project.apply_changes(removes, specs, author=author,
                               label=label or "%s（%s）" % (plan.kind, plan.id),
                               origin="auto" if plan.kind == "guide" else "manual")
    info["changeset"] = cs.id
    log.get().info("plan %s を確定: x=%.4f 外した %d / 入れた %d", plan.id, xs,
                   len(removes), len(specs))
    return cs, info


def write_plan(project, plan, path=None):
    """計画を JSON ファイルに書く（画面が読む。MCP の結果に数値列を載せないため）。"""
    d = project.sub("cache")
    path = path or os.path.join(d, "plan-%s.json" % plan.id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan.to_json(), f, ensure_ascii=False, separators=(",", ":"))
    from .store import replace_file
    replace_file(tmp, path)
    return os.path.abspath(path)
