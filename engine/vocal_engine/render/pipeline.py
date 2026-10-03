# -*- coding: utf-8 -*-
"""編集リスト → 波形。

約束:
  - **編集していない区間は元のサンプルをそのまま出す。**
  - 編集した区間だけ再合成し、境界は 20 ms のクロスフェードでつなぐ。
  - クロスフェードの 20 ms は、つなぐ両側が**それぞれ途切れずに続いている音**どうしを重ねる。
    再合成する側は区間の前後 10 ms ずつ余分に再合成して（= マージン）その部分を使う。
    原音のままの側は原音の続きを使う。
    （段階3で修正: 以前は再合成した区間の後ろに原音を直に継ぎ足してから等パワーで重ねていたので、
     境界に段差（クリック）と最大 +3 dB の膨らみが出ていた。）
  - クロスフェードが縮まないように、**出力が 20 ms 未満の区間は隣と束ねるか、隣の隙間から借りて広げる**
    （`bundle_short_segments`）。1 つの chunk の頭と尻のクロスフェードは chunk の中で重ならない（`_join`）。
    縮んだり重なったりすると、再合成の端と次の音が 1 サンプルの段差（クリック）でつながる。
  - 隣り合う区間は別々に再合成するので位相がそろわない（rho が 0 に近いと等パワーで混ざって痩せ、ピッチが細かく揺れる）。
    入ってくる再合成の区間を ±ALIGN_MAX_MS 動かして、つなぎ目の相互相関をそろえる（`_join`）。原音のままの区間は動かさない。
  - フェードの形は、重ねる 2 つの音の相関 rho に合わせて振幅を補正する
    （rho=1 → 振幅の和が 1、rho=0 → 等パワー）。同じ原音どうしでも、ピッチの違う音どうしでも
    膨らまない・痩せない。

出力の長さの決め方:
  区間 [T0, T1) を、編集区間（segment）と非編集区間（gap）に分ける。
  移動（move）は **直前の gap が吸収する**（note を m だけ後ろへ動かす
  = 直前の gap が m 長くなり、直後の gap が m 短くなる）。
  伸縮（stretch）はその区間の出力長を ratio 倍にする。
  **無音の挿入（silence）** はその時刻に無音を足し、**切り取り（crop）** はその区間を出さない
  （`project/timing.py` がこの 3 つで「後ろをずらさない」編集を組む）。
  **無音にする（mute）** は長さ・位置を変えずに中身を無音にする（隣とは 20 ms で無音へフェード）。
  **ノートのフェード（fade）** は時間にもピッチにも効かない。`project/fades.py` が作る**印の Segment**
  （`Segment.fade`）を render_range がつないだ後の出力に音量の包絡として掛ける（出力の秒 = 編集後の秒）。

出力の位置は**累積の秒をサンプルに丸めてから**各 chunk の長さを決める
（chunk ごとに丸めると誤差が積もり、編集の後ろの原音が 1〜2 サンプルずれて
「範囲外はサンプル一致」が崩れるため）。
"""
from dataclasses import dataclass, field, replace

import numpy as np

from .. import XFADE_MS
from ..project.model import Edit
from .base import get_backend, resolve_backend_name

MIN_GAP_MS = 5.0
ALIGN_MAX_MS = 5.0       # つなぎ目で再合成した区間の位相をそろえるために動かす最大（±ms。0 なら無効）
ALIGN_MIN_GAIN = 0.05    # 相関がこれ以上よくならなければ動かさない
ALIGN_RIGHT_WEIGHT = 0.5  # 尻の側（動かせない次の区間）との相関の重み。頭の側（前の区間）を優先する
ALIGN_TIE = 0.01         # 相関の差がこれ以下の量どうしでは、動かす量の小さい方を選ぶ


@dataclass
class Segment:
    start_sec: float
    end_sec: float
    cents: float = 0.0
    ratio: float = 1.0
    move_ms: float = 0.0
    curve_points: list | None = None
    edit_ids: list = field(default_factory=list)
    silence_sec: float = 0.0        # > 0 なら start_sec（= end_sec）の時刻に挟む無音
    gain: float = 1.0               # 0 = 無音にした区間（`mute`。長さ・位置は変えない）
    # フェードの印（issue #20。`project/fades.py`）: (編集後の頭, 尻, "in"/"out")。再合成には使わず、
    # 書き出し・再生の窓をこの区間に掛けるためと、出力に音量の包絡を掛けるためだけに置く
    fade: tuple | None = None

    def is_identity(self):
        return (abs(self.cents) < 1e-9 and abs(self.ratio - 1.0) < 1e-9
                and abs(self.move_ms) < 1e-9 and not self.curve_points
                and self.silence_sec <= 0.0 and abs(self.gain - 1.0) < 1e-9
                and self.fade is None)


@dataclass
class _Pseudo:
    """`move_boundary` を前後 2 区間の伸縮に展開したときの仮の編集。"""
    id: str
    kind: str
    params: dict


