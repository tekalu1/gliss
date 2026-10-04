# -*- coding: utf-8 -*-
"""ガイドとの対応付け（DTW）。

**既定は MFCC + `librosa.sequence.dtw`（ISC）**。段階2 のスパイク
（評価の記録）で、同一歌手・同じ歌詞の別テイク C ↔ C2 の
音素境界を写した誤差を比べた結果:

| 特徴量 | 中央値 | 90%点 | 最大 |
|---|---|---|---|
| 線形スケール（下限の基準） | 29.5 ms | — | 245 ms |
| synctoolbox MrMsDTW（chroma + DLNCO） | 26.8 ms | 167 ms | 240 ms |
| **MFCC(19) + librosa DTW** | **12.8 ms** | **40 ms** | **100 ms** |

テイク間でキーが 4〜5 半音違うと chroma の pitch-class が合わず、
**線形スケールとほぼ同じ精度まで落ちる**。そのため既定を MFCC に替えた。
synctoolbox（MIT）は `method="synctoolbox"` で残してある。

- 出力は warping path（テイクのフレーム ↔ ガイドのフレーム）と、そこから作る
  区分線形の時間写像 take_sec → guide_sec（および逆）。
- ノート単位のずれ: ピッチ = セント差（テイクの中央値 − 対応するガイド区間の中央値）、
  タイミング = ms（テイクのノート頭 − ガイドの対応ノート頭を逆写像でテイク時間に戻したもの）。
- 音素境界のずれ: `boundary_deviations()`（段階2）。

synctoolbox / librosa はどちらも MIT / ISC。dtw-python（GPL）は使わない。
"""
from dataclasses import dataclass, asdict

import numpy as np

from ..audio import resample
from .f0 import hz_to_midi

FEATURE_RATE = 50
SYNC_SR = 22050

MFCC_SR = 22050
MFCC_HOP = 220                  # 22050 Hz で 10 ms
MFCC_N = 20                     # C0 を落として 19 次元使う
DEFAULT_METHOD = "mfcc"


@dataclass
class Deviation:
    note_id: str
    start_sec: float
    end_sec: float
    guide_note_id: str | None
    take_note: str | None
    guide_note: str | None
    pitch_cents: float | None        # + はテイクがガイドより高い
    timing_ms: float | None          # + はテイクがガイドより遅い
    confidence: float
    reason: str | None = None

    def to_json(self):
        return asdict(self)


def _features(x, sr):
    from synctoolbox.feature.chroma import pitch_to_chroma, quantize_chroma
    from synctoolbox.feature.dlnco import pitch_onset_features_to_DLNCO
    from synctoolbox.feature.pitch import audio_to_pitch_features
    from synctoolbox.feature.pitch_onset import audio_to_pitch_onset_features

    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    y = np.ascontiguousarray(resample(x, sr, SYNC_SR)) if sr != SYNC_SR else np.ascontiguousarray(x)
    f_pitch = audio_to_pitch_features(f_audio=y, Fs=SYNC_SR, feature_rate=FEATURE_RATE,
                                      midi_min=21, midi_max=108, verbose=False)
    f_chroma = quantize_chroma(pitch_to_chroma(f_pitch=f_pitch))
    peaks = audio_to_pitch_onset_features(f_audio=y, Fs=SYNC_SR, midi_min=21, midi_max=108,
                                          verbose=False)
    f_dlnco = pitch_onset_features_to_DLNCO(f_peaks=peaks, feature_rate=FEATURE_RATE,
                                            feature_sequence_length=f_chroma.shape[1],
                                            midi_min=21, midi_max=108, visualize=False)
    return f_chroma, f_dlnco


def _mono(x, sr, target_sr):
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != target_sr:
        x = resample(x, sr, target_sr)
    return np.ascontiguousarray(x)


def _mfcc(x, sr, hop=MFCC_HOP):
    import librosa
    y = _mono(x, sr, MFCC_SR)
    m = librosa.feature.mfcc(y=y, sr=MFCC_SR, n_mfcc=MFCC_N, hop_length=hop,
                             n_fft=2048)
    m = m[1:]                                   # C0（音量）を落とす
    return (m - m.mean(axis=1, keepdims=True)) / (m.std(axis=1, keepdims=True) + 1e-8)


