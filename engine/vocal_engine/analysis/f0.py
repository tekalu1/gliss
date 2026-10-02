# -*- coding: utf-8 -*-
"""F0 推定。RMVPE（ONNX）を正、FCPE を代替。10 ms ホップ。

V/UV は段階0（方式の評価）の判定を踏襲する:
    有声 = RMVPE が F0 を出している（salience >= 閾値）かつ フレーム RMS > -55 dBFS

RMVPE の ONNX（yxlllc/RMVPE release 230917）は salience の生値を返さず
(f0, uv) しか出さないので、confidence は「有声のまま残る最大の threshold」を
掃引して作る（段階0と同じ手口）。掃引は 13 回推論するぶん遅いので、
既定は 1 パス（confidence は 0/1 の代用値）。
"""
import os
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .. import HOP_S
from ..audio import frame_rms_db, read_mono

RMVPE_THRESHOLD = 0.03
ENERGY_FLOOR_DB = -55.0

# 重みは同梱しない。重みの置き場を参照する（無ければ分かりやすいエラー）。
# 置き場の決め方は config.models_dir()（環境変数 VOCAL_ENGINE_MODELS_DIR。旧名 VOCAL_ENGINE_MODELS も可）
from ..config import models_dir as _models_dir  # noqa: E402

DEFAULT_MODELS_DIR = _models_dir()
RMVPE_PATH = os.path.join(DEFAULT_MODELS_DIR, "rmvpe.onnx")

_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()     # 裏の準備（issue #63）と表が同時に初めて読むとき、2 回読まない


class ModelMissingError(RuntimeError):
    pass


def hz_to_cents(f0, ref_hz=10.0):
    """0（無声）は NaN にして返す。"""
    f0 = np.asarray(f0, dtype="float64")
    out = np.full(f0.shape, np.nan)
    v = f0 > 0
    out[v] = 1200.0 * np.log2(f0[v] / ref_hz)
    return out


def cents_to_hz(cents, ref_hz=10.0):
    return ref_hz * 2.0 ** (np.asarray(cents, dtype="float64") / 1200.0)