def expand_boundary_edits(edits, span_of):
    """`move_boundary` を「左の音素の伸縮」＋「右の音素の伸縮」に展開する。

    境界 b を +dt 動かす = 左の音素が dt 長くなり、右の音素が dt 短くなる
    （全体の長さは変わらない）。伸縮比は編集を作った時点で params に入っているので、
    ここでは区間に割り当て直すだけでよい。
    """
    out = []
    for e in edits:
        if getattr(e, "kind", None) != "move_boundary":
            out.append((float(span_of(e)[0]), float(span_of(e)[1]), e))
            continue
        p = e.params
        lo, mid, hi = float(p["left_sec"]), float(p["boundary_sec"]), float(p["right_sec"])
        if mid > lo:
            out.append((lo, mid, _Pseudo(e.id, "stretch", {"ratio": float(p["left_ratio"])})))
        if hi > mid:
            out.append((mid, hi, _Pseudo(e.id, "stretch", {"ratio": float(p["right_ratio"])})))
    return out


def edits_to_segments(edits, span_of):
    """編集リスト → 重なりのない Segment 列（時間順）。

    span_of(edit) -> (start_sec, end_sec)
    `move_boundary` は前後 2 区間の伸縮に展開してから扱う。

    **すべての編集の端で切って、素の区間ごとに足し合わせる。**
    重なる編集を「1 つの Segment に畳んで端を広げる」と、
    ノート単位のピッチ編集（範囲が広い）と音素単位の伸縮（範囲が狭い）が
    同居したときに、**狭い方の伸縮比が広い方の区間全体に掛かってしまう**
    （段階2 で音素単位のタイミング編集を入れて表に出た）。

    区間ごとに: cents は加算、ratio は積、move は加算、pitch_curve は後勝ち。
    ratio を細切れにしても出力長の合計は変わらないので、伸縮の意味は保たれる。

    `crop` は ratio 0（その区間を出さない）、`silence` は長さ 0 の Segment
    （`silence_sec` 秒の無音）として同じ列に入る。`connection` は時間に効かないので飛ばす。
    """
    silences = {}
    rest = []
    for e in edits:
        k = getattr(e, "kind", None)
        if k in ("connection", "transition", "pitch_draw", "split", "merge", "fade"):
            continue                          # 時間に効かない／ピッチの層（project/pitch.py）・フェード（fades.py）で当てる
        if k == "silence":
            t = round(float(span_of(e)[0]), 9)
            cur = silences.setdefault(t, [0.0, []])
            cur[0] += float(e.params["sec"])
            cur[1].append(e.id)
            continue
        rest.append(e)
    items = expand_boundary_edits(rest, span_of)
    sil = [Segment(start_sec=t, end_sec=t, silence_sec=v, edit_ids=ids)
           for t, (v, ids) in sorted(silences.items()) if v > 1e-9]
    if not items:
        return sil
    # 無音を挟む時刻でも切る（範囲のピッチ編集の途中に無音が入っても、その位置に出すため）
    cuts = sorted({round(float(v), 9) for s, t, _ in items for v in (s, t)}
                  | set(silences))
    segs = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        if b - a <= 1e-9:
            continue
        seg = None
        for s, t, e in items:                 # 編集リストの順（pitch_curve は後勝ち）
            if s > a + 1e-9 or t < b - 1e-9:
                continue
            if seg is None:
                seg = Segment(start_sec=a, end_sec=b)
            _apply_to_segment(seg, e, origin=s)
        if seg is not None:
            segs.append(seg)
    segs = _coalesce(segs, barriers=set(silences))
    for sg in segs:
        # 曲線と一定量が重なったら曲線に畳む（ずらし量 = cents + 曲線。画面の cents_offset と同じ。
        # バックエンドによって「曲線が cents より優先」だったので、足し算にそろえる）
        if sg.curve_points and abs(sg.cents) > 0:
            sg.curve_points = [[t, c + sg.cents] for t, c in sg.curve_points]
            sg.cents = 0.0
    if sil:
        segs = sorted(segs + sil, key=lambda s: (s.start_sec, s.end_sec))
    return segs


def _apply_to_segment(seg, e, origin=None):
    seg.edit_ids.append(e.id if isinstance(e, (Edit, _Pseudo)) else str(e))
    if e.kind == "pitch_shift":
        seg.cents += float(e.params["cents"])
    elif e.kind == "pitch_curve":
        # 曲線は「編集の頭からの相対秒」なので、切ったぶんだけ原点をずらす
        shift = 0.0 if origin is None else (seg.start_sec - float(origin))
        seg.curve_points = [[float(t) - shift, float(c)] for t, c in e.params["points"]]
    elif e.kind == "move":
        seg.move_ms += float(e.params["ms"])
    elif e.kind == "stretch":
        seg.ratio *= float(e.params["ratio"])
    elif e.kind == "crop":
        seg.ratio = 0.0
    elif e.kind == "mute":
        seg.gain = 0.0