def _flat_cols(m):
    """特徴量の列のうち 0 ベクトルのもの（そろえた結果、窓の中が MFCC の下限より静かで全部同じ値だった）。"""
    return ~np.any(np.asarray(m) != 0.0, axis=0)


def _dtw_path(m1, m2, metric="cosine", band=None):
    """DTW の経路。**0 ベクトルの列があっても落ちない**（issue #32）。

    ほぼ無音（−110 dBFS 程度。MFCC の下限より静か）の窓は、そろえた MFCC が全フレーム 0 ベクトルになり、
    コサイン距離が 0/0 = NaN になって librosa の DTW が例外を出す。0 ベクトルとの距離は「似ていない」= 1 にする
    （0 ベクトルが無ければ librosa に任せたときと同じ行列・同じ経路）。

    band = (rate, t0, g0, offset_sec, radius_sec): **帯の制約**。テイクの秒（t0 + i / rate）− ガイドの秒
    （g0 + j / rate）が offset_sec ± radius_sec を外れる所は通さない（同じ時間軸の素材。`estimate_timeline`）。
    経路は両端（最初のフレームどうし・最後のフレームどうし）を通るので、端の近く（全体のずれ・長さの差の
    分 + `BAND_END_SEC` の角の四角）だけは帯の外も通す（ファイルの頭と尻は、ふつう歌っていない）。"""
    import librosa
    if band is not None:
        from scipy.spatial.distance import cdist
        with np.errstate(invalid="ignore", divide="ignore"):
            C = cdist(np.asarray(m1).T, np.asarray(m2).T, metric=metric)
        C = np.nan_to_num(C, nan=1.0, posinf=1.0, neginf=1.0)
        rate, t0, g0, off, rad = band
        ti = t0 + np.arange(C.shape[0]) / rate
        gj = g0 + np.arange(C.shape[1]) / rate
        outside = np.abs(ti[:, None] - gj[None, :] - off) > rad
        n1, n2 = C.shape[0] / rate, C.shape[1] / rate
        fs = abs(t0 - g0 - off) + BAND_END_SEC                    # 頭: 0 フレームどうしのずれ
        fe = abs((t0 + n1) - (g0 + n2) - off) + BAND_END_SEC      # 尻
        outside &= ~((ti[:, None] < t0 + fs) & (gj[None, :] < g0 + fs))
        outside &= ~((ti[:, None] > t0 + n1 - fe) & (gj[None, :] > g0 + n2 - fe))
        C[outside] = BAND_PENALTY
        _, wp = librosa.sequence.dtw(C=C, subseq=False, backtrack=True)
        return np.asarray(wp[::-1].T, dtype="float64")
    if metric == "cosine" and (_flat_cols(m1).any() or _flat_cols(m2).any()):
        from scipy.spatial.distance import cdist
        with np.errstate(invalid="ignore", divide="ignore"):
            C = cdist(np.asarray(m1).T, np.asarray(m2).T, metric=metric)
        C = np.nan_to_num(C, nan=1.0, posinf=1.0, neginf=1.0)
        _, wp = librosa.sequence.dtw(C=C, subseq=False, backtrack=True)
    else:
        _, wp = librosa.sequence.dtw(X=m1, Y=m2, metric=metric, subseq=False, backtrack=True)
    return np.asarray(wp[::-1].T, dtype="float64")


