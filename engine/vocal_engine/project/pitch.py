# -*- coding: utf-8 -*-
"""ピッチの重ね（層）— **ノートの変わり目のなだらかさ**と**鉛筆で描いたピッチ**。

ピッチのずらし量（セント、編集前の時間軸）は 3 段で決まる:

    1. 基本: `pitch_shift` / `pitch_curve`（`render/pipeline.py: edits_to_segments`）
    2. つなぎ（transition）: 接続された境目の**移動量の差の段差**を、なだらかな曲線で渡す
    3. 鉛筆（pitch_draw）: 描いた範囲をその音程に置き換える（編集リストの順。後から描いた方が上）

## 2. つなぎ（Melodyne のピッチトランジション相当）

つなぎは**基本のずらし量に段差がある時刻**に掛かる（issue #6。以前は「接続された境目」に
持っていたので、結合で境目が消えると段差だけが残って音が変わった）。段差の置き場は 2 通り:

- **接続された隣り合う音程ノート a → b の境目**（a の尻 ta、b の頭 tb。隙間なしなら ta = tb）。
  Δ = [ta, tb] の中の段差の合計（隙間の中で曲線が続いているぶんは段差に数えない）
- **ノートの中の段差**（結合した元の境目・範囲の編集の端など。kind = "step"）。ta = tb = その時刻

どちらも Δ を、窓 [ta − hl, tb + hr] の raised-cosine S(t)（0 → 1）で渡す:

    off'(t) = off(t) + (S(t) − H(t)) · Δ        H = 0（a 側）/ 1（b 側）

- **原音の移り変わりの形はそのまま**（足すのは移動量の差の補間だけ。Δ = 0 なら何もしない
  ＝ピッチを動かしていない境目は原音のまま）
- 切り離された境目・音程のあるノートの外には効かせない。隙間（無声）の中は触らない
- なだらかさの値（`transition` 編集）は**時刻で**引く（編集の範囲が段差の時刻に重なるもの）。
  分割・結合でノートの id が変わっても、同じ段差に同じ値が掛かる
- 窓の幅 w: なだらかさ v（0〜1、既定 0.5 = 自動）から `width_for`。自動の幅は**原音の境目で
  実際に音程が移り変わっている長さ**（F0 の 10〜90% の区間 ÷ 0.8、30〜250 ms）。
  段差の無い境目（同じ音程を分割した等）は 80 ms。hl, hr はそれぞれのノートの長さの半分まで

線形（Δ に比例）なので、画面はノートのドラッグ中に同じ式で描ける
（`app/renderer/state.js: editedCurve`）。

## 3. 鉛筆

描いた範囲 [t0, t1] の中は描いた音程 D(t)（MIDI）に置き換える: off = (D(t) − 原音(t))·100。
両端の外側 `ramp_sec`（既定 40 ms）は重み w（raised-cosine）で元の曲線からつなぐ:

    off' = (1 − w) · off + w · target,   target = (D(端) − 原音(端))·100（端の値で固定）

鉛筆より**後**に入ったピッチ編集（ノートのドラッグなど）は描いた線にも足す（描いた線ごと動く）。
無声のところは音程が無いので効かない → 範囲は有声のフレームに切り詰める（画面も描かない）。

## 出口

`layered_segments(project)` が層を当てた Segment 列を返す（再合成・書き出し・画面の曲線はこれ）。
**ノートの中心の音程**（blob の高さ・ガイドに合わせるの基準）は基本の段だけで測る
（つなぎのカーブでノートの高さがずれて見えないように）。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

import numpy as np

from .. import log
from ..analysis.f0 import hz_to_midi
from .model import Target

DEFAULT_VALUE = 0.5            # なだらかさの既定（自動）
DEFAULT_AUTO_SEC = 0.08        # 原音に段差が無い境目の自動の幅
AUTO_MIN_SEC, AUTO_MAX_SEC = 0.03, 0.25
SLOW_MIN_SEC = 0.40            # 右端（v = 1）の幅の下限
SLOW_FACTOR = 3.0              # 右端の幅 = max(SLOW_MIN_SEC, 自動 × これ)
HALF_MAX_OF_NOTE = 0.5         # 窓の片側はノートの長さのこれだけまで
DRAW_RAMP_SEC = 0.04
GRID_SEC = 0.005               # 曲線を点にする刻み（F0 の 10 ms のフレームを必ず含む）
EPS_CENTS = 0.1               # これより小さい差は段差として扱わない（聞き分けられない）
MATCH_TOL_SEC = 5e-4          # なだらかさの値を段差の時刻に引くときの許容
AUTO_CACHE_MAX = 4096         # 自動の幅のキャッシュの件数の上限（最近使ったもの優先）
PITCH_KINDS = ("pitch_shift", "pitch_curve")


class PitchError(RuntimeError):
    pass


# ================================================================ つなぎ
def width_for(value, auto_sec):
    """なだらかさ v（0〜1）→ 窓の幅（秒）。0 = 段差、0.5 = 自動、1 = ゆっくり。"""
    v = min(1.0, max(0.0, float(value)))
    a = float(auto_sec)
    if v <= 0.5:
        return a * v / 0.5
    slow = max(SLOW_MIN_SEC, a * SLOW_FACTOR)
    return a + (slow - a) * (v - 0.5) / 0.5


def _midi_frames(f0r):
    f0 = np.asarray(f0r.f0, dtype="float64")
    ok = np.asarray(f0r.voiced, dtype=bool) & (f0 > 0)
    m = np.full(len(f0), np.nan)
    m[ok] = hz_to_midi(f0[ok])
    return m


def auto_width(f0r, a, b, midi=None, lo=None, hi=None):
    """原音の境目で音程が移り変わっている長さ（秒）。F0 の 10〜90% の区間から。

    lo / hi: 測る範囲の端（既定は a の頭・b の尻）。`transitions` は**解析のノート**の端を渡す
    （ユーザーが分割しても、既存の境目の自動の幅が変わらないように）。"""
    hop = f0r.hop_s
    if b.start_sec - a.end_sec > 2 * hop:
        return DEFAULT_AUTO_SEC, "gap"
    m = _midi_frames(f0r) if midi is None else midi
    tb = 0.5 * (a.end_sec + b.start_sec)
    from dataclasses import replace as _rep
    a = _rep(a, start_sec=a.start_sec if lo is None else lo)
    b = _rep(b, end_sec=b.end_sec if hi is None else hi)

    def med(s, e):
        i0, i1 = max(0, int(round(s / hop))), min(len(m), int(round(e / hop)))
        v = m[i0:i1]
        v = v[np.isfinite(v)]
        return float(np.median(v)) if len(v) >= 2 else None

    A = med(max(a.start_sec, tb - 0.25), max(a.start_sec + hop, tb - 0.05)) \
        or med(a.start_sec, a.end_sec)
    B = med(min(b.end_sec - hop, tb + 0.05), min(b.end_sec, tb + 0.25)) \
        or med(b.start_sec, b.end_sec)
    if A is None or B is None or abs(B - A) < 0.5:
        return DEFAULT_AUTO_SEC, "flat"
    lo_i = max(0, int(round(max(a.start_sec, tb - 0.3) / hop)))
    hi_i = min(len(m) - 1, int(round(min(b.end_sec, tb + 0.3) / hop)))
    if hi_i - lo_i < 3:
        return DEFAULT_AUTO_SEC, "short"
    p = (m[lo_i:hi_i + 1] - A) / (B - A)
    ts = (np.arange(lo_i, hi_i + 1)) * hop
    fin = np.isfinite(p)
    if fin.sum() < 3:
        return DEFAULT_AUTO_SEC, "unvoiced"
    # 境目にいちばん近い 50% の横切り
    cross = [k for k in range(len(p) - 1) if fin[k] and fin[k + 1]
             and (p[k] - 0.5) * (p[k + 1] - 0.5) <= 0]
    if not cross:
        return DEFAULT_AUTO_SEC, "no-cross"
    c = min(cross, key=lambda k: abs(ts[k] - tb))
    k0 = c
    while k0 > 0 and fin[k0 - 1] and p[k0 - 1] > 0.1:
        k0 -= 1
    k1 = c + 1
    while k1 < len(p) - 1 and fin[k1 + 1] and p[k1 + 1] < 0.9:
        k1 += 1
    w = (ts[k1] - ts[k0] + hop) / 0.8
    return float(min(AUTO_MAX_SEC, max(AUTO_MIN_SEC, w))), "measured"


@dataclass
class Transition:
    a: str
    b: str
    ta: float
    tb: float
    value: float
    auto_sec: float
    auto_how: str
    max_hl: float
    max_hr: float
    hl: float = 0.0
    hr: float = 0.0
    delta: float = 0.0          # 基本の段のずらし量の段差（セント）
    set_by_user: bool = False
    kind: str = "boundary"      # "boundary"（接続された境目）/ "step"（ノートの中の段差。a = b）

    def __post_init__(self):
        self.set_value(self.value)

    def set_value(self, v):
        self.value = float(v)
        w = width_for(self.value, self.auto_sec)
        self.hl = min(0.5 * w, self.max_hl)
        self.hr = min(0.5 * w, self.max_hr)

    @property
    def lo(self):
        return self.ta - self.hl

    @property
    def hi(self):
        return self.tb + self.hr

    @property
    def active(self):
        return self.hl + self.hr > 1e-6 and abs(self.delta) > EPS_CENTS

    def s_of(self, t):
        L = self.hi - self.lo
        if t <= self.lo:
            return 0.0
        if t >= self.hi:
            return 1.0
        return 0.5 - 0.5 * math.cos(math.pi * (t - self.lo) / L)

    def term(self, t, side):
        """off に足す量（セント）。窓の外・隙間の中は 0。"""
        if t < self.lo or t > self.hi:
            return 0.0
        if self.ta < self.tb and (self.ta < t < self.tb or (t == self.ta and side == "right")
                                  or (t == self.tb and side == "left")):
            return 0.0
        h = 1.0 if (t > self.tb or (t == self.tb and side == "right")) else 0.0
        return (self.s_of(t) - h) * self.delta

    def to_json(self):
        r = lambda v, n=6: round(float(v), n)          # noqa: E731
        return {"a": self.a, "b": self.b, "ta": r(self.ta), "tb": r(self.tb),
                "value": r(self.value, 4), "auto_sec": r(self.auto_sec, 4),
                "auto_how": self.auto_how, "max_hl": r(self.max_hl), "max_hr": r(self.max_hr),
                "hl": r(self.hl), "hr": r(self.hr), "delta": r(self.delta, 3),
                "user": self.set_by_user, "kind": self.kind}


def transition_edits(project):
    """編集リストの `transition`（順に。後勝ち）。[(edit, 開始秒, 終了秒)]"""
    return [(e, float(e.target.start_sec), float(e.target.end_sec))
            for e in project.edits if e.kind == "transition"]


def _matches(e, s, t, tr):
    """なだらかさの編集 e（範囲 [s, t]）が段差 tr に掛かるか。**時刻で**見る（範囲が重なる）。
    境目のものは、解析し直しで境目が少しずれたときのために id の組でも引く（以前の持ち方）。"""
    if s <= tr.tb + MATCH_TOL_SEC and t >= tr.ta - MATCH_TOL_SEC:
        return True
    return tr.kind == "boundary" and (e.params.get("a"), e.params.get("b")) == (tr.a, tr.b)


def transition_values(project):
    """編集リストの `transition`（後勝ち）。{(a, b): value}（id の組。一覧・互換用）"""
    out = {}
    for e in project.edits:
        if e.kind == "transition":
            out[(e.params["a"], e.params["b"])] = float(e.params["value"])
    return out


def _auto_cache(project, f0r):
    """自動の幅のキャッシュ（件数に上限。最近使ったもの優先）。F0 が変わったら作り直す。"""
    from collections import OrderedDict
    cache = getattr(project, "_auto_width_cache", None)
    if cache is None or cache[0] is not f0r:
        cache = (f0r, OrderedDict(), _midi_frames(f0r))
        project._auto_width_cache = cache
    return cache


def _auto_width_cached(cache, f0r, a, b, lo, hi):
    key = (lo, a.end_sec, b.start_sec, hi)
    d = cache[1]
    if key in d:
        d.move_to_end(key)
        return d[key]
    v = auto_width(f0r, a, b, cache[2], lo=lo, hi=hi)
    d[key] = v
    while len(d) > AUTO_CACHE_MAX:
        d.popitem(last=False)
    return v


def forget_cache(project):
    """プロジェクトを閉じた（編集対象を切り替えた）: 自動の幅のキャッシュを捨てる。"""
    if project is not None and getattr(project, "_auto_width_cache", None) is not None:
        project._auto_width_cache = None


def _base_index(project):
    from ..render.pipeline import edits_to_segments
    return _SegIndex(edits_to_segments(project.edits, project.edit_span))


def transitions(project, overrides=None, base=None):
    """つなぎの一覧（`Transition`。delta まで入れる）。overrides は `connections` と同じ。

    base: 基本の段（`_SegIndex`）。省くと編集リストから作る。"""
    from dataclasses import replace as _rep
    from .timing import connections
    f0r = project.take_f0
    if base is None:
        base = _base_index(project)
    user = transition_edits(project)
    cache = _auto_cache(project, f0r)
    analysis = [n for n in project.analysis_notes if n.kind == "note"]

    def outer(t, pick):
        for n in analysis:
            if n.start_sec - 1e-9 <= t <= n.end_sec + 1e-9:
                return pick(n)
        return None

    def make(a, b, a_id, b_id, kind, delta):
        lo = outer(a.end_sec - 1e-4, lambda n: n.start_sec)
        hi = outer(b.start_sec + 1e-4, lambda n: n.end_sec)
        lo = a.start_sec if lo is None else min(lo, a.start_sec)
        hi = b.end_sec if hi is None else max(hi, b.end_sec)
        aw, how = _auto_width_cached(cache, f0r, a, b, lo, hi)
        # 窓の片側の上限（ノートの長さの半分）も**解析のノート**で測る。分割した片で測ると、
        # 分割しただけで既存の境目の窓が縮んで音が変わる（頭から 50 ms で分割して 33 セント）。
        # ただし 1 つの解析のノートの中の段差（分割した境目・ノートの中の段差）は、分けた片の長さで測る
        oa = outer(a.end_sec - 1e-4, lambda n: n)
        same = oa is not None and oa is outer(b.start_sec + 1e-4, lambda n: n)
        la = (a.end_sec - a.start_sec) if same else (a.end_sec - lo)
        lb = (b.end_sec - b.start_sec) if same else (hi - b.start_sec)
        tr = Transition(a=a_id, b=b_id, ta=float(a.end_sec), tb=float(b.start_sec),
                        value=DEFAULT_VALUE, auto_sec=aw, auto_how=how,
                        max_hl=HALF_MAX_OF_NOTE * la, max_hr=HALF_MAX_OF_NOTE * lb,
                        delta=float(delta), kind=kind)
        v = None
        for e, s, t in user:
            if _matches(e, s, t, tr):
                v = float(e.params["value"])
        if v is not None:
            tr.set_value(v)
            tr.set_by_user = True
        return tr

    out = []
    for a, b, conn, _ in connections(project, overrides):
        if not conn:
            continue
        d = sum(j for _, j in base.jumps(a.end_sec, b.start_sec))
        out.append(make(a, b, a.id, b.id, "boundary", d))
    # ノートの中の段差（結合した元の境目・範囲の編集の端）。ノートの端のものは上の境目が受け持つ
    for n in sorted([n for n in project.take_notes if n.kind == "note"], key=lambda n: n.start_sec):
        for x, j in base.jumps(n.start_sec + 1e-4, n.end_sec - 1e-4):
            out.append(make(_rep(n, end_sec=x), _rep(n, start_sec=x), n.id, n.id, "step", j))
    return out


# ================================================================ 鉛筆
@dataclass
class Draw:
    id: str
    t0: float
    t1: float
    pts_t: np.ndarray
    pts_m: np.ndarray
    ramp: float
    later: object = None           # _SegIndex（この鉛筆より後のピッチ編集）
    orig: object = None            # (times, midi)（有声のフレームだけ）
    ramp_l: float = None           # 左右のつなぎの長さ（既定 = ramp。「オリジナルに戻す」で切った側は 0）
    ramp_r: float = None

    def __post_init__(self):
        if self.ramp_l is None:
            self.ramp_l = self.ramp
        if self.ramp_r is None:
            self.ramp_r = self.ramp

    @property
    def lo(self):
        return self.t0 - self.ramp_l

    @property
    def hi(self):
        return self.t1 + self.ramp_r

    def w(self, t):
        if self.t0 <= t <= self.t1:
            return 1.0
        if t <= self.lo or t >= self.hi:
            return 0.0
        u = (t - self.lo) / self.ramp_l if t < self.t0 else (self.hi - t) / self.ramp_r
        return 0.5 - 0.5 * math.cos(math.pi * u)

    def target(self, t, side):
        tt = min(self.t1, max(self.t0, t))
        d = float(np.interp(tt, self.pts_t, self.pts_m))
        o = float(np.interp(tt, self.orig[0], self.orig[1]))
        v = (d - o) * 100.0
        if self.later is not None:
            v += self.later.at(t, side)
        return v


def _orig_track(f0r):
    m = _midi_frames(f0r)
    t = np.asarray(f0r.times, dtype="float64")
    ok = np.isfinite(m)
    if not ok.any():
        return t[:1], np.array([60.0])
    return t[ok], m[ok]


def draws(project, edits=None):
    edits = project.edits if edits is None else edits
    from ..render.pipeline import edits_to_segments
    f0r = project.take_f0
    orig = _orig_track(f0r)
    out = []
    for i, e in enumerate(edits):
        if e.kind != "pitch_draw":
            continue
        pts = np.asarray(e.params["points"], dtype="float64")
        later = [x for x in edits[i + 1:] if x.kind in PITCH_KINDS]
        idx = _SegIndex(edits_to_segments(later, project.edit_span)) if later else None
        out.append(Draw(id=e.id, t0=float(e.target.start_sec), t1=float(e.target.end_sec),
                        pts_t=pts[:, 0], pts_m=pts[:, 1],
                        ramp=float(e.params.get("ramp_sec", DRAW_RAMP_SEC)),
                        ramp_l=_opt(e.params.get("ramp_l_sec")),
                        ramp_r=_opt(e.params.get("ramp_r_sec")),
                        later=idx, orig=orig))
    return out


def _opt(v):
    return None if v is None else float(v)


def voiced_span(project, t0, t1):
    """[t0, t1] の中の最初と最後の有声フレームの秒（無ければ None）。"""
    f0r = project.take_f0
    m = _midi_frames(f0r)
    hop = f0r.hop_s
    i0 = max(0, int(math.ceil(t0 / hop - 1e-9)))
    i1 = min(len(m) - 1, int(math.floor(t1 / hop + 1e-9)))
    ok = [i for i in range(i0, i1 + 1) if np.isfinite(m[i])]
    if not ok:
        return None
    return ok[0] * hop, ok[-1] * hop


def draw_specs(project, points, ramp_sec=DRAW_RAMP_SEC):
    """鉛筆の編集。points = [[素材の秒, MIDI], ...]。(外す id, 入れる spec, info)

    範囲は有声のフレームに切り詰める（無声は音程が無いので描いても効かない）。
    この範囲に**すっぽり入る**以前の鉛筆は外す（上から描き直した）。"""
    pts = sorted([[float(t), float(m)] for t, m in points], key=lambda q: q[0])
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1e-3:
        raise PitchError("描いた線が短すぎる（2 点以上・1 ms 以上）")
    vs = voiced_span(project, pts[0][0], pts[-1][0])
    if vs is None:
        raise PitchError("描いた範囲に音程のある（有声の）ところが無い。無声には描けない")
    t0, t1 = vs
    if t1 - t0 < 1e-6:
        t1 = t0 + 1e-3
    tt = np.array([p[0] for p in pts])
    mm = np.array([p[1] for p in pts])
    inner = [[t, m] for t, m in pts if t0 < t < t1]
    clipped = ([[t0, float(np.interp(t0, tt, mm))]] + inner
               + [[t1, float(np.interp(t1, tt, mm))]])
    rm = [e.id for e in project.edits if e.kind == "pitch_draw"
          and float(e.target.start_sec) >= t0 - 1e-9 and float(e.target.end_sec) <= t1 + 1e-9]
    spec = {"kind": "pitch_draw", "target": Target.range(t0, t1),
            "params": {"points": [[round(t, 6), round(m, 4)] for t, m in clipped],
                       "ramp_sec": float(ramp_sec)}}
    return rm, [spec], {"start_sec": round(t0, 4), "end_sec": round(t1, 4),
                        "points": len(clipped), "replaced": len(rm),
                        "clipped": [round(pts[0][0], 4), round(pts[-1][0], 4)] != [round(t0, 4), round(t1, 4)]}


def trim_draws(project, spans):
    """「オリジナルに戻す」: spans（編集前の秒の区間）にかかる鉛筆を、外側の部分だけ残して切る。
    (外す id, 入れる spec)"""
    rm, add = [], []
    for e in project.edits:
        if e.kind != "pitch_draw":
            continue
        a, b = float(e.target.start_sec), float(e.target.end_sec)
        ramp = float(e.params.get("ramp_sec", DRAW_RAMP_SEC))
        rl0 = float(e.params.get("ramp_l_sec", ramp))
        rr0 = float(e.params.get("ramp_r_sec", ramp))
        # 両端のつなぎ（ramp）が戻すノートに入り込むものも対象（入り込んだぶんは縮める）
        near = [(s, t) for s, t in spans if t > a - rl0 + 1e-9 and s < b + rr0 - 1e-9]
        if not near:
            continue
        hit = [(s, t) for s, t in near if t > a and s < b]
        pts = e.params["points"]
        tt = np.array([p[0] for p in pts])
        mm = np.array([p[1] for p in pts])
        keep = [(a, b)]
        for s, t in hit:
            nxt = []
            for x, y in keep:
                if s > x:
                    nxt.append((x, min(y, s)))
                if t < y:
                    nxt.append((max(x, t), y))
            keep = [(x, y) for x, y in nxt if y - x > 0.02]
        new = []
        for x, y in keep:
            cut_l, cut_r = x > a, y < b
            if cut_l or cut_r:
                # 戻すノートの端のフレームは含めない（そのノートは原音）
                vs = voiced_span(project, x + (1e-6 if cut_l else 0.0), y - (1e-6 if cut_r else 0.0))
                if vs is None or vs[1] - vs[0] < 0.02:
                    continue
                x, y = vs
            # 切った側はつなぎ無し。切っていない側も、つなぎが戻すノートに入らない長さまで
            rl = 0.0 if cut_l else rl0
            rr = 0.0 if cut_r else rr0
            for s, t in spans:
                if t <= x + 1e-9:
                    rl = min(rl, max(0.0, x - t))
                if s >= y - 1e-9:
                    rr = min(rr, max(0.0, s - y))
            new.append((x, y, rl, rr))
        if len(new) == 1 and abs(new[0][0] - a) < 1e-9 and abs(new[0][1] - b) < 1e-9                 and abs(new[0][2] - rl0) < 1e-9 and abs(new[0][3] - rr0) < 1e-9:
            continue                                  # 変わらない
        rm.append(e.id)
        for x, y, rl, rr in new:
            inner = [[t, m] for t, m in pts if x < t < y]
            npts = [[x, float(np.interp(x, tt, mm))]] + inner + [[y, float(np.interp(y, tt, mm))]]
            params = {"points": npts, "ramp_sec": ramp}
            if abs(rl - ramp) > 1e-9:
                params["ramp_l_sec"] = rl
            if abs(rr - ramp) > 1e-9:
                params["ramp_r_sec"] = rr
            add.append({"kind": "pitch_draw", "target": Target.range(x, y), "params": params,
                        "in_place_of": e.id})
    return rm, add


def _outside(a, b, spans, min_len=1e-4):
    keep = [(a, b)]
    for s, t in spans:
        nxt = []
        for x, y in keep:
            if s > x:
                nxt.append((x, min(y, s)))
            if t < y:
                nxt.append((max(x, t), y))
        keep = [(x, y) for x, y in nxt if y - x > min_len]
    return keep


def trim_pitch_edits(project, spans):
    """「オリジナルに戻す」: pitch_shift / pitch_curve を spans の分だけ外す。(外す id, 入れる spec)

    ノート対象は丸ごと外す。範囲対象で spans に一部だけかかるもの（分割・結合で範囲に
    直したもの、複数ノートにまたがる範囲）は、外側の部分だけ残す。"""
    note_ids = {n.id for n in project.take_notes
                if any(abs(n.start_sec - s) < 1e-9 and abs(n.end_sec - t) < 1e-9 for s, t in spans)}
    rm, add = [], []
    for e in project.edits:
        if e.kind not in PITCH_KINDS:
            continue
        if e.target.type == "note":
            if e.target.note_id in note_ids:
                rm.append(e.id)
            continue
        a, b = float(e.target.start_sec), float(e.target.end_sec)
        if not any(t > a + 1e-9 and s < b - 1e-9 for s, t in spans):
            continue
        rm.append(e.id)
        for x, y in _outside(a, b, spans):
            params = dict(e.params)
            if e.kind == "pitch_curve":
                pts = e.params["points"]
                tt = [q[0] for q in pts]
                cc = [q[1] for q in pts]
                inner = [[t - (x - a), c] for t, c in pts if x - a < t < y - a]
                params["points"] = ([[0.0, float(np.interp(x - a, tt, cc))]] + inner
                                    + [[y - x, float(np.interp(y - a, tt, cc))]])
            add.append({"kind": e.kind, "target": Target.range(x, y), "params": params,
                        "note": e.note, "in_place_of": e.id})
    return rm, add


# ================================================================ 層を当てる
class _SegIndex:
    """重ならない Segment 列の、時刻 → ずらし量（セント）。"""

    def __init__(self, segs):
        self.segs = sorted([s for s in segs if s.silence_sec <= 0 and s.end_sec > s.start_sec],
                           key=lambda s: s.start_sec)
        self.starts = [s.start_sec for s in self.segs]
        self.edges = sorted({s.start_sec for s in self.segs} | {s.end_sec for s in self.segs})

    def jumps(self, t0, t1):
        """[t0, t1] の中の段差 [(時刻, 右 − 左)]（|段差| が EPS_CENTS より大きいものだけ）。"""
        i0 = bisect.bisect_left(self.edges, float(t0) - 1e-9)
        i1 = bisect.bisect_right(self.edges, float(t1) + 1e-9)
        out = []
        for x in self.edges[i0:i1]:
            j = self.at(x, "right") - self.at(x, "left")
            if abs(j) > EPS_CENTS:
                out.append((x, j))
        return out

    def seg_at(self, t, side="right"):
        i = bisect.bisect_right(self.starts, t) - 1
        if side == "left" and i >= 0 and self.segs[i].start_sec == t:
            i -= 1
        if i < 0:
            return None
        s = self.segs[i]
        if side == "left":
            return s if s.start_sec < t <= s.end_sec else None
        return s if s.start_sec <= t < s.end_sec else None

    def at(self, t, side="right"):
        s = self.seg_at(t, side)
        return 0.0 if s is None else seg_value(s, t)


def seg_value(s, t):
    v = float(s.cents)
    if s.curve_points:
        pts = s.curve_points
        v += float(np.interp(t - s.start_sec, [p[0] for p in pts], [p[1] for p in pts]))
    return v


def build_layers(project, segs, edits=None):
    """(つなぎ, 鉛筆)。つなぎの Δ は基本の段（segs）から測る。"""
    base = _SegIndex(segs)
    return transitions(project, base=base), draws(project, edits), base


class Layered:
    """基本の段＋つなぎ＋鉛筆の、時刻 → ずらし量。"""

    def __init__(self, base, trs, drs):
        self.base = base
        self.trs = sorted([t for t in trs if t.active], key=lambda t: t.lo)
        self._los = [t.lo for t in self.trs]
        self._maxw = max([t.hi - t.lo for t in self.trs], default=0.0)
        self.drs = drs
        # 鉛筆も時刻で引けるようにする（鉛筆の多いトラックで、画面の 1 回の書き出しが時刻 × 鉛筆の数だけ
        # 回っていた: 鉛筆 245 本・1.6 万時刻で 400 万回・約 1.2 秒。issue #58 の測定）。
        # 当てる順（後の鉛筆が上書き）は元の並びのまま
        self._dr_order = sorted(range(len(drs)), key=lambda i: drs[i].lo)
        self._dr_los = [drs[i].lo for i in self._dr_order]
        self._dr_maxw = max([d.hi - d.lo for d in drs], default=0.0)

    def _draws_at(self, t):
        """t に重みを持ちうる鉛筆（元の並び）。窓の外の鉛筆は w = 0 なので、除いても値は同じ。"""
        i0 = bisect.bisect_left(self._dr_los, t - self._dr_maxw - 1e-9)
        i1 = bisect.bisect_right(self._dr_los, t + 1e-9)
        if i1 - i0 == len(self.drs):
            return self.drs
        return [self.drs[i] for i in sorted(self._dr_order[i0:i1])]

    def windows(self):
        ws = [(max(0.0, t.lo), t.hi) for t in self.trs] + [(max(0.0, d.lo), d.hi)
                                                            for d in self.drs]
        ws.sort()
        out = []
        for a, b in ws:
            if out and a <= out[-1][1] + 1e-9:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    def at(self, t, side="right", with_draws=True):
        v = self.base.at(t, side)
        # 窓が t を含みうるつなぎだけ（長い素材で全部を毎回回すと遅い）
        i0 = bisect.bisect_left(self._los, t - self._maxw - 1e-9)
        i1 = bisect.bisect_right(self._los, t + 1e-9)
        for tr in self.trs[i0:i1]:
            v += tr.term(t, side)
        if with_draws:
            for d in self._draws_at(t):
                w = d.w(t)
                if w > 0:
                    v = (1.0 - w) * v + w * d.target(t, side)
        return v

    def keep(self, t):
        """鉛筆が基本の段とつなぎをどれだけ残すか（Π(1 − w)）。画面のプレビュー用。"""
        k = 1.0
        for d in self._draws_at(t):
            k *= 1.0 - d.w(t)
        return k


def _slice(seg, a, b):
    from ..render.pipeline import Segment
    cp = None
    if seg.curve_points:
        sh = a - seg.start_sec
        cp = [[float(t) - sh, float(c)] for t, c in seg.curve_points]
    return Segment(start_sec=a, end_sec=b, cents=seg.cents, ratio=seg.ratio,
                   move_ms=seg.move_ms, curve_points=cp, edit_ids=list(seg.edit_ids),
                   silence_sec=0.0, gain=seg.gain)


def apply_layers(segs, lay):
    """Segment 列に層を当てる。窓の中の Segment はずらし量を点列（curve_points）にする。"""
    from ..render.pipeline import Segment
    wins = lay.windows()
    if not wins:
        return segs
    cuts = sorted({round(v, 9) for w in wins for v in w})
    out = []
    for sg in segs:
        if sg.silence_sec > 0 or sg.end_sec <= sg.start_sec:
            out.append(sg)
            continue
        pts = [sg.start_sec] + [c for c in cuts if sg.start_sec < c < sg.end_sec] + [sg.end_sec]
        for a, b in zip(pts[:-1], pts[1:]):
            out.append(_slice(sg, a, b))          # 複製する（基本の段の Segment は評価に使う）
    # 窓の中で編集の無いところを埋める
    live = sorted([s for s in out if s.silence_sec <= 0], key=lambda s: s.start_sec)
    sil_t = sorted({round(s.start_sec, 9) for s in segs if s.silence_sec > 0})

    def fill(a, b):
        # 無音の挿入（silence）の時刻で切る（埋めがその時刻をまたぐと、無音が埋めの後ろに出る）。
        # 旧式の move: 接している隣の Segment の移動量を引き継ぐ（移動量 0 の埋めが挟まると、
        # 隣の移動を吸収する隙間が無くなり、後ろ全体がずれる）
        nxt = next((x for x in live if abs(x.start_sec - b) < 1e-9), None)
        prv = next((x for x in live if abs(x.end_sec - a) < 1e-9), None)
        mv = nxt.move_ms if nxt is not None else (prv.move_ms if prv is not None else 0.0)
        pts = [a] + [t for t in sil_t if a + 1e-9 < t < b - 1e-9] + [b]
        for x, y in zip(pts[:-1], pts[1:]):
            out.append(Segment(start_sec=x, end_sec=y, move_ms=mv, edit_ids=["pitch-layer"]))

    for w0, w1 in wins:
        cur = w0
        for s in live:
            if s.end_sec <= cur or s.start_sec >= w1:
                continue
            if s.start_sec > cur:
                fill(cur, s.start_sec)
            cur = max(cur, s.end_sec)
        if cur < w1:
            fill(cur, w1)
    out.sort(key=lambda s: (s.start_sec, s.end_sec))

    def inside(s):
        m = 0.5 * (s.start_sec + s.end_sec)
        return any(w0 - 1e-9 <= m <= w1 + 1e-9 for w0, w1 in wins)

    res = []
    for s in out:
        if s.silence_sec > 0 or not inside(s):
            res.append(s)
            continue
        a, b = s.start_sec, s.end_sec
        k0 = int(math.floor(a / GRID_SEC)) + 1
        k1 = int(math.ceil(b / GRID_SEC)) - 1
        ts = [a] + [k * GRID_SEC for k in range(k0, k1 + 1) if a + 1e-9 < k * GRID_SEC < b - 1e-9] + [b]
        vals = [lay.at(t, "right") for t in ts[:-1]] + [lay.at(b, "left")]
        if max(abs(v) for v in vals) < EPS_CENTS and abs(s.ratio - 1.0) < 1e-12 \
                and abs(s.move_ms) < 1e-12 and abs(s.gain - 1.0) < 1e-12:
            continue                                  # 何も変わらない（原音のまま）
        s.curve_points = [[round(t - a, 9), float(v)] for t, v in zip(ts, vals)]
        s.cents = 0.0
        s._layer = True
        res.append(s)
    return _join_layer_pieces(res)


def _as_curve(s):
    if s.curve_points:
        return [[float(t), float(c) + float(s.cents)] for t, c in s.curve_points]
    d = s.end_sec - s.start_sec
    return [[0.0, float(s.cents)], [d, float(s.cents)]]


def _join_layer_pieces(segs):
    """窓で切った Segment のうち、途切れずに続く（同じ伸縮・移動の）ものを 1 つにまとめる。

    窓の端で切ったまま渡すと、ノート 1 つのピッチ移動でも「窓の手前｜窓の中｜ノートの残り｜
    次の窓」と別々に再合成して 20 ms のクロスフェードでつなぐことになり、つなぎ目が増える
    （別々に合成した音どうしの重ねで、エンベロープに −1.5 dB ほどのくぼみが出た）。
    ずらし量は点列（curve_points）でそのまま持てるので、1 つの区間として合成する。
    窓に触れていない Segment 同士は今までどおり（まとめない）。"""
    out = []
    for s in segs:
        p = out[-1] if out else None
        if (p is not None and (getattr(p, "_layer", False) or getattr(s, "_layer", False))
                and p.silence_sec <= 0 and s.silence_sec <= 0
                and abs(p.end_sec - s.start_sec) < 1e-9
                and abs(p.ratio - s.ratio) < 1e-12 and p.ratio > 0
                and abs(p.gain - s.gain) < 1e-12
                and abs(p.move_ms - s.move_ms) < 1e-12):
            sh = s.start_sec - p.start_sec
            p.curve_points = _as_curve(p) + [[t + sh, c] for t, c in _as_curve(s)]
            p.cents = 0.0
            p.end_sec = s.end_sec
            p.edit_ids = list(p.edit_ids) + [i for i in s.edit_ids if i not in p.edit_ids]
            p._layer = True
            continue
        out.append(s)
    return out


def layered_segments(project, edits=None):
    """再合成・書き出し・画面の曲線に使う Segment 列（層を当てたもの）。"""
    from ..render.pipeline import edits_to_segments
    edits = project.edits if edits is None else edits
    segs = edits_to_segments(edits, project.edit_span)
    fs = []
    if edits is project.edits:
        # ノートのフェード（issue #20）: 窓を掛けるための印（再合成には使わない）。層を当てる前の列の時間で作る
        from .fades import fade_segments
        fs = fade_segments(project, segs)
    trs, drs, base = build_layers(project, segs, edits)
    lay = Layered(base, trs, drs)
    out = apply_layers(segs, lay)
    return list(out) + fs if fs else out


def edits_base_index(project):
    """基本の段（pitch_shift / pitch_curve）の 時刻 → ずらし量。"""
    return _base_index(project)


def edited_note_centers(project, notes=None):
    """{note id: 編集後の中心の音程（MIDI）}。基本の段（shift_pitch・曲線）と**鉛筆**を含む。

    `current_note_pitches`（基本の段だけ）から寄せると、鉛筆で直したノートでは鉛筆の線の上に
    「元の中心 → 目標」の差が二重に乗る（鉛筆の線は後から入ったずらしを足すので）。
    つなぎ（なだらかさ）は含めない（寄せた後の段差で変わり、寄せる量が決まらなくなるため）。"""
    from ..render.pipeline import edits_to_segments
    f0r = project.take_f0
    segs = edits_to_segments(project.edits, project.edit_span)
    base = _SegIndex(segs)
    lay = Layered(base, [], draws(project))
    hop = f0r.hop_s
    out = {}
    for n in (project.take_notes if notes is None else notes):
        if n.pitch_midi is None:
            continue
        a = max(0, int(round(n.start_sec / hop)))
        b = min(len(f0r.times), int(round(n.end_sec / hop)))
        if b <= a:
            out[n.id] = float(n.pitch_midi)
            continue
        off = [lay.at(float(f0r.times[i]), "right") for i in range(a, b)]
        out[n.id] = float(n.pitch_midi) + float(np.mean(off)) / 100.0
    return out


def pitch_edited_notes(project, notes):
    """notes のうち、音程の編集（shift_pitch・曲線・鉛筆）が掛かっているノートの id の集合。"""
    out = set()
    eds = [e for e in project.edits if e.kind in PITCH_KINDS + ("pitch_draw",)]
    for n in notes:
        for e in eds:
            if e.target.type == "note":
                if e.target.note_id == n.id:
                    out.add(n.id)
                    break
                continue
            a, b = float(e.target.start_sec), float(e.target.end_sec)
            if b > n.start_sec + 1e-6 and a < n.end_sec - 1e-6:
                out.add(n.id)
                break
    return out


def pitch_model(project):
    """(基本の段の Segment 列, 層を当てた Segment 列, Layered)。画面のデータ用。"""
    from ..render.pipeline import edits_to_segments
    segs = edits_to_segments(project.edits, project.edit_span)
    trs, drs, base = build_layers(project, segs)
    lay = Layered(base, trs, drs)
    out = apply_layers(list(segs), lay)
    return segs, out, lay, trs


# ================================================================ つなぎの編集
def transition_pairs(project, note_a=None, note_b=None, note_ids=None, start_sec=None,
                     end_sec=None):
    """対象の境目（接続されたもの）。note_ids はそのノートの両側の境目。"""
    trs = transitions(project)
    if note_a or note_b:
        sel = [t for t in trs if t.a == note_a and t.b == note_b]
        if not sel:
            raise PitchError("%s｜%s は接続された隣り合うノートではない（list_connections で確認）"
                             % (note_a, note_b))
        return sel
    if note_ids:
        ids = set(note_ids)
        return [t for t in trs if t.a in ids or t.b in ids]
    if start_sec is not None or end_sec is not None:
        s = -1e18 if start_sec is None else float(start_sec)
        e = 1e18 if end_sec is None else float(end_sec)
        return [t for t in trs if s <= t.ta <= e]
    return trs


def transition_specs(project, pairs, value):
    """(外す id, 入れる spec)。value = None または 0.5 は「自動」（上書きを外すだけ）。

    外すのは、その段差に**時刻で**掛かっている `transition`（`_matches`）。"""
    rm = [e.id for e, s, t in transition_edits(project)
          if any(_matches(e, s, t, tr) for tr in pairs)]
    add = []
    if value is not None and abs(float(value) - DEFAULT_VALUE) > 1e-9:
        for t in pairs:
            add.append({"kind": "transition", "target": Target.range(t.ta, t.tb),
                        "params": {"a": t.a, "b": t.b, "value": float(value)}})
    return rm, add


def log_summary(project):
    trs = transitions(project)
    log.get().info("つなぎ: 接続 %d 組（上書き %d）", len(trs), sum(t.set_by_user for t in trs))