def _coalesce(segs, barriers=()):
    """中身が同じ隣り合う区間は 1 つに戻す（つなぎ目を増やさないため）。"""
    out = []
    for sg in segs:
        p = out[-1] if out else None
        if (p is not None and abs(p.end_sec - sg.start_sec) < 1e-9
                and round(p.end_sec, 9) not in barriers
                and p.silence_sec <= 0.0 and sg.silence_sec <= 0.0
                and p.curve_points is None and sg.curve_points is None
                and abs(p.cents - sg.cents) < 1e-12
                and abs(p.ratio - sg.ratio) < 1e-12
                and abs(p.move_ms - sg.move_ms) < 1e-12
                and abs(p.gain - sg.gain) < 1e-12
                and p.edit_ids == sg.edit_ids):
            p.end_sec = sg.end_sec
            continue
        out.append(sg)
    return out


FIT_HOLD_MAX = 3        # `_fit` が最後のサンプルを保持して埋める最大の長さ（それ以上の不足は 0 で埋める）


def _bundleable(sg):
    """束ねられる区間（時間を伸縮・移動するかピッチを変えるだけの、ふつうの区間）。"""
    return sg.silence_sec <= 0.0 and sg.ratio > 0.0 and sg.gain > 0.0


def _dur(g):
    return g.end_sec - g.start_sec


def _olen(g):
    return _dur(g) * g.ratio


def _shift_curve(cp, dt):
    """曲線（区間の頭からの相対秒）を、頭が dt 手前へ伸びたぶん後ろへずらす。"""
    return [[float(t) + dt, float(c)] for t, c in cp] if cp else cp


def bundle_short_segments(segs, t0, t1, min_out_sec, min_gap_sec=MIN_GAP_MS / 1000.0):
    """出力の長さが min_out_sec 未満の区間を、隣り合うものと束ねて 1 つの再合成にする。

    `_join` のクロスフェードは両側とも**区間の出力の長さ**を超えられない（超えるとつなぐ相手が足りない）ので、
    極短い区間（「ガイドへ寄せる」の細かい伸縮は 0.5 ms まである）は 0.1〜0.2 ms のクロスフェードに縮み、
    前の再合成の端と次の音が段差でつながって 1 サンプルのクリックになる。次の順で長さを稼ぐ
    （どちらも**出力の長さの合計は変えない**ので、後ろの位置は動かない）:

      1. **接している**（隙間が無い）隣の区間の長い方へ束ねる。束ねた区間は長い方のピッチ（cents・曲線）・
         移動・編集 id を引き継ぎ、伸縮比だけを「束ねた出力の長さ ÷ 束ねた元の長さ」にする
         （短い側の中の伸縮・ピッチの細かい違いは隣に溶ける。20 ms 未満の中身なので耳には届かない）。
      2. 孤立していて（前後に原音のままの隙間がある）まだ短い区間は、その隙間から前後へ足りない分だけ
         借りて広げる（広げた分は等倍で、区間のピッチ・伸縮比に畳む）。隙間は MIN_GAP_MS を割らない
         （割るときは隙間ごと取り込む）。

    無音の挿入・切り取り・無音にする・フェードの印は束ねない（境目として残す）。
    渡した Segment は書き換えない（束ねた・広げた区間は新しく作る）。
    """
    segs = [replace(g, start_sec=max(t0, g.start_sec), end_sec=min(t1, g.end_sec))
            if g.silence_sec <= 0.0 and (g.start_sec < t0 or g.end_sec > t1) else g
            for g in segs]
    for _ in range(4):
        segs, c1 = _merge_touching(segs, min_out_sec)
        segs, c2 = _borrow_from_gaps(segs, t0, t1, min_out_sec, min_gap_sec)
        if not (c1 or c2):
            break
    return segs


def _merge_touching(segs, min_out_sec):
    runs, cur = [], []
    for sg in segs:
        if _bundleable(sg):
            if cur and abs(cur[-1].end_sec - sg.start_sec) < 1e-9:
                cur.append(sg)
            else:
                if cur:
                    runs.append(cur)
                cur = [sg]
        else:
            if cur:
                runs.append(cur)
            cur = []
            runs.append([sg])
    if cur:
        runs.append(cur)
    result, changed = [], False
    for run in runs:
        run = list(run)
        while len(run) > 1:
            k = min(range(len(run)), key=lambda i: _olen(run[i]))
            if _olen(run[k]) >= min_out_sec:
                break
            j = max((i for i in (k - 1, k + 1) if 0 <= i < len(run)), key=lambda i: _olen(run[i]))
            a, b = (run[j], run[k]) if j < k else (run[k], run[j])
            keep, tiny = run[j], run[k]
            src = _dur(a) + _dur(b)
            ratio = (_olen(a) + _olen(b)) / src if src > 0 else keep.ratio
            merged = replace(keep, start_sec=a.start_sec, end_sec=b.end_sec, ratio=ratio,
                             curve_points=_shift_curve(keep.curve_points, _dur(tiny)) if tiny is a
                             else keep.curve_points,
                             edit_ids=list(keep.edit_ids) + [i for i in tiny.edit_ids
                                                              if i not in keep.edit_ids])
            lo = min(j, k)
            run[lo:lo + 2] = [merged]
            changed = True
        result.extend(run)
    return result, changed