# ---------------------------------------------------------------- 同じ時間軸の素材（帯の制約）
# 同じ DAW の曲から書き出したテイクとガイドは、同じ秒に同じ所を歌っている（ずれは歌い手の走り・もたりの
# ±0.3 秒ほど）。ところが曲全体の DTW は、テイクが歌っていない所の多いファイルで別のフレーズに写り、
# 250 ms を超えて外れる（手元の曲で、歌った音程ノートの 2〜4 割。中央値で 2 秒外れた組もある）。
# そこで先に、発音の強さの包絡の相互相関で**全体のずれ**を測り、声のある窓ごとのずれが全体のずれに
# そろっていれば同じ時間軸とみなして、DTW の経路を「全体のずれ ± BAND_SEC」の帯の中に限る。
# 窓が足りない短い素材（8 秒未満）・別の演奏（窓ごとのずれがばらばら）は以前どおり制約しない。
ALIGN_VERSION = 2               # 対応付けの中身を変えたら上げる（キャッシュの鍵。1 = 帯の制約なし）
ENV_HOP = MFCC_HOP              # 包絡の間隔（10 ms）
TL_MAX_LAG_SEC = 6.0            # 全体のずれを探す範囲
TL_WIN_SEC = 8.0                # 所ごとのずれを確かめる窓の長さ（半分ずつずらす）
TL_LOCAL_LAG_SEC = 1.5          # 窓の中で探す範囲（全体のずれの前後）
TL_AGREE_SEC = 0.15             # 窓のずれが全体のずれとこれ以内なら「そろっている」
TL_MIN_WINDOWS = 3              # 同じ時間軸とみなすのに要る窓の数
TL_MIN_AGREE = 0.6              # そろっている窓の割合の下限
BAND_SEC = 0.3                  # 帯の半径（全体のずれからこれを超えて外れる対応は取らない。146 BPM の 1 拍 0.41 秒より狭く）
BAND_PENALTY = 1e3              # 帯の外のコスト（コサイン距離は 0〜2）
BAND_LOOSE_SEC = 1.0            # 「たぶん同じ時間軸」（下）の帯の半径
TL_LOOSE_MAX_SEC = 0.15         # たぶん同じ時間軸: 全体のずれがこれ以内で、
TL_LOOSE_AGREE = 0.25           # そろっている窓がこの割合以上（窓は 1 つ以上）
BAND_END_SEC = 0.5              # 経路の両端へつなぐために帯の外も通す幅（端からのずれの分に足す）


def _envelopes(x, sr):
    """発音の強さ（onset strength）と RMS の包絡（10 ms）。"""
    import librosa
    y = _mono(x, sr, MFCC_SR)
    on = librosa.onset.onset_strength(y=y, sr=MFCC_SR, hop_length=ENV_HOP)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=ENV_HOP)[0]
    n = min(len(on), len(rms))
    return on[:n].astype("float64"), rms[:n].astype("float64")


def _znorm(v):
    v = np.asarray(v, dtype="float64")
    return (v - v.mean()) / (v.std() + 1e-9)


