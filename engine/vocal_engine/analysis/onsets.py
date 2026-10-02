# -*- coding: utf-8 -*-
"""発音の頭（オンセット）の検出 — 「ガイドに合わせる」のタイミングの単位（issue #12）。

音程で切ったノートの境目は、なめらかにつながる所（レガート・しゃくり）では
音の立ち上がりと 30〜100 ms ずれる。しかもテイクとガイドで切れ方が違うので、
ノートの頭どうしを合わせるとリズムが崩れる（`docs/guide-timing.md` §3）。
そこで音そのものの立ち上がり（メルスペクトルの superflux のピーク）を拾う。

- 22.05 kHz・5 ms ホップ・80 メル帯（80 Hz〜8 kHz）の dB に superflux（lag 2・周波数方向の最大値 3）
- ピークは前後 30 ms の極大・前後 100 ms の平均より `DELTA` 上・次のピークまで 80 ms 以上
- ピークから 40 ms 先までの音量が最大の `FLOOR_DB` 以上（息や残響の小さな変化を捨てる）
- ±50 ms に 6 割以上の高さの山がもう 1 つある頭は捨てる（どちらを拾うかがぶれる）
"""
from __future__ import annotations

import numpy as np

SR = 22050
HOP = 110                  # 5 ms
FLOOR_DB = -35.0
DELTA = 0.07
AMBIG_FRAMES = 10          # ±50 ms
AMBIG_RATIO = 0.6
VERSION = 2


def detect(x, sr, floor_db=FLOOR_DB, delta=DELTA):
    """発音の頭（秒、昇順）。"""
    import librosa
    from ..audio import resample
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if len(x) < sr * 0.1:
        return np.zeros(0)
    y = resample(x, sr, SR) if sr != SR else x
    y = np.ascontiguousarray(y, dtype="float32")
    S = librosa.feature.melspectrogram(y=y, sr=SR, n_fft=1024, hop_length=HOP, n_mels=80,
                                       fmin=80, fmax=8000)
    L = librosa.power_to_db(S, ref=np.max)
    env = librosa.onset.onset_strength(S=L, sr=SR, hop_length=HOP, lag=2, max_size=3)
    env = env / (np.max(env) + 1e-9)
    fr = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=HOP,
                                    units="frames", backtrack=False, pre_max=6, post_max=6,
                                    pre_avg=20, post_avg=20, delta=delta, wait=16)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=HOP)[0]
    db = 20 * np.log10(rms / (np.max(rms) + 1e-12) + 1e-12)
    out = []
    for f in fr:
        if not len(db[f:f + 8]) or np.max(db[f:f + 8]) < floor_db:
            continue
        if _ambiguous(env, int(f)):
            continue
        out.append(f * HOP / SR)
    return np.asarray(out, dtype="float64")


def _ambiguous(env, f, reach=AMBIG_FRAMES, ratio=AMBIG_RATIO):
    """ピークのすぐ近く（±reach フレーム）に同じくらいの山がもう 1 つあるか。

    そういう頭はどちらの山を拾うかが素材のわずかな違い（半サンプルのずれでも）で入れ替わり、
    30 ms 以上ぶれる。合わせる単位には使わない。"""
    a, b = max(1, f - reach), min(len(env) - 1, f + reach + 1)
    for k in range(a, b):
        if abs(k - f) <= 2:
            continue
        if env[k] >= env[k - 1] and env[k] >= env[k + 1] and env[k] >= ratio * env[f]:
            return True
    return False