def _borrow_from_gaps(segs, t0, t1, min_out_sec, min_gap_sec):
    """孤立した短い区間を、前後の原音のままの隙間から借りて min_out_sec まで広げる（`bundle_short_segments` の 2）。"""
    segs = list(segs)
    changed = False
    cursor, m_prev = t0, 0.0         # `render_range` のループと同じ数え方（累積の位置と、移動の繰り越し）
    for i, sg in enumerate(segs):
        if sg.silence_sec > 0:
            cursor = max(cursor, sg.start_sec)
            continue
        s, e = max(t0, sg.start_sec), min(t1, sg.end_sec)
        if _bundleable(sg) and _olen(sg) < min_out_sec:
            need = min_out_sec - _olen(sg)
            # 手前の隙間
            l_src = s - cursor
            l_out = l_src + (sg.move_ms - m_prev) / 1000.0
            # 後ろの隙間（次が無ければ末尾）
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            if nxt is None:
                r_src = t1 - e
                r_out = r_src - sg.move_ms / 1000.0
            elif nxt.silence_sec > 0:
                r_src = r_out = nxt.start_sec - e
            else:
                r_src = max(t0, nxt.start_sec) - e
                r_out = r_src + (nxt.move_ms - sg.move_ms) / 1000.0

            def room(src, out):
                a = min(src, out)
                if a <= 1e-9:
                    return 0.0
                if abs(src - out) < 1e-9 and src <= need + min_gap_sec:
                    return src                  # 取り込んだ残りが MIN_GAP を割るなら隙間ごと
                return max(0.0, a - min_gap_sec)

            lm, rm = room(l_src, l_out), room(r_src, r_out)
            dl = min(need / 2.0, lm)
            dr = min(need - dl, rm)
            dl = min(need - dr, lm)
            if dl + dr > 1e-9:
                total = _dur(sg) + dl + dr
                sg = replace(sg, start_sec=s - dl, end_sec=e + dr,
                             ratio=(_olen(sg) + dl + dr) / total,
                             curve_points=_shift_curve(sg.curve_points, dl))
                segs[i] = sg
                s, e = s - dl, e + dr
                changed = True
        cursor, m_prev = e, sg.move_ms
    return segs, changed


def _fit(y, want):
    """合成結果を want サンプルに合わせる（丸めの差 1〜2 サンプルを詰める／足す）。

    足りない分は **0 ではなく最後のサンプルの保持**で埋める。0 を足すと、`_join` のクロスフェードの
    ちょうど中央に「片側が 0 のサンプル」が来て、そこだけ振幅が半分ほどに落ちる 1 サンプルのクリックになる
    （`_render_with_margins` が続きを持っているときは `want` を直接渡して続きで埋めるので、ここへは来ない）。
    """
    y = np.asarray(y, dtype="float64")
    if len(y) > want:
        return y[:want]
    if len(y) < want:
        k = want - len(y)
        tail = y[-1] if len(y) and k <= FIT_HOLD_MAX else 0.0
        return np.concatenate([y, np.full(k, tail)])
    return y


def _fade(n, rising):
    t = np.linspace(0.0, 1.0, int(n), endpoint=False)
    return np.sin(t * np.pi / 2.0) if rising else np.cos(t * np.pi / 2.0)


def _xfade_gains(a, b, rho=None):
    """重なり部分 a（出ていく側）と b（入ってくる側）のフェード係数 (ga, gb)。

    形は u = sin^2（振幅の和が 1）。2 つの音の相関 rho（0..1）から
    k = 1 / sqrt(u^2 + (1-u)^2 + 2 rho u (1-u)) を掛けて、重ねた音のパワーが保たれるようにする。
    rho = 1（同じ音）なら k = 1、rho = 0（無相関）なら等パワーと同じ。
    """
    n = len(a)
    t = (np.arange(n) + 0.5) / max(1, n)
    u = np.sin(t * np.pi / 2.0) ** 2            # 入ってくる側
    if rho is None:
        den = np.sqrt(np.sum(a * a) * np.sum(b * b))
        rho = float(np.sum(a * b) / den) if den > 1e-20 else 1.0
    rho = float(np.clip(rho, 0.0, 1.0))
    k = 1.0 / np.sqrt(np.maximum(u ** 2 + (1 - u) ** 2 + 2 * rho * u * (1 - u), 1e-6))
    return (1 - u) * k, u * k, rho