def estimate_timeline(take_x, take_sr, guide_x, guide_sr):
    """テイクとガイドが**同じ時間軸**か。全体のずれ（テイク − ガイド、秒）と、声のある窓ごとのずれの一致。

    返り値: {"offset_sec", "same", "windows", "agree", "peak"}。same は窓が `TL_MIN_WINDOWS` 以上あり、
    その `TL_MIN_AGREE` 以上で窓のずれが全体のずれから `TL_AGREE_SEC` 以内のとき。"""
    from scipy.signal import correlate
    ta, trms = _envelopes(take_x, take_sr)
    ga, grms = _envelopes(guide_x, guide_sr)
    rate = MFCC_SR / float(ENV_HOP)
    if not len(ta) or not len(ga):
        return {"offset_sec": 0.0, "same": False, "windows": 0, "agree": 0.0, "peak": 0.0}
    c = correlate(_znorm(ta), _znorm(ga), mode="full", method="fft") / max(1, min(len(ta), len(ga)))
    lags = np.arange(-len(ga) + 1, len(ta))
    m = np.abs(lags) <= int(TL_MAX_LAG_SEC * rate)
    k = int(np.argmax(c[m]))
    lag = int(lags[m][k])
    off = lag / rate
    t_floor = max(1e-4, float(np.percentile(trms, 95)) * 0.05)
    g_floor = max(1e-4, float(np.percentile(grms, 95)) * 0.05)
    w = int(TL_WIN_SEC * rate)
    L = int(TL_LOCAL_LAG_SEC * rate)
    loc = []
    for a in range(0, len(ta) - w + 1, max(1, w // 2)):
        g0, g1 = a - lag - L, a + w - lag + L
        if g0 < 0 or g1 > len(ga):
            continue
        if (trms[a:a + w] > t_floor).mean() < 0.25 or (grms[g0:g1] > g_floor).mean() < 0.15:
            continue                 # 片方が歌っていない窓は手がかりにならない
        cc = correlate(_znorm(ga[g0:g1]), _znorm(ta[a:a + w]), mode="valid")
        loc.append((a - (g0 + int(np.argmax(cc)))) / rate)
    agree = float(np.mean([abs(v - off) <= TL_AGREE_SEC for v in loc])) if loc else 0.0
    same = bool(len(loc) >= TL_MIN_WINDOWS and agree >= TL_MIN_AGREE)
    # たぶん同じ時間軸: 声の少ないテイク（掛け声だけ）・ガイドと歌い回しの違う所の多いテイクは窓がそろわないが、
    # Gliss のガイドはタイムライン上の位置で重ねるので、全体のずれが小さく窓が少しでもそろえば、DTW が何秒も
    # 離れた所へ写らないように広い帯（`BAND_LOOSE_SEC`）を掛ける（素材で −22 秒の全体のずれが出た）
    loose = bool(not same and loc and abs(off) <= TL_LOOSE_MAX_SEC and agree >= TL_LOOSE_AGREE)
    return {"offset_sec": round(off, 3), "windows": len(loc), "agree": round(agree, 3),
            "peak": round(float(c[m][k]), 3), "same": same, "loose": loose}


def align_mfcc(take_x, take_sr, guide_x, guide_sr, metric="cosine"):
    """MFCC + librosa DTW。(2, K) のフレーム対応（特徴レート 100 Hz）。"""
    return _dtw_path(_mfcc(take_x, take_sr), _mfcc(guide_x, guide_sr), metric)


# ---------------------------------------------------------------- 長い素材（曲全体）
# librosa の DTW は **コスト行列を丸ごと確保する**（float64）。10 ms ホップの
# 158 秒 × 156 秒は 15800 × 15600 = 2.5 億セル = 約 2 GB で、現実的でない。
# そこで 2 段にする:
#   1. ホップを粗くして曲全体を 1 回通す（セル数が MAX_DTW_CELLS 以下になるまで）
#   2. **歌詞を付けた区間（＝実際に編集するところ）だけ 10 ms で取り直す**
# 曲全体の対応は「どのフレーズがどのフレーズか」が分かればよく、
# 境界の精度が要るのは編集する区間だけ、という切り分け。
MAX_DTW_CELLS = 32_000_000      # 約 256 MB
REFINE_PAD_SEC = 0.5            # 取り直す窓の余白（テイク側）
REFINE_GUIDE_PAD_SEC = 0.35     # ガイド側にさらに足す余白


def _coarse_hop(dur1, dur2, max_cells):
    n1 = dur1 * MFCC_SR / MFCC_HOP
    n2 = dur2 * MFCC_SR / MFCC_HOP
    k = int(np.ceil(np.sqrt(max(1.0, n1 * n2 / float(max_cells)))))
    return MFCC_HOP * max(1, k), max(1, k)


def align_seconds(take_x, take_sr, guide_x, guide_sr, method=DEFAULT_METHOD,
                  refine_ranges=None, max_cells=MAX_DTW_CELLS, timeline=True):
    """(take_sec, guide_sec, feature_rate, info) を返す。長い素材は粗 → 精の 2 段。

    timeline=True: 同じ時間軸の素材（`estimate_timeline`）なら、経路を全体のずれ ± `BAND_SEC` の帯に限る
    （info["timeline"] に測った値、info["band_sec"] に帯の半径）。数（秒）を渡すと、測らずにそのずれの
    同じ時間軸とみなす（譜面ガイド）。False は帯を使わない（以前の対応付け）。"""
    if method != "mfcc":
        wp = align(take_x, take_sr, guide_x, guide_sr, method=method)
        fr = feature_rate_of(method)
        return wp[0] / fr, wp[1] / fr, fr, {"stage": "single", "hop_ms": 1000.0 / fr}

    d1 = len(np.atleast_1d(take_x)) / float(take_sr)
    d2 = len(np.atleast_1d(guide_x)) / float(guide_sr)
    if timeline is True:
        tl = estimate_timeline(take_x, take_sr, guide_x, guide_sr)
    elif timeline is False or timeline is None:
        tl = None
    else:
        tl = {"offset_sec": float(timeline), "same": True, "given": True}
    off = tl["offset_sec"] if tl is not None and (tl["same"] or tl.get("loose")) else None
    hop, k = _coarse_hop(d1, d2, max_cells)
    m1, m2 = _mfcc(take_x, take_sr, hop), _mfcc(guide_x, guide_sr, hop)
    rate = MFCC_SR / float(hop)
    rad = None if off is None else (BAND_SEC if tl["same"] else BAND_LOOSE_SEC)
    wp = _dtw_path(m1, m2, band=None if off is None else (rate, 0.0, 0.0, off, rad))
    ta, ga = wp[0] / rate, wp[1] / rate
    info = {"stage": "single" if k == 1 else "coarse", "hop_ms": round(1000.0 / rate, 1),
            "cells": int(m1.shape[1]) * int(m2.shape[1]), "refined": 0}
    if tl is not None:
        info["timeline"] = tl
        info["band_sec"] = None if rad is None else round(rad, 3)
    if k > 1 and refine_ranges:
        ta, ga, n = _refine(ta, ga, take_x, take_sr, guide_x, guide_sr, refine_ranges,
                            d1, d2, offset=off, radius=rad)
        info.update(stage="coarse+refine", refined=n, refine_hop_ms=10.0)
    return ta, ga, feature_rate_of("mfcc"), info


def _refine(ta, ga, take_x, take_sr, guide_x, guide_sr, ranges, d1, d2, offset=None, radius=BAND_SEC):
    """編集する区間だけ 10 ms ホップで取り直して、粗い対応に差し込む。

    offset（同じ時間軸の素材の全体のずれ）があれば、ガイドの窓をその位置に取り、帯の制約を掛ける。"""
    pieces = []
    done = 0
    for s, t in sorted(ranges):
        a = max(0.0, s - REFINE_PAD_SEC)
        b = min(d1, t + REFINE_PAD_SEC)
        if b - a < 0.2:
            continue
        if offset is None:
            g0 = float(np.interp(a, ta, ga)) - REFINE_GUIDE_PAD_SEC
            g1 = float(np.interp(b, ta, ga)) + REFINE_GUIDE_PAD_SEC
        else:
            g0 = a - offset - REFINE_GUIDE_PAD_SEC
            g1 = b - offset + REFINE_GUIDE_PAD_SEC
        g0, g1 = max(0.0, g0), min(d2, g1)
        if g1 - g0 < 0.2:
            continue
        tx = np.atleast_1d(take_x)[int(a * take_sr):int(b * take_sr)]
        gx = np.atleast_1d(guide_x)[int(g0 * guide_sr):int(g1 * guide_sr)]
        m1, m2 = _mfcc(tx, take_sr), _mfcc(gx, guide_sr)
        if _flat_cols(m1).all() or _flat_cols(m2).all():
            # 片側がほぼ無音（部分的に録り直したテイク・ガイドの歌っていない所）: 取り直しても手がかりが無いので
            # 粗い対応のままにする（issue #32）
            continue
        r = MFCC_SR / float(MFCC_HOP)
        wp = _dtw_path(m1, m2, band=None if offset is None else (r, a, g0, offset, radius))
        pieces.append((a, b, wp[0] / r + a, wp[1] / r + g0))
        done += 1
    if not pieces:
        return ta, ga, 0
    xs, ys = [], []
    cur = 0
    for a, b, pt, pg in pieces:
        m = ta < a
        xs.append(ta[m & (np.arange(len(ta)) >= cur)])
        ys.append(ga[m & (np.arange(len(ga)) >= cur)])
        xs.append(pt)
        ys.append(pg)
        cur = int(np.searchsorted(ta, b))
    xs.append(ta[cur:])
    ys.append(ga[cur:])
    x = np.concatenate(xs)
    y = np.concatenate(ys)
    order = np.argsort(x, kind="stable")
    x, y = x[order], np.maximum.accumulate(y[order])
    return x, y, done


def align(take_x, take_sr, guide_x, guide_sr, use_onset=True, method=DEFAULT_METHOD):
    """warping path を返す。(2, K)（0 行目=テイク、1 行目=ガイド、単位はフレーム）。

    method: "mfcc"（既定・段階2 で最良）/ "synctoolbox"（chroma + DLNCO）
    フレームの長さは `feature_rate_of(method)` で割ると秒になる。
    """
    if method == "mfcc":
        return align_mfcc(take_x, take_sr, guide_x, guide_sr)

    from synctoolbox.dtw.mrmsdtw import sync_via_mrmsdtw
    from synctoolbox.dtw.utils import make_path_strictly_monotonic

    c1, o1 = _features(take_x, take_sr)
    c2, o2 = _features(guide_x, guide_sr)
    wp = sync_via_mrmsdtw(f_chroma1=c1, f_onset1=(o1 if use_onset else None),
                          f_chroma2=c2, f_onset2=(o2 if use_onset else None),
                          input_feature_rate=FEATURE_RATE, verbose=False, alpha=0.5)
    wp = make_path_strictly_monotonic(wp)
    return np.asarray(wp, dtype="float64")


def feature_rate_of(method=DEFAULT_METHOD):
    return int(round(MFCC_SR / MFCC_HOP)) if method == "mfcc" else FEATURE_RATE


@dataclass
class Alignment:
    take_sec: np.ndarray
    guide_sec: np.ndarray
    feature_rate: int = FEATURE_RATE
    method: str = DEFAULT_METHOD

    @classmethod
    def from_wp(cls, wp, feature_rate=None, method=DEFAULT_METHOD):
        """warping path → 時間写像。**同じテイク時刻に複数のガイド時刻**が対応する
        （DTW の階段）ので、平均に畳んで単調増加にしてから持つ。"""
        fr = feature_rate if feature_rate is not None else feature_rate_of(method)
        a = np.asarray(wp[0], dtype="float64")
        b = np.asarray(wp[1], dtype="float64")
        order = np.argsort(a, kind="stable")
        a, b = a[order], b[order]
        ua, first = np.unique(a, return_index=True)
        sums = np.add.reduceat(b, first)
        counts = np.diff(np.append(first, len(b)))
        ub = sums / counts
        return cls(take_sec=ua / fr, guide_sec=ub / fr, feature_rate=fr, method=method)

    @classmethod
    def from_seconds(cls, take_sec, guide_sec, method=DEFAULT_METHOD, feature_rate=None):
        """秒で来た対応を単調増加に畳んで持つ（`align_seconds` の出口）。"""
        a = np.asarray(take_sec, dtype="float64")
        b = np.asarray(guide_sec, dtype="float64")
        order = np.argsort(a, kind="stable")
        a, b = a[order], b[order]
        ua, first = np.unique(np.round(a, 6), return_index=True)
        sums = np.add.reduceat(b, first)
        counts = np.diff(np.append(first, len(b)))
        return cls(take_sec=ua, guide_sec=sums / counts,
                   feature_rate=int(feature_rate or feature_rate_of(method)), method=method)

    def to_guide(self, t):
        return np.interp(np.asarray(t, dtype="float64"), self.take_sec, self.guide_sec)

    def to_take(self, t):
        return np.interp(np.asarray(t, dtype="float64"), self.guide_sec, self.take_sec)

    def to_json(self):
        return {"feature_rate": self.feature_rate, "method": self.method,
                "info": getattr(self, "info", None),
                "take_sec": [round(float(v), 4) for v in self.take_sec],
                "guide_sec": [round(float(v), 4) for v in self.guide_sec]}

    @classmethod
    def from_json(cls, d):
        al = cls(take_sec=np.asarray(d["take_sec"], dtype="float64"),
                 guide_sec=np.asarray(d["guide_sec"], dtype="float64"),
                 feature_rate=int(d.get("feature_rate", FEATURE_RATE)),
                 method=d.get("method", "synctoolbox"))
        al.info = d.get("info")
        return al

    def summary(self):
        off = self.guide_sec - self.take_sec
        return {
            "method": self.method,
            "dtw": getattr(self, "info", None),
            "n_points": int(len(self.take_sec)),
            "take_span_sec": [round(float(self.take_sec[0]), 3), round(float(self.take_sec[-1]), 3)],
            "guide_span_sec": [round(float(self.guide_sec[0]), 3), round(float(self.guide_sec[-1]), 3)],
            "offset_median_ms": round(float(np.median(off)) * 1000.0, 1),
            "offset_iqr_ms": round(float(np.subtract(*np.percentile(off, [75, 25]))) * 1000.0, 1),
            "offset_max_abs_ms": round(float(np.max(np.abs(off))) * 1000.0, 1),
        }


def _median_midi(f0, voiced, hop, t0, t1):
    a = max(0, int(round(t0 / hop)))
    b = min(len(f0), int(round(t1 / hop)))
    if b <= a:
        return None
    f = np.asarray(f0)[a:b]
    v = np.asarray(voiced)[a:b].astype(bool) & (f > 0)
    if not v.any():
        return None
    return float(np.median(hz_to_midi(f[v])))


MAX_NOTE_TIMING_MS = 300.0     # 描く位置でこれより離れたガイドノートとの timing_ms は出さない


def deviations(take_notes, take_f0, guide_notes, guide_f0, alignment,
               detrend_timing=True, kinds=("note",), guide_to_take=None, offset_sec=None):
    """ノート単位のずれ（セント / ms）。

    - ピッチ: テイクノートの中央値 − 対応するガイド区間の中央値（セント）
    - タイミング: テイクノートの頭 − 対応ガイドノートの頭をテイク時間へ移した値（ms）
      `guide_to_take`（`guide_timing.GuideTiming.to_take` = ガイドの時刻 + 全体のずれ）が
      あればそれで移す（画面に描くガイドの位置。全体のずれは引いてある）。無ければ DTW の
      逆写像で移して、`detrend_timing=True` なら全体の中央値オフセットを引く。
      **DTW の逆写像はテイク自身のリズムに沿う**ので、そちらのタイミングのずれは DTW の誤差に
      近い（issue #12）。
    """
    g2t = guide_to_take if guide_to_take is not None else al_to_take(alignment)
    al = alignment
    out = []
    g_pitched = [n for n in guide_notes if n.kind in kinds]
    raw_timing = []
    rows = []
    for n in take_notes:
        if n.kind not in kinds:
            continue
        gs, ge = float(al.to_guide(n.start_sec)), float(al.to_guide(n.end_sec))
        # 対応するガイドノート = 写像した区間と最も重なるもの
        best, best_ov = None, 0.0
        for g in g_pitched:
            ov = min(ge, g.end_sec) - max(gs, g.start_sec)
            if ov > best_ov:
                best, best_ov = g, ov
        gm = (_median_midi(guide_f0.f0, guide_f0.voiced, guide_f0.hop_s, gs, ge)
              if best is None else
              _median_midi(guide_f0.f0, guide_f0.voiced, guide_f0.hop_s,
                           max(gs, best.start_sec), min(ge, best.end_sec)))
        cents = None
        if gm is not None and n.pitch_midi is not None:
            cents = (n.pitch_midi - gm) * 100.0
        timing = None
        if best is not None:
            timing = (n.start_sec - float(g2t(best.start_sec))) * 1000.0
            if guide_to_take is not None and abs(timing) > MAX_NOTE_TIMING_MS:
                timing = None          # DTW の対応が別のフレーズを指している（描く位置では離れている）
            else:
                raw_timing.append(timing)
        conf = n.confidence * (1.0 if best is not None else 0.5)
        reason = None
        if gm is None:
            reason = "ガイド側に有声区間が無い（息・無音の可能性）"
        elif best is None:
            reason = "対応するガイドのノートが見つからない（重なり 0）。区間の中央値で代用"
        rows.append((n, best, cents, timing, conf, reason))

    med = float(np.median(raw_timing)) if (detrend_timing and raw_timing
                                           and guide_to_take is None) else 0.0
    for n, best, cents, timing, conf, reason in rows:
        out.append(Deviation(
            note_id=n.id, start_sec=n.start_sec, end_sec=n.end_sec,
            guide_note_id=(best.id if best is not None else None),
            take_note=n.note_name, guide_note=(best.note_name if best is not None else None),
            pitch_cents=(None if cents is None else round(cents, 1)),
            timing_ms=(None if timing is None else round(timing - med, 1)),
            confidence=round(float(conf), 3), reason=reason))
    if guide_to_take is not None and offset_sec is not None:
        med = float(offset_sec) * 1000.0
    return out, {"timing_detrend_ms": round(med, 1), "n_notes": len(out)}


def al_to_take(al):
    return lambda t: al.to_take(t)


# ---------------------------------------------------------------- 音素境界のずれ
@dataclass
class BoundaryDeviation:
    """テイクの音素境界と、ガイド側の対応する境界とのずれ（ms）。"""
    boundary_id: str
    index: int
    take_sec: float
    kind: str
    before_text: str | None
    after_text: str | None
    guide_sec: float | None          # ガイド側の時刻（ガイドの時間軸）
    target_take_sec: float | None    # それをテイクの時間軸に戻したもの
    timing_ms: float | None          # + はテイクの方が遅い
    confidence: float
    reason: str | None = None

    def to_json(self):
        return asdict(self)


def _guide_reference(guide_phonemes, guide_notes):
    """ガイド側の「境界の候補」（ガイドの時間軸、秒）と、その出どころ。"""
    if guide_phonemes is not None and guide_phonemes.boundaries:
        return (np.array([b.time_sec for b in guide_phonemes.boundaries], dtype="float64"),
                "guide_phonemes")
    times = []
    for g in guide_notes or []:
        if g.kind == "silence":
            continue
        times.append(g.start_sec)
        times.append(g.end_sec)
    return np.array(sorted(set(times)), dtype="float64"), "guide_notes"


def boundary_deviations(take_phonemes, guide_phonemes, guide_notes, alignment,
                        tolerance_ms=150.0, detrend=True, guide_to_take=None, offset_sec=None):
    """音素境界ごとのタイミングのずれ。

    - **ガイドにも歌詞があるとき**は、ガイドを直接アラインした境界を使う（最優先）。
      段階2 §4-1 のとおり、DTW で写した境界は 90%点で 40〜55 ms ずれるので、
      直接アラインできるならその方がよい。
    - ガイドに歌詞が無いときは、テイクの境界を DTW でガイドへ写し、
      **ガイド側の音符の境目**のうち最も近いものを「ガイド側の推定境界」とする。
    - 見つけたガイド側の境界をテイクの時間へ戻すのは、`guide_to_take`（ガイドの時刻 + 全体の
      ずれ。画面に描くガイドの位置）があればそれ（全体のずれは引いてある）。無ければ DTW の
      逆写像と中央値（issue #12: DTW の逆写像はテイク自身のリズムに沿う）。
    """
    if take_phonemes is None or alignment is None:
        return [], {"n": 0, "source": None}
    ref, ref_kind = _guide_reference(guide_phonemes, guide_notes)
    tol = tolerance_ms / 1000.0
    rows = []
    raw = []
    by = {p.index: p for p in take_phonemes.phonemes}
    for b in take_phonemes.boundaries:
        g_hat = float(alignment.to_guide(b.time_sec))
        gsec = tsec = ms = None
        reason = None
        if len(ref):
            j = int(np.argmin(np.abs(ref - g_hat)))
            if abs(ref[j] - g_hat) <= tol:
                gsec = float(ref[j])
                tsec = float(guide_to_take(gsec) if guide_to_take is not None
                             else alignment.to_take(gsec))
                ms = (b.time_sec - tsec) * 1000.0
                if guide_to_take is not None and abs(ms) > MAX_NOTE_TIMING_MS:
                    gsec = tsec = ms = None
                    reason = "ガイド側の境界が描く位置では離れている（DTW が外れた所）"
                else:
                    raw.append(ms)
            else:
                reason = "ガイド側に %.0f ms 以内の境界が無い" % tolerance_ms
        else:
            reason = "ガイド側の境界が取れない"
        rows.append([b, gsec, tsec, ms, reason])
    med = float(np.median(raw)) if (detrend and raw and guide_to_take is None) else 0.0
    if guide_to_take is not None and offset_sec is not None:
        med_report = float(offset_sec) * 1000.0
    else:
        med_report = med
    out = []
    for b, gsec, tsec, ms, reason in rows:
        out.append(BoundaryDeviation(
            boundary_id=b.id, index=b.index, take_sec=b.time_sec, kind=b.kind,
            before_text=(by[b.before_index].text if b.before_index in by else None),
            after_text=(by[b.after_index].text if b.after_index in by else None),
            guide_sec=(None if gsec is None else round(gsec, 4)),
            target_take_sec=(None if tsec is None else round(tsec - med / 1000.0, 4)),
            timing_ms=(None if ms is None else round(ms - med, 1)),
            confidence=round(float(b.confidence), 3), reason=reason))
    meta = {"n": len(out), "source": ref_kind, "timing_detrend_ms": round(med_report, 1),
            "matched": sum(1 for d in out if d.timing_ms is not None)}
    return out, meta