def hz_to_midi(f0):
    f0 = np.asarray(f0, dtype="float64")
    out = np.full(f0.shape, np.nan)
    v = f0 > 0
    out[v] = 69.0 + 12.0 * np.log2(f0[v] / 440.0)
    return out


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_name(m):
    if m is None or not np.isfinite(m):
        return None
    i = int(round(float(m)))
    return "%s%d" % (NOTE_NAMES[i % 12], i // 12 - 1)


@dataclass
class F0Result:
    f0: np.ndarray            # (n_frames,) Hz、無声は 0
    confidence: np.ndarray    # (n_frames,) 0..1
    voiced: np.ndarray        # (n_frames,) bool（採用する V/UV）
    rms_db: np.ndarray        # (n_frames,) dBFS
    hop_s: float = HOP_S
    sr: int = 48000
    estimator: str = "rmvpe"
    elapsed_sec: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def n_frames(self):
        return len(self.f0)

    @property
    def times(self):
        return np.arange(len(self.f0)) * self.hop_s

    def to_json(self):
        """MCP には生の配列を返さないので、保存用の dict だけ作る。"""
        return {
            "estimator": self.estimator,
            "hop_s": self.hop_s,
            "sr": self.sr,
            "n_frames": int(self.n_frames),
            "elapsed_sec": round(self.elapsed_sec, 4),
            "f0": [round(float(v), 4) for v in self.f0],
            "confidence": [round(float(v), 5) for v in self.confidence],
            "voiced": [int(v) for v in self.voiced],
            "rms_db": [round(float(v), 2) for v in self.rms_db],
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, d):
        return cls(
            f0=np.asarray(d["f0"], dtype="float64"),
            confidence=np.asarray(d["confidence"], dtype="float64"),
            voiced=np.asarray(d["voiced"], dtype=bool),
            rms_db=np.asarray(d["rms_db"], dtype="float64"),
            hop_s=float(d.get("hop_s", HOP_S)),
            sr=int(d.get("sr", 48000)),
            estimator=d.get("estimator", "rmvpe"),
            elapsed_sec=float(d.get("elapsed_sec", 0.0)),
            meta=d.get("meta", {}),
        )


def _rmvpe(model_path=None):
    key = ("rmvpe", model_path or RMVPE_PATH)
    with _MODEL_LOCK:
        return _rmvpe_locked(key, model_path)


def _rmvpe_locked(key, model_path):
    if key not in _MODEL_CACHE:
        from .rmvpe_onnx import RMVPE
        p = model_path or RMVPE_PATH
        if not os.path.exists(p):
            raise ModelMissingError(
                "RMVPE の重みが見つからない: %s\n"
                "解析モデルが未取得。Gliss の「モデルの準備」画面（初回画面）からダウンロードするか、"
                "公開元から rmvpe.onnx を取得してその場所に置く（置き場は環境変数 "
                "VOCAL_ENGINE_MODELS_DIR で変えられる。重みは同梱していない）。" % p)
        _MODEL_CACHE[key] = RMVPE(model_path=p, threshold=RMVPE_THRESHOLD)
    return _MODEL_CACHE[key]


def _to_grid(times_src, values, n_frames, hop_s=HOP_S, is_f0=True):
    """任意のフレーム時刻の系列を 10 ms グリッドへ。f0 は無声(0)を跨いで補間しない。"""
    t_dst = np.arange(n_frames) * hop_s
    times_src = np.asarray(times_src, dtype="float64")
    values = np.asarray(values, dtype="float64")
    if len(times_src) == 0:
        return np.zeros(n_frames)
    if not is_f0:
        return np.interp(t_dst, times_src, values)
    v = values > 0
    out = np.zeros(n_frames)
    if v.any():
        out = np.exp(np.interp(t_dst, times_src[v], np.log(values[v])))
    near = np.clip(np.searchsorted(times_src, t_dst), 0, len(values) - 1)
    out[~v[near]] = 0.0
    return out


def estimate_f0(path=None, x=None, sr=None, estimator="rmvpe", sweep=False,
                model_path=None, threshold=RMVPE_THRESHOLD,
                energy_floor_db=ENERGY_FLOOR_DB):
    """10 ms ホップの F0 と V/UV。

    estimator: "rmvpe"（正） / "fcpe"（代替） / "auto"（rmvpe → 落ちたら fcpe）
    sweep:     RMVPE の confidence を threshold 掃引で作る（13 倍遅い）
    """
    if x is None:
        if path is None:
            raise ValueError("path か (x, sr) のどちらかが要る")
        x, sr = read_mono(path)
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    n_frames = int(np.floor(len(x) / sr / HOP_S)) + 1

    order = {"rmvpe": ["rmvpe"], "fcpe": ["fcpe"], "auto": ["rmvpe", "fcpe"]}[estimator]
    last_err = None
    for est in order:
        try:
            t0 = time.perf_counter()
            if est == "rmvpe":
                f0, conf = _estimate_rmvpe(x, sr, n_frames, sweep, model_path, threshold)
            else:
                f0, conf = _estimate_fcpe(x, sr, n_frames)
            elapsed = time.perf_counter() - t0
            rms = frame_rms_db(x, sr, n_frames, HOP_S)
            voiced = (f0 > 0) & (rms > energy_floor_db)
            f0 = np.where(voiced, f0, 0.0)
            return F0Result(f0=f0, confidence=conf, voiced=voiced, rms_db=rms,
                            hop_s=HOP_S, sr=int(sr), estimator=est, elapsed_sec=elapsed,
                            meta={"vuv_rule": "%s f0>0 AND rms > %.1f dBFS" % (est, energy_floor_db),
                                  "threshold": threshold, "sweep": bool(sweep)})
        except Exception as e:      # noqa: BLE001 — 代替にフォールバックするため
            last_err = e
            if est == order[-1]:
                raise
    raise last_err


def _estimate_rmvpe(x, sr, n_frames, sweep, model_path, threshold):
    m = _rmvpe(model_path)
    f0, conf = m.infer(x, sr, threshold=threshold, sweep=sweep)
    t = np.arange(len(f0)) * HOP_S
    f0g = _to_grid(t, f0, n_frames)
    if sweep:
        confg = _to_grid(t, conf, n_frames, is_f0=False)
        confg = np.clip(confg / 0.9, 0.0, 1.0)        # 掃引値（0〜0.9）を 0..1 に正規化
    else:
        confg = (f0g > 0).astype("float64")           # 生の salience が取れない ONNX のため
    return f0g, confg


def _estimate_fcpe(x, sr, n_frames):
    import torch
    import torchfcpe
    key = ("fcpe",)
    if key not in _MODEL_CACHE:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _MODEL_CACHE[key] = (torchfcpe.spawn_bundled_infer_model(device=dev), dev)
    model, dev = _MODEL_CACHE[key]
    wav = torch.from_numpy(np.asarray(x, dtype="float32"))[None, :, None].to(dev)
    # torchfcpe 0.0.4: retur_uv=True のときは output_interp_target_length が必須
    f0, uv = model.infer(wav, sr=sr, decoder_mode="local_argmax", threshold=0.006,
                         f0_min=50.0, f0_max=1100.0, interp_uv=False,
                         output_interp_target_length=n_frames, retur_uv=True)
    f0 = f0.squeeze().detach().cpu().numpy().astype("float64")
    uv = uv.squeeze().detach().cpu().numpy().astype("float64")
    voiced = uv < 0.5
    f0 = np.where(voiced & (f0 > 0), f0, 0.0)
    return f0, voiced.astype("float64")


def summarize(f0, voiced, confidence=None, lo=None, hi=None):
    """MCP 用の要約統計（生の配列は返さない）。lo/hi はフレーム番号。"""
    sl = slice(lo, hi)
    f = np.asarray(f0)[sl]
    v = np.asarray(voiced)[sl].astype(bool) & (f > 0)
    out = {
        "n_frames": int(len(f)),
        "n_voiced": int(v.sum()),
        "voiced_ratio": round(float(v.mean()), 4) if len(f) else 0.0,
    }
    if not v.any():
        out.update({"median_hz": None, "median_note": None, "mean_hz": None,
                    "min_hz": None, "max_hz": None, "iqr_semitones": None,
                    "range_semitones": None, "confidence_mean": None})
        return out
    fv = f[v]
    midi = hz_to_midi(fv)
    q1, q3 = np.percentile(midi, [25, 75])
    med = float(np.median(fv))
    out.update({
        "median_hz": round(med, 2),
        "median_note": midi_to_name(float(np.median(midi))),
        "median_midi": round(float(np.median(midi)), 3),
        "mean_hz": round(float(np.mean(fv)), 2),
        "min_hz": round(float(np.min(fv)), 2),
        "max_hz": round(float(np.max(fv)), 2),
        "iqr_semitones": round(float(q3 - q1), 3),
        "range_semitones": round(float(np.max(midi) - np.min(midi)), 3),
    })
    if confidence is not None:
        c = np.asarray(confidence)[sl][v]
        out["confidence_mean"] = round(float(np.mean(c)), 4) if c.size else None
    return out