class Renderer:
    """1 プロジェクトぶんのレンダラ（バックエンドの状態を使い回す）。"""

    def __init__(self, x, sr, f0, voiced, hop_s=0.010, backend=None, ref=None):
        """ref: 別の Renderer（同じ素材をモノラル化したもの）。praat ではその解析（パルス・PitchTier）と
        音量合わせのゲインを共有する（ステレオ書き出しでチャンネル間をそろえる）。他のバックエンドは無視。"""
        self.x = np.asarray(x, dtype="float64")
        self.sr = int(sr)
        self.backend_requested = backend
        self.backend_name = resolve_backend_name(backend)   # None = 既定（praat。無ければ psola）
        self.backend = get_backend(self.backend_name)
        if ref is not None and self.backend_name == "praat" and ref.backend_name == "praat":
            self.ctx = self.backend.prepare(self.x, self.sr, f0, voiced, hop_s, ref=ref.ctx)
        else:
            self.ctx = self.backend.prepare(self.x, self.sr, f0, voiced, hop_s)

    def _cut(self, a_sec, b_sec):
        a = max(0, int(round(a_sec * self.sr)))
        b = min(len(self.x), int(round(b_sec * self.sr)))
        if b <= a:
            return np.zeros(0)
        return self.x[a:b].copy()

    def render_range(self, t0, t1, segments, xfade_ms=XFADE_MS, fit_out_sec=None, xfade_rhos=None,
                     xfade_lags=None):
        """[t0, t1) を編集して返す。(y, info)

        `fit_out_sec` を渡すと**出力の長さをその秒数ぴったりに合わせる**
        （`export_wav` が「元と同じ長さ」を守るため）。差は**末尾の非編集区間**が
        吸収する（末尾は無音を選んであるので、ここを伸び縮みさせても聞こえない）。
        末尾に吸収できる隙間が無いときは 0 詰め／切り詰めにして warnings に出す。

        `xfade_rhos`: つなぎ目ごとのクロスフェードの相関（info["xfade_rhos"]）。多チャンネルの書き出しで
        モノラル化した音で決めた値を全チャンネルにそろえる（チャンネルごとに測ると、ゲインが
        チャンネルで違って L + R の関係・定位が崩れる）。`xfade_lags` も同じ（つなぎ目ごとに区間を動かした
        サンプル数。info["xfade_lags"]）。
        """
        sr = self.sr
        hx = max(1, int(round(xfade_ms / 2000.0 * sr)))     # 片側マージン = 10 ms
        fade = 2 * hx                                       # クロスフェード長 = 20 ms
        tau = int(round(ALIGN_MAX_MS / 1000.0 * sr))        # 位相をそろえるために区間を動かす最大
        fades = [s.fade for s in segments if getattr(s, "fade", None) is not None]
        segs = [s for s in segments if not s.is_identity() and getattr(s, "fade", None) is None
                and ((s.end_sec > t0 and s.start_sec < t1)
                     or (s.silence_sec > 0 and t0 <= s.start_sec <= t1))]
        segs.sort(key=lambda s: (s.start_sec, s.end_sec))
        segs = bundle_short_segments(segs, t0, t1, fade / float(sr))

        chunks = []          # (kind, core_audio, src_start_sec, src_end_sec, out_len)
        warnings = []
        cursor = t0
        m_prev = 0.0
        min_gap = MIN_GAP_MS / 1000.0
        base = int(round(t0 * sr))
        pos = [t0]           # 出力側の累積秒（編集後の時間軸）

        def slot(out_sec):
            """出力の累積秒を丸めて、この chunk が占めるサンプル数を決める。"""
            a = int(round(pos[0] * sr))
            pos[0] += max(0.0, out_sec)
            return max(0, int(round(pos[0] * sr)) - a)

        for sg in segs:
            if sg.silence_sec > 0:
                t = sg.start_sec
                if t > cursor:
                    chunks.append(self._gap_chunk(cursor, t, None, slot(t - cursor)))
                    cursor = t
                chunks.append({"kind": "silence", "audio": np.zeros(slot(sg.silence_sec)),
                               "src": (t, t), "edit_ids": sg.edit_ids})
                continue
            s = max(t0, sg.start_sec)
            e = min(t1, sg.end_sec)
            if e <= s:
                continue
            gap_src = s - cursor
            gap_out = gap_src + (sg.move_ms - m_prev) / 1000.0
            if gap_src > 0:
                if gap_out < min_gap:
                    warnings.append(
                        "%.3f s の手前の隙間が足りず、移動を %.0f ms に制限した"
                        % (s, (min_gap - gap_src + m_prev / 1000.0) * 1000.0))
                    gap_out = min(gap_src, min_gap)
                chunks.append(self._gap_chunk(cursor, s, None, slot(gap_out)))
            elif abs(gap_out) > 1e-9 and cursor > t0:
                warnings.append("%.3f s は直前の編集と隣接していて移動を吸収できない" % s)
            want = slot((e - s) * sg.ratio)
            if sg.ratio <= 0.0:                       # crop: この区間は出さない
                core, pre, post, info, full = np.zeros(0), None, None, {"cropped": True}, None
            elif sg.gain <= 0.0:                      # mute: 長さはそのまま、中身は無音（再合成しない）
                core, pre, post, info, full = (np.zeros(0), np.zeros(hx), np.zeros(hx),
                                               {"muted": True}, None)
            elif abs(sg.cents) < 1e-9 and abs(sg.ratio - 1.0) < 1e-9 and not sg.curve_points:
                # 移動だけ: 中身は原音そのもの（再合成しない）
                core, post = self._verbatim(s, want, hx)
                pre, info, full = None, {"verbatim": True}, None
            else:
                core, pre, post, info, full = self._render_with_margins(
                    s, e, hx, cents=sg.cents, ratio=sg.ratio, curve_points=sg.curve_points, want=want,
                    slack=tau)
            chunks.append({"kind": "muted" if sg.gain <= 0.0 and sg.ratio > 0.0 else "edited",
                           "audio": _fit(core, want), "pre": pre, "post": post, "full": full,
                           "src": (s, e), "edit_ids": sg.edit_ids, "info": info})
            cursor = e
            m_prev = sg.move_ms

        tail_src = t1 - cursor
        tail_out = tail_src - m_prev / 1000.0
        if tail_src > 0:
            if fit_out_sec is not None:
                done = int(round(pos[0] * sr)) - base
                want = max(0, int(round(fit_out_sec * sr)) - done)
            else:
                want = slot(max(tail_out, 0.0))
            chunks.append(self._gap_chunk(cursor, t1, None, want))

        for c in chunks:                                  # バックエンドの警告（Praat → psola の代替など）
            for w in (c.get("info") or {}).get("warnings", []):
                if w not in warnings:
                    warnings.append(w)
        rhos, lags = [], []
        y = self._join(chunks, hx, fade, rhos=xfade_rhos, used=rhos, lags=xfade_lags, used_lags=lags,
                       tau=tau)
        if fit_out_sec is not None:
            want = int(round(fit_out_sec * sr))
            if len(y) != want:
                warnings.append("出力を %d → %d サンプルに合わせた（末尾で吸収しきれなかった）"
                                % (len(y), want))
                y = (y[:want] if len(y) > want
                     else np.concatenate([y, np.zeros(want - len(y))]))
        if fades:                                   # ノートのフェード（issue #20）: 音量だけ
            from ..project.fades import apply_fades
            y = apply_fades(y, sr, t0, fades)
        return y, {"backend": self.backend_name, "chunks": len(chunks),
                   "edited_chunks": sum(1 for c in chunks if c["kind"] in ("edited", "muted")),
                   "warnings": warnings,
                   "out_samples": int(len(y)),
                   "xfade_rhos": rhos,
                   "xfade_lags": lags,
                   "out_sec": round(len(y) / sr, 4)}

    def _verbatim(self, a_sec, want, hx):
        """原音そのままの want サンプル（a_sec から）と、その**直後の続き** hx サンプル（post）。

        `slot()` が累積の秒から決めた長さ want は、区間の長さを丸めたものと 1 サンプルずれることがある。
        足りない分は 0 ではなく**原音の続き**で埋め、post もその続きの先頭から取る（長さと post が噛み合う）。
        素材の終わりを越えた分だけ 0。"""
        ia = max(0, int(round(a_sec * self.sr)))
        n = len(self.x)
        core = self.x[ia:min(n, ia + want)]
        if len(core) < want:
            core = np.concatenate([core, np.zeros(want - len(core))])
        else:
            core = core.copy()
        return core, self.x[min(n, ia + want):min(n, ia + want + hx)].copy()

    def _render_with_margins(self, s, e, hx, cents=0.0, ratio=1.0, curve_points=None, want=None,
                             slack=0):
        """[s, e) を再合成し、前後に hx サンプルぶんの**同じ設定で続けた**マージンを付けて返す。

        返り値 (core, pre, post, info)。core の長さは want（既定は `backend.render(s, e)` と同じ）。
        want は呼び出し側が累積の秒から決めた長さ。区間の長さを丸めたものと 1 サンプルずれることがあり、
        そのとき core を切る位置を伸ばし縮みさせて**再合成の続き**で足りない分を埋める
        （core を 0 で埋めると、つなぎ目のクロスフェードの中央が 1 サンプルのクリックになる）。
        post は core の直後の続きから取る。
        マージンは素材の端で足りなければ短くなる（_join が短い方に合わせる）。

        slack > 0 なら、マージンを hx + slack サンプル取り、再合成した全体 (y, core の先頭の位置) を
        5 つ目の返り値に付ける（`_join` が区間を ±slack 動かして位相をそろえるため）。無ければ None。
        """
        sr = self.sr
        n = len(self.x)
        a = int(round(s * sr))
        b = int(round(e * sr))
        if want is None:
            want = max(1, int(round((b - a) * float(ratio))))
        want = int(want)
        if want <= 0:
            return np.zeros(0), None, None, {}, None
        ext = int(np.ceil((hx + slack) / float(ratio))) + 1
        ea = min(ext, a)
        eb = min(ext, max(0, n - b))
        cp = None
        if curve_points:
            cp = [[float(t) + ea / sr, float(c)] for t, c in curve_points]
        y, info = self.backend.render(self.ctx, (a - ea) / sr, (b + eb) / sr, cents=cents,
                                      ratio=ratio, curve_points=cp)
        y = np.asarray(y, dtype="float64")
        npre = int(round(ea * float(ratio)))
        core = _fit(y[npre:npre + want], want)
        pre = y[max(0, npre - hx):npre]
        post = y[npre + want:npre + want + hx]
        full = (y, npre) if slack > 0 and len(y) >= npre + want else None
        return core, pre, post, info, full

    def _gap_chunk(self, a_sec, b_sec, out_sec, want=None):
        """非編集区間。長さが変わらないなら原音そのまま、変わるなら伸縮する。

        want（サンプル数）を渡すとそれに合わせる（累積位置から決めた長さ）。"""
        src = self._cut(a_sec, b_sec)
        if want is None:
            want = max(0, int(round(out_sec * self.sr)))
        if want == 0:
            return {"kind": "gap", "audio": np.zeros(0), "src": (a_sec, b_sec)}
        hx = max(1, int(round(XFADE_MS / 2000.0 * self.sr)))
        if abs(want - len(src)) <= 1:
            audio, post = self._verbatim(a_sec, want, hx)
            return {"kind": "gap", "audio": audio, "post": post,
                    "src": (a_sec, b_sec), "verbatim": True}
        r = want / max(1, len(src))
        tau = int(round(ALIGN_MAX_MS / 1000.0 * self.sr))
        core, pre, post, info, full = self._render_with_margins(a_sec, b_sec, hx, cents=0.0,
                                                                ratio=float(np.clip(r, 0.25, 4.0)),
                                                                want=want, slack=tau)
        return {"kind": "gap", "audio": core, "pre": pre, "post": post, "full": full,
                "src": (a_sec, b_sec), "verbatim": False, "stretched_to": round(r, 5), "info": info}

    def _join(self, chunks, hx, fade, rhos=None, used=None, lags=None, used_lags=None, tau=0):
        """クロスフェードつきの重ね合わせ。長さは chunk の core の合計と一致する。

        境界ごとに、出ていく側の [core の最後 h | post h] と、入ってくる側の
        [pre h | core の最初 h] を重ねる（h <= hx。どちらかのマージンや core が短ければ短い方に合わせる）。
        pre / post は chunk が持っていればそれ（再合成した続き）、無ければ原音の続き。

        **1 つの chunk の頭と尻のクロスフェードは、その chunk の中で重ならない**（頭の h + 尻の h <= chunk の長さ）。
        重なると、後から書く方が先に書いた混ぜ具合を途中から上書きして、そこが 1 サンプルの段差になる
        （20 ms 未満の隙間や短い区間で起きた）。足りないときは両側を比例して縮める。

        **位相をそろえる**（tau > 0 のとき）: 隣り合う区間は別々に再合成するので、つなぎ目で位相がそろわない
        （相関 rho が 0 に近い → 等パワーで混ざって、ピッチが細かく揺れる）。入ってくる側が再合成した区間
        （`full` を持つ）なら、出ていく側との相互相関が最大になるよう、その区間の中身を ±tau サンプルの範囲で
        **丸ごと動かす**（再合成した続き `full` から切り直すので、長さは変わらず、マージンも続きのまま）。
        相関が ALIGN_MIN_GAIN 以上よくならなければ動かさない。原音のままの区間は動かさない
        （編集していない区間は元のサンプルのまま、が約束）。動かした量は used_lags に残し、lags で渡せば
        同じ動かし方になる（多チャンネルでモノラルの結果をそろえる。rhos と同じ）。
        """
        chunks = [c for c in chunks if len(c["audio"]) > 0]
        if not chunks:
            return np.zeros(0)
        total = int(sum(len(c["audio"]) for c in chunks))
        auds = [np.asarray(c["audio"], dtype="float64") for c in chunks]
        shift = [0] * len(chunks)

        def view(i, d):
            """chunk i（再合成した区間）を d サンプル後ろへ動かした (audio, pre, post)。"""
            y, npre = chunks[i]["full"]
            n = len(auds[i])
            return (y[npre - d:npre - d + n], y[npre - d - hx:npre - d],
                    y[npre - d + n:npre - d + n + hx])

        posts, pres, hs = [], [], []
        for i in range(len(chunks) - 1):
            L, R = chunks[i], chunks[i + 1]
            # 挿入した無音には原音の端を足さない（隣の音がその中で短くフェードする）
            if L.get("kind") == "silence":
                post = np.zeros(hx)
            elif shift[i]:
                post = view(i, shift[i])[2]
            else:
                post = L.get("post")
                if post is None:
                    post = self._cut(L["src"][1], L["src"][1] + hx / self.sr)
            if R.get("kind") == "silence":
                pre = np.zeros(hx)
            else:
                pre = R.get("pre")
                if pre is None:
                    pre = self._cut(R["src"][0] - hx / self.sr, R["src"][0])
            h = min(hx, len(post), len(pre), len(auds[i]), len(auds[i + 1]))
            if tau > 0 and h > 0 and L.get("kind") != "silence" and R.get("full") is not None:
                right = None
                if i + 2 < len(chunks):                  # 次が動かせない区間なら、その側の相関も見る
                    R2 = chunks[i + 2]
                    if R2.get("full") is None and R2.get("kind") not in ("silence", "muted"):
                        pre2 = R2.get("pre")
                        if pre2 is None:
                            pre2 = self._cut(R2["src"][0] - hx / self.sr, R2["src"][0])
                        h2 = min(hx, len(pre2), len(auds[i + 2]), len(auds[i + 1]))
                        if h2 > 0:
                            right = (np.concatenate([np.asarray(pre2[-h2:]), auds[i + 2][:h2]]), h2)
                d = self._align(auds[i], post, R, h, hx, tau,
                                None if lags is None or i >= len(lags) else lags[i], right)
                if d:
                    auds[i + 1], pre, _ = view(i + 1, d)
                    shift[i + 1] = d
            if used_lags is not None:
                used_lags.append(int(shift[i + 1]))
            posts.append(post)
            pres.append(pre)
            hs.append(min(hx, len(post), len(pre), len(auds[i]), len(auds[i + 1])))
        out = np.concatenate(auds)
        for _ in range(len(hs) + 2):              # 頭 + 尻 > chunk の長さ を比例して縮める（収束するまで）
            changed = False
            for i in range(len(chunks)):
                lh = hs[i - 1] if i > 0 else 0
                rh = hs[i] if i < len(hs) else 0
                n = len(auds[i])
                if lh + rh > n:
                    nl = min(lh, n * lh // (lh + rh))
                    nr = min(rh, n - nl)
                    if i > 0:
                        hs[i - 1] = nl
                    if i < len(hs):
                        hs[i] = nr
                    changed = True
            if not changed:
                break
        pos = 0
        for i in range(len(chunks) - 1):
            pos += len(auds[i])
            h = hs[i]
            if h <= 0:
                continue
            post, pre = posts[i], pres[i]
            a = np.concatenate([auds[i][-h:], np.asarray(post[:h])])
            b = np.concatenate([np.asarray(pre[-h:]), auds[i + 1][:h]])
            k = len(used) if used is not None else 0
            r = rhos[k] if rhos is not None and k < len(rhos) else None
            ga, gb, r = _xfade_gains(a, b, r)
            if used is not None:
                used.append(r)
            out[pos - h:pos + h] = a * ga + b * gb
        return out[:total]

    def _align(self, l_audio, l_post, R, h, hx, tau, given=None, right=None):
        """入ってくる再合成の区間 R を何サンプル後ろへ動かせば、出ていく側 [l_audio の最後 h | l_post] と
        そろうか（相互相関が最大になる量。動かさないなら 0）。given があればそれ（動かせる範囲に丸める）。

        right: R の次が動かせない区間（原音のまま）のとき、そのつなぎ目の [pre の最後 h2 | 先頭 h2]。
        R の尻の側の相関も（ALIGN_RIGHT_WEIGHT 倍で）足して、頭と尻の折り合いのよい量を選ぶ。再合成した区間の
        位相は中でずれていく（無声をまたぐ・ピッチを変える）ので、両端が同時にそろうとは限らない。頭の側を優先する。
        相関の差が小さい量の中では、動かす量の小さい方を選ぶ。"""
        y, npre = R["full"]
        n = len(R["audio"])
        te = min(tau, npre - hx, len(y) - (npre + n + hx))     # 動かしても pre・post が hx 取れる範囲
        if te <= 0:
            return 0
        if given is not None:
            return int(np.clip(given, -te, te))
        a = np.concatenate([l_audio[-h:], np.asarray(l_post[:h])])
        score = _ncc(y[npre - h - te:npre + h + te], a, te)
        if score is None:
            return 0
        if right is not None:
            ref, h2 = right
            s2 = _ncc(y[npre + n - h2 - te:npre + n + h2 + te], ref, te)
            if s2 is not None:
                score = score + ALIGN_RIGHT_WEIGHT * s2
        best = float(np.max(score))
        if best < float(score[te]) + ALIGN_MIN_GAIN:
            return 0
        cand = np.flatnonzero(score >= best - ALIGN_TIE)
        j = int(cand[np.argmin(np.abs(cand - te))])
        return te - j                                               # j = te - d


def _ncc(seg, ref, te):
    """seg を ref に対して j = 0..2te サンプルずらしたときの正規化相互相関（j = te がずらさない）。足りなければ None。"""
    m = len(ref)
    if len(seg) < m + 2 * te:
        return None
    er = float(np.dot(ref, ref))
    if er < 1e-12:
        return None
    from scipy.signal import fftconvolve
    c = fftconvolve(seg, ref[::-1], mode="valid")                  # c[j] = sum ref[k] * seg[j + k]
    cs = np.concatenate([[0.0], np.cumsum(seg * seg)])
    es = cs[m:m + 2 * te + 1] - cs[:2 * te + 1]
    return c / np.sqrt(er * np.maximum(es, 1e-12))


def build_renderer(project, backend=None):
    x, sr = project.audio("take")
    f0r = project.take_f0
    return Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s, backend=backend)


def segments_for(project, start_sec=None, end_sec=None):
    """再合成に使う Segment 列（ノートの変わり目のなだらかさ・鉛筆の層まで当てたもの）。"""
    from ..project.pitch import layered_segments
    return layered_segments(project)
