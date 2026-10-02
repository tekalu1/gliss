# -*- coding: utf-8 -*-
"""テストで「発音の頭」を測る、エンジンと**別の方式**の検出（3 帯域の対数エネルギーの立ち上がり）。

エンジン自身の頭の検出で結果を確かめると、同じ誤りを見逃すので、こちらで測り直す（issue #12）。
"""
import numpy as np

SR = 22050
HOP = 110                       # 5 ms


def detect_onsets_energy(x, sr, floor_db=-35.0):
    """発音の頭（秒）。3 帯域の対数エネルギーの 20 ms 前との差を足し、ピークを拾う。音量の小さい所は捨てる。"""
    import librosa
    from scipy.signal import find_peaks
    from vocal_engine.audio import resample
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    y = resample(x, sr, SR) if sr != SR else x
    y = np.ascontiguousarray(y, dtype="float32")
    St = np.abs(librosa.stft(y, n_fft=512, hop_length=HOP)) ** 2
    f = librosa.fft_frequencies(sr=SR, n_fft=512)
    nov = 0
    for lo, hi in ((80, 500), (500, 2500), (2500, 8000)):
        e = 10 * np.log10(St[(f >= lo) & (f < hi)].sum(axis=0) + 1e-10)
        e = np.convolve(e, np.ones(3) / 3, mode="same")
        de = np.zeros_like(e)
        de[4:] = e[4:] - e[:-4]                     # 20 ms 前との差
        nov = nov + np.maximum(de, 0)
    tot = 10 * np.log10(St.sum(axis=0) + 1e-10)
    tot -= tot.max()
    pk, _ = find_peaks(nov, height=np.percentile(nov, 90), distance=16)
    return np.array([k * HOP / SR for k in pk if np.max(tot[k:k + 8]) >= floor_db])
