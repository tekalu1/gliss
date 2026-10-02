# -*- coding: utf-8 -*-
"""ガイドのリズムの基準 — **タイムライン上のガイドの時刻**（置き場所の違う素材だけ + 全体のずれ。issues #12, #61）。

## なぜ DTW の写像をそのまま基準にしないか

DTW（`align.py`）は「中身が同じところ」を対応づける。テイクが 60 ms 遅れて歌っていれば、
ガイドの発音の頭は**テイクの遅れた発音の頭**に写る。写したガイドに合わせても、動くのは
DTW の誤差とノート分割の違いの分だけで、リズムは直らない（むしろ崩れる。`docs/guide-timing.md`）。

そこで DTW は**どのノートとどのノートが対応するか**を決めるのにだけ使い、位置の基準は
ガイドの**ファイル上の時刻**に「全体のずれ」を足したものにする:

- 全体のずれ = 対応した発音の頭どうしの差（テイク − ガイド）のいちばん混んでいる値（曲全体で 1 つ。
  DTW が外れた所の差に引っ張られないよう、ただの中央値ではなく最頻の近くの中央値）。
  同じ DAW の時間軸から書き出した素材なら 0 付近になる。
- **全体のずれが `TIMELINE_MAX_SEC`（150 ms）以内なら、基準はずれ 0 = タイムライン上に置いたガイドの位置**
  （issue #61）。その程度のずれは歌い手の走り・もたりか、トラックの位置ずらし（`offset_sec`。ガイドは
  タイムライン上の位置で切り出して渡される）で、「ガイドに合わせる」はそれごと合わせる。以前は小さくても
  全体のずれを足した位置へ合わせていたので、テイクが全体に遅れていると、ガイドに合っていた頭まで
  そのぶん遅らせ、強くするほどガイドから離れた。150 ms を超える（曲の置き場所が違う素材・頭に無音を
  足したガイド）ときだけ、全体のずれを足した位置を基準にする（`GuideTiming.basis`）
- ある所の差の中央値（前後 ±`LOCAL_SEC`）が全体のずれから `LOCAL_SWITCH_SEC` を超えて離れ、
  しかもその中でそろっている（ばらつき ≤ `LOCAL_MAD_SEC`）なら、そこだけはその値を使う
  （別の演奏で、フレーズの置き場所自体が違う。158 秒の曲の後半のように 0.2〜1.9 秒ずれる素材）。
  離れているのにそろっていない所は、基準が決まらないので**動かさない**。

## 発音の頭の対応（1 対 1 だけ）

テイクとガイドの**両方に歌詞がある**ときは、同じ音節どうしを組にする（`build_syllables`）。
無いときは次のとおり音の立ち上がりを DTW で組にする（`build`）。

発音の頭は音の立ち上がり（`onsets.py`）。音程で切ったノートの頭は、なめらかにつながる所で
立ち上がりと 30〜100 ms ずれ、テイクとガイドで切れ方も違うので使わない。
どの頭とどの頭が同じ音節かは**DTW で決める**（位置の近さで決めると、100 ms 遅れて歌った所で
隣の音節と組んでしまう。C の後半がそう）。テイクの頭 s を DTW でガイドの時間に写し、
最も近いガイドの頭 g を取る。次のどれかに当たる s は**対応なし（動かさない）**とする:

- DTW の写像そのものと、前後 ±`SMOOTH_SEC` の流れ（中央値）で写した位置とで、最も近い
  ガイドの頭が違う（写像がその所で揺れていて信頼できない）
- 写した位置から `PAIR_TOL_SEC` 以内にガイドの頭が無い（片方にしか無い立ち上がり）
- 2 番目の候補との差が `AMBIG_SEC` 未満（どちらか決められない）
- g を DTW でテイクに戻すと、別のテイクの頭の方が近い（相互に最も近い組ではない = 1 対多）

ただし DTW の組の差がそろっている（同じ DAW の曲から書き出した、同じ時間軸の素材）ときは、
DTW で組めなかった頭も、全体のずれを引いた位置で互いに最も近く、動かす量が隣の頭との間の
1/3 以下なら組にする（`_same_timeline_pairs`。DTW が局所的に外れる所を救う）。
- そこで全体のずれが定まらない（上）、または動かす量が `MAX_SHIFT_SEC` を超える
- 全体のずれを引いた位置で見ると、s に最も近いガイドの頭が g でない（または g に最も近い
  テイクの頭が s でない）。隣の頭との間の半分より大きく動かすことになり、どちらと組むのか
  決められない（C の後半のように 100 ms 以上遅れて歌った所は、ここで動かさない）
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

PAIR_TOL_SEC = 0.05        # DTW で写した位置からこの範囲にガイドの頭があること（DTW の誤差は 90% 点 40 ms）
AMBIG_SEC = 0.02           # 2 つの候補の近さの差がこれ未満なら決めない
SAME_MIN = 8               # 同じ時間軸とみなすのに要る DTW の組の数
SAME_MAD_SEC = 0.03        # そのときの差のばらつき（中央絶対偏差）の上限
SAME_FRAC = 0.6            # 全体のずれの近くにある DTW の組の割合の下限
POS_TOL_SEC = 0.08         # 同じ時間軸の素材で、位置で組むときの動かす量の上限
SYL_DTW_TOL_SEC = 0.15     # 歌詞で組んだ音節の頭と、DTW で写した位置との差の上限
SNAP_ONSET_SEC = 0.04      # 歌詞の音節の頭を、これより近い音の立ち上がりに寄せる
SMOOTH_SEC = 1.0           # DTW の写像の「ゆっくりした流れ」を取る前後の幅
MAX_SHIFT_SEC = 0.30       # 1 つの頭を動かす量の上限（これを超える組は別の音節に対応している）
LOCAL_SEC = 2.5            # 所ごとのずれを取る前後の幅
LOCAL_MIN = 3              # 所ごとのずれを取るのに要る組の数
LOCAL_SWITCH_SEC = 0.15    # 全体のずれからこれを超えて離れたら「置き場所が違う所」
LOCAL_MAD_SEC = 0.06       # そのときの差のばらつき（中央絶対偏差）の上限
LOCAL_MAX_SEC = 3.0        # 所ごとのずれが全体のずれからこれ以上離れる所は DTW が外れているとみなす
TIMELINE_MAX_SEC = LOCAL_SWITCH_SEC   # 全体のずれがこれ以内なら、タイムライン上のガイドの位置そのものを基準にする（issue #61）


@dataclass
class OnsetPair:
    take_id: str
    guide_id: str
    take_sec: float            # テイクの発音の頭（編集前の秒）
    guide_sec: float           # ガイドの発音の頭（ガイドのファイル上の秒）
    offset_sec: float          # そこでの全体のずれ（テイク − ガイド）
    mode: str                  # global / local

    @property
    def target_sec(self):
        """合わせる先（テイクの時間）= ガイドの頭 + 全体のずれ。"""
        return self.guide_sec + self.offset_sec

    def to_json(self):
        return {"take": self.take_id, "guide": self.guide_id,
                "take_sec": round(self.take_sec, 4), "guide_sec": round(self.guide_sec, 4),
                "offset_sec": round(self.offset_sec, 4), "target_sec": round(self.target_sec, 4),
                "mode": self.mode}


@dataclass
class GuideTiming:
    offset_sec: float                                   # 合わせる基準のずれ（テイク − ガイド）。タイムライン基準なら 0
    pairs: list = field(default_factory=list)           # [OnsetPair]
    skipped: dict = field(default_factory=dict)         # テイクの頭の id -> 理由
    knots_g: np.ndarray = None                          # ガイドの秒 → そこでのずれ（折れ線）
    knots_off: np.ndarray = None
    n_candidates: int = 0
    source: str = "onsets"                              # onsets（立ち上がり + DTW）/ syllables（歌詞）
    same_timeline: bool = False                         # 同じ時間軸の素材とみなした（位置でも組んだ）
    measured_offset_sec: float = 0.0                    # 測った全体のずれ（発音の頭どうしの差の最頻値）
    basis: str = "timeline"                             # timeline（ガイドの置いた位置）/ offset（+ 全体のずれ）

    def to_take(self, g):
        """ガイドの秒 → テイクの時間（リズムの基準。画面にガイドを描く位置）。"""
        g = np.asarray(g, dtype="float64")
        if self.knots_g is None or not len(self.knots_g):
            return g + self.offset_sec
        return g + np.interp(g, self.knots_g, self.knots_off)

    def pair_of(self, take_id):
        for p in self.pairs:
            if p.take_id == take_id:
                return p
        return None

    def summary(self):
        local = [p for p in self.pairs if p.mode == "local"]
        return {"offset_ms": round(self.offset_sec * 1000.0, 1),
                "measured_offset_ms": round(self.measured_offset_sec * 1000.0, 1),
                "basis": self.basis,
                "pairs": len(self.pairs), "local_pairs": len(local),
                "candidates": self.n_candidates, "skipped": len(self.skipped),
                "source": self.source, "same_timeline": self.same_timeline}


def _mad(v):
    v = np.asarray(v, dtype="float64")
    return float(np.median(np.abs(v - np.median(v))))


def _mode_offset(r, win=0.05, keep=0.10):
    """差の列のいちばん混んでいる所（±win に最も多く入る値）の近く（±keep）の中央値。

    DTW が外れた所（中身の違うフレーズに写った所）の差は大きくばらつくので、
    ただの中央値だと引っ張られる。"""
    r = np.sort(np.asarray(r, dtype="float64"))
    lo = np.searchsorted(r, r - win, side="left")
    hi = np.searchsorted(r, r + win, side="right")
    c = r[int(np.argmax(hi - lo))]
    m = np.abs(r - c) <= keep
    return float(np.median(r[m]))


def _smooth_offset(al, t, half=SMOOTH_SEC):
    """テイクの秒 t での DTW のずれ（テイク − ガイド）の前後 ±half 秒の中央値。"""
    ts = np.asarray(al.take_sec, dtype="float64")
    off = ts - np.asarray(al.guide_sec, dtype="float64")
    lo = np.searchsorted(ts, np.asarray(t) - half, side="left")
    hi = np.searchsorted(ts, np.asarray(t) + half, side="right")
    return np.array([float(np.median(off[a:b])) if b > a else float(np.interp(v, ts, off))
                     for a, b, v in zip(np.atleast_1d(lo), np.atleast_1d(hi), np.atleast_1d(t))])


def _nearest(arr, v):
    """(最も近い番号, 距離, 2 番目の距離)。"""
    j = int(np.searchsorted(arr, v))
    cand = [k for k in (j - 1, j, j + 1, j - 2) if 0 <= k < len(arr)]
    cand.sort(key=lambda k: abs(arr[k] - v))
    d1 = abs(arr[cand[0]] - v)
    d2 = abs(arr[cand[1]] - v) if len(cand) > 1 else np.inf
    return cand[0], float(d1), float(d2)


def build(take_sec, guide_sec, alignment, take_ids=None, guide_ids=None):
    """テイク・ガイドの発音の頭（秒の列）と DTW から、リズムの基準と 1 対 1 の組を作る。"""
    S = np.sort(np.asarray(take_sec, dtype="float64"))
    G = np.sort(np.asarray(guide_sec, dtype="float64"))
    tid = list(take_ids) if take_ids is not None else ["t%.3f" % v for v in S]
    gid = list(guide_ids) if guide_ids is not None else ["g%.3f" % v for v in G]
    out = GuideTiming(offset_sec=0.0, source="onsets")
    if alignment is None or not len(S) or not len(G):
        return out
    al = alignment
    # ---- 組の候補: DTW で写した位置に最も近いガイドの頭（相互に最も近い・紛れが無い）
    #      どの頭とどの頭が同じ音節かは DTW（中身）で決める。位置の基準には使わない
    to_g = np.asarray(al.to_guide(S), dtype="float64")
    g_to = np.asarray(al.to_take(G), dtype="float64")
    smooth = S - _smooth_offset(al, S)          # DTW のゆっくりした流れだけで写した位置
    cand = []                                   # (take id, take 秒, guide id, guide 秒)
    dtw_miss = []
    for i in range(len(S)):
        j, d1, d2 = _nearest(G, to_g[i])
        if _nearest(G, smooth[i])[0] != j:
            out.skipped[tid[i]] = "DTW の写像がその所で揺れている（信頼度が低い）"
            dtw_miss.append(i)
            continue
        if d1 > PAIR_TOL_SEC:
            out.skipped[tid[i]] = "DTW で写した位置の近くにガイドの発音の頭が無い"
            dtw_miss.append(i)
            continue
        if d2 - d1 < AMBIG_SEC:
            out.skipped[tid[i]] = "ガイドの頭の候補が 2 つある（決められない）"
            continue
        i2, _, _ = _nearest(S, g_to[j])
        if i2 != i:
            out.skipped[tid[i]] = "ガイドの頭には別のテイクの頭の方が近い（1 対多）"
            continue
        cand.append((tid[i], float(S[i]), gid[j], float(G[j])))
    cand = _same_timeline_pairs(out, cand, dtw_miss, S, G, tid, gid)
    return _model(out, cand, positions=(S, G))


def _same_timeline_pairs(out, cand, idx, S, G, tid, gid):
    """DTW で組めなかった頭を、**同じ時間軸の素材**なら位置で組む。

    DTW の組の差（テイク − ガイド）がそろっている（中央絶対偏差 ≤ `SAME_MAD_SEC`、
    `SAME_MIN` 組以上）なら、テイクとガイドは同じ時間軸に置かれている（DAW の同じ曲から
    書き出した素材）。そのときは全体のずれを引いた位置で互いに最も近く、動かす量が隣の頭との
    間の 1/3 以下（`POS_TOL_SEC` 以下）の頭どうしを組にする。DTW が局所的に外れた所
    （ユーザーの素材の 42.17 秒など、写像が 100 ms 以上ずれる所）を救う。

    同じ時間軸なら、差が全体のずれから `LOCAL_SWITCH_SEC` を超えて離れた DTW の組は
    DTW が外れた所（テイクが歌っていない所の多いファイルで、別のフレーズに写った所）なので、
    その頭も位置で組み直す。返り値は組の候補の全体。"""
    if len(cand) < SAME_MIN:
        return cand
    r = np.array([c[1] - c[3] for c in cand])
    og = _mode_offset(r)
    inl = np.abs(r - og) <= LOCAL_SWITCH_SEC
    if inl.sum() < SAME_MIN or inl.mean() < SAME_FRAC or _mad(r[inl]) > SAME_MAD_SEC:
        return cand
    out.same_timeline = True
    pos = {v: k for k, v in enumerate(tid)}
    for c, ok in zip(cand, inl):
        if not ok:
            out.skipped[c[0]] = "DTW の組が全体のずれから大きく離れている（DTW が外れた所）"
    idx = list(idx) + [pos[c[0]] for c, ok in zip(cand, inl) if not ok]
    cand = [c for c, ok in zip(cand, inl) if ok]
    have_g = {c[2] for c in cand}
    extra = []
    for i in sorted(idx):
        j, d1, d2 = _nearest(G, S[i] - og)
        gap_t = min([abs(S[i] - S[k]) for k in (i - 1, i + 1) if 0 <= k < len(S)] or [np.inf])
        gap_g = min([abs(G[j] - G[k]) for k in (j - 1, j + 1) if 0 <= k < len(G)] or [np.inf])
        if gid[j] in have_g or d1 > min(POS_TOL_SEC, gap_t / 3.0, gap_g / 3.0):
            continue
        if d2 - d1 < AMBIG_SEC or _nearest(S, G[j] + og)[0] != i:
            continue
        out.skipped.pop(tid[i], None)
        extra.append((tid[i], float(S[i]), gid[j], float(G[j])))
        have_g.add(gid[j])
    return cand + extra


def build_syllables(take_syl, guide_syl, take_on=None, guide_on=None, alignment=None):
    """テイクとガイドの**両方に歌詞がある**とき: 同じ音節どうしを組にする（DTW より確か）。

    take_syl / guide_syl = [(romaji, 頭の秒)]。並びの違い（歌詞の範囲・言い直し）は
    difflib で合う所だけ取る。頭の秒は、`SNAP_ONSET_SEC` 以内に音の立ち上がりがあれば
    そちらに寄せる（アライナーの境界は立ち上がりから 10〜30 ms ずれることがある）。"""
    import difflib
    out = GuideTiming(offset_sec=0.0, source="syllables")
    if not take_syl or not guide_syl:
        return out

    def snap(t, on):
        if on is None or not len(on):
            return float(t)
        j, d1, _ = _nearest(np.asarray(on), t)
        return float(on[j]) if d1 <= SNAP_ONSET_SEC else float(t)

    ta = [r for r, _ in take_syl]
    ga = [r for r, _ in guide_syl]
    sm = difflib.SequenceMatcher(a=ta, b=ga, autojunk=False)
    cand = []
    matched = set()
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            i, j = blk.a + k, blk.b + k
            matched.add(i)
            # 同じ音節が続く所（「ら ら ら」）は並びだけだと 1 つずれて組むことがある。
            # DTW で写した位置と大きく違う組は使わない
            if (alignment is not None and abs(float(alignment.to_guide(take_syl[i][1]))
                                              - guide_syl[j][1]) > SYL_DTW_TOL_SEC):
                out.skipped["s%03d" % i] = "歌詞の並びの組と DTW の対応が食い違う（決められない）"
                continue
            cand.append(("s%03d" % i, snap(take_syl[i][1], take_on), "gs%03d" % j,
                         snap(guide_syl[j][1], guide_on)))
    for i in range(len(ta)):
        if i not in matched:
            out.skipped["s%03d" % i] = "ガイドの歌詞に同じ音節が無い"
    # 寄せた結果、同じ立ち上がりを 2 つの音節が取り合ったら、その 2 つは使わない
    for side in (1, 3):
        seen = {}
        for c in cand:
            seen.setdefault(round(c[side], 4), []).append(c[0])
        dup = {x for v in seen.values() if len(v) > 1 for x in v}
        for x in dup:
            out.skipped[x] = "隣の音節と同じ立ち上がりに寄ってしまう（決められない）"
        cand = [c for c in cand if c[0] not in dup]
    return _model(out, cand)


def _model(out, cand, positions=None):
    """組の候補 → 全体のずれ（所ごとのずれ）と、合わせる組。

    positions = (テイクの頭の列, ガイドの頭の列) を渡すと、全体のずれを引いた位置で見ても
    その 2 つが互いに最も近いことを確かめる（DTW の組と位置の組が一致しない頭は動かさない:
    隣の頭との間の半分より大きく動かすことになり、どちらの頭と組むのか決められない）。"""
    out.n_candidates = len(cand)
    if not cand:
        return out
    cand = sorted(cand, key=lambda c: c[1])
    cs = np.array([c[1] for c in cand])
    cr = np.array([c[1] - c[3] for c in cand])
    og = _mode_offset(cr)
    # 合わせる基準（issue #61）: 全体のずれが小さい（`TIMELINE_MAX_SEC` 以内）なら、それは歌い手の走り・もたり
    # （とトラックの位置ずらし）なので、**タイムライン上に置いたガイドの位置そのもの**（ずれ 0）へ合わせる。
    # 以前は全体のずれを足した位置へ合わせていたので、テイクが全体に 30 ms 遅れていれば、ガイドに合っていた
    # 頭まで 30 ms 遅らせ、強くするほどガイドから離れた。大きい（曲の置き場所が違う素材・頭に無音を足した
    # ガイド）ときだけ、以前どおり全体のずれを足した位置を基準にする
    ref = 0.0 if abs(og) <= TIMELINE_MAX_SEC else og
    out.offset_sec = ref
    out.measured_offset_sec = og
    out.basis = "timeline" if ref == 0.0 else "offset"

    def offset_at(s):
        m = np.abs(cs - s) <= LOCAL_SEC
        if m.sum() < LOCAL_MIN:
            return ref, "global"
        lm = float(np.median(cr[m]))
        if abs(lm - og) <= LOCAL_SWITCH_SEC:
            return ref, "global"
        if abs(lm - og) <= LOCAL_MAX_SEC and _mad(cr[m]) <= LOCAL_MAD_SEC:
            return lm, "local"
        return None, None

    # ---- 合わせる組と、画面に描く位置（ガイドの秒 → ずれ）の折れ線
    kg, ko = [], []
    for (ti, s, gi, g), r in zip(cand, cr):
        o, mode = offset_at(s)
        if o is None:
            out.skipped[ti] = "全体のずれがそこで定まらない（DTW が外れている所）"
            continue
        kg.append(g)
        ko.append(o)
        if abs(r - o) > MAX_SHIFT_SEC:
            out.skipped[ti] = "ずれが大きすぎる（%.0f ms。別の音節に対応している）" % ((r - o) * 1000)
            continue
        if positions is not None:
            PS, PG = positions
            if (abs(PG[_nearest(PG, s - o)[0]] - g) > 1e-9
                    or abs(PS[_nearest(PS, g + o)[0]] - s) > 1e-9):
                out.skipped[ti] = ("隣の頭との間の半分より大きく動かすことになる（%.0f ms。"
                                   "どちらの頭と組むか決められない）" % ((r - o) * 1000))
                continue
        out.pairs.append(OnsetPair(take_id=ti, guide_id=gi, take_sec=s, guide_sec=g,
                                   offset_sec=float(o), mode=mode))
    if kg and any(abs(o - ref) > 1e-9 for o in ko):
        order = np.argsort(kg, kind="stable")
        g_ = np.asarray(kg, dtype="float64")[order]
        o_ = np.asarray(ko, dtype="float64")[order]
        # 描く位置 g + ずれ が逆向きに進まないように（ずれが間隔より大きく減る段差は、減った後の
        # 値を手前へ広げる。画面は guide_sec が単調だとして二分探索する）
        pos = g_ + o_
        for k in range(len(pos) - 2, -1, -1):
            if pos[k] > pos[k + 1]:
                pos[k] = pos[k + 1]
        out.knots_g = g_
        out.knots_off = pos - g_
    return out


def of_project(project, align_lyrics=True):
    """プロジェクトから作る。ガイドが無ければ None。

    テイクとガイドの両方に歌詞（音素）があれば音節どうし、無ければ音の立ち上がりを DTW で組にする。
    align_lyrics=False: 音素のアラインがまだなら走らせない（画面を描く途中で重い処理をしない）。"""
    import os
    if project.guide is None or project.alignment is None:
        return None
    t = project.onsets("take")
    g = project.onsets("guide")

    def ph(role):
        if not project.has_lyrics(role):
            return None
        if (not align_lyrics and project._phonemes.get(role) is None
                and not os.path.exists(project._cache_path("%s-phonemes.json" % role))):
            return None
        return project.phonemes(role)

    tp = ph("take")
    gp = ph("guide") if tp is not None else None
    if tp is not None and gp is not None and tp.syllables and gp.syllables:
        ts = [(x["romaji"], x["start_sec"]) for x in tp.syllables]
        gs = [(x["romaji"], x["start_sec"]) for x in gp.syllables]
        return build_syllables(ts, gs, t, g, alignment=project.alignment)
    return build(t, g, project.alignment,
                 take_ids=["o%04d" % i for i in range(len(t))],
                 guide_ids=["go%04d" % i for i in range(len(g))])
