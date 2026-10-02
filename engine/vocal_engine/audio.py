# -*- coding: utf-8 -*-
"""音声 IO とサンプル単位のユーティリティ。

段階0（方式の評価）と同じ約束:
  - 読み込みは float64 の (n, ch)
  - 書き出しは元と同じ ch 数・24 bit
  - つなぎ目は等パワークロスフェード
"""
import hashlib
import os
import threading

import numpy as np
import soundfile as sf

# ファイルの署名（サイズ・更新時刻・ID）が同じ間は、SHA-256 を取り直さない（issue #63）。トラックを選ぶたびに
# テイクとガイドのハッシュを取っていて、クラウドの仮想ドライブ上の 2 本で 0.1〜0.5 秒かかっていた
_sha_cache = {}
_sha_lock = threading.Lock()


def file_sig(path):
    """(サイズ, 更新時刻 ns, ファイル ID)。無ければ None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def sha256_file(path, chunk=1 << 20):
    key = os.path.normcase(os.path.abspath(path))
    sig = file_sig(path)
    if sig is not None:
        with _sha_lock:
            hit = _sha_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    digest = h.hexdigest()
    if sig is not None and file_sig(path) == sig:     # 読んでいる間に書き換わっていなければ覚える
        with _sha_lock:
            _sha_cache[key] = (sig, digest)
    return digest


def read_wav(path, mono=False):
    """(n, ch) float64 と sr を返す。"""
    x, sr = sf.read(path, always_2d=True, dtype="float64")
    if mono and x.shape[1] > 1:
        x = x.mean(axis=1, keepdims=True)
    return x, sr


def read_mono(path):
    """(n,) float64 と sr。"""
    x, sr = read_wav(path, mono=True)
    return np.ascontiguousarray(x[:, 0]), sr


def audio_info(path):
    info = sf.info(path)
    return {
        "path": os.path.abspath(path),
        "sr": int(info.samplerate),
        "channels": int(info.channels),
        "frames": int(info.frames),
        "duration_sec": round(info.frames / info.samplerate, 6),
        "subtype": info.subtype,
    }


def write_wav(path, x, sr, subtype="PCM_24"):
    """(n,) か (n, ch) を書く。1.0 を超えたら 0.999 にピーク正規化して記録する。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    x = np.asarray(x, dtype="float64")
    if x.ndim == 1:
        x = x[:, None]
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    normalized = peak > 1.0
    if normalized:
        x = x / peak * 0.999
    sf.write(path, x, int(sr), subtype=subtype)
    return {"peak": peak, "normalized": bool(normalized), "samples": int(x.shape[0])}


def resample(x, sr_in, sr_out):
    if sr_in == sr_out:
        return x
    import soxr
    return soxr.resample(x, sr_in, sr_out, quality="VHQ")


def equal_power_crossfade(a, b, n_fade):
    """a の末尾と b の先頭を n_fade サンプルで等パワークロスフェードして連結。

    a, b はともに (n,) か (n, ch)。
    """
    a = a[:, None] if a.ndim == 1 else a
    b = b[:, None] if b.ndim == 1 else b
    n_fade = int(min(n_fade, a.shape[0], b.shape[0]))
    if n_fade <= 0:
        return np.concatenate([a, b], axis=0)
    t = np.linspace(0.0, 1.0, n_fade, endpoint=False)[:, None]
    fo = np.cos(t * np.pi / 2.0)
    fi = np.sin(t * np.pi / 2.0)
    mid = a[-n_fade:] * fo + b[:n_fade] * fi
    return np.concatenate([a[:-n_fade], mid, b[n_fade:]], axis=0)


def frame_rms_db(x, sr, n_frames, hop_s=0.010, win_s=0.025):
    """10 ms グリッドのフレーム RMS（dBFS）。V/UV のエネルギー条件に使う。"""
    hop = max(1, int(round(hop_s * sr)))
    win = max(2, int(round(win_s * sr)))
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    # 累積和で高速化（段階0のループ版と同じ値になる）
    pad = win // 2
    xp = np.concatenate([np.zeros(pad), x ** 2, np.zeros(win)])
    cs = np.concatenate([[0.0], np.cumsum(xp)])
    centers = np.arange(n_frames) * hop
    a = centers                      # xp 上では pad 分ずれるので開始 = center - pad + pad
    b = np.minimum(centers + win, len(xp))
    n = np.maximum(b - a, 1)
    ms = (cs[b] - cs[a]) / n
    return 20.0 * np.log10(np.maximum(np.sqrt(ms), 1e-12))


def sec_to_sample(t, sr):
    return int(round(float(t) * sr))


def clamp_range(start_sec, end_sec, duration_sec):
    """範囲を [0, duration] に収める。None は端に寄せる。"""
    s = 0.0 if start_sec is None else max(0.0, float(start_sec))
    e = duration_sec if end_sec is None else min(duration_sec, float(end_sec))
    if e <= s:
        e = min(duration_sec, s + 1e-3)
    return s, e
