# -*- coding: utf-8 -*-
"""RMVPE (ONNX) の推論ラッパ。

- 入力は 16 kHz モノラル。hop 160 サンプル（= 10 ms）。
- メル前処理は RMVPE の仕様どおり（n_fft=1024 / win=1024 / hop=160 / n_mels=128 /
  fmin=30 / fmax=8000 / log(clamp(x, 1e-5))、STFT は center=True・reflect）。
- 出力は 360 ビンの salience。20 セント刻み、基準 1997.3794084376191 セント
  （f0 = 10 * 2**(cents/1200)）。前後 ±4 ビンの重心で局所平均する。
- 重みは再配布しない。重みの置き場（config.models_dir()）の rmvpe.onnx を各自で用意する
  （画面の初回のダウンロード。入手元とハッシュは app/model-download.mjs）。

使い方:
    from rmvpe_onnx import RMVPE
    rmvpe = RMVPE()                      # 重みの置き場の rmvpe.onnx を自動で探す
    f0, conf = rmvpe.infer_file("take.wav")
"""
import os

import numpy as np

from ..config import models_dir as _models_dir  # noqa: E402

DEFAULT_MODEL = os.path.join(_models_dir(), "rmvpe.onnx")

SR = 16000
N_FFT = 1024
WIN_LENGTH = 1024
HOP_LENGTH = 160          # 10 ms
N_MELS = 128
F_MIN = 30.0
F_MAX = 8000.0
CLAMP = 1e-5
N_BINS = 360
CENTS_BASE = 1997.3794084376191   # = 1200*log2(32.7/10)
CENTS_STEP = 20.0


class RMVPE:
    def __init__(self, model_path=None, providers=None, threshold=0.03):
        import onnxruntime as ort

        self.model_path = os.path.abspath(model_path or DEFAULT_MODEL)
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"RMVPE の重みが無い: {self.model_path}\n"
                "画面の初回の「モデルの準備」で取得するか、VOCAL_ENGINE_MODELS_DIR で置き場を指すこと（再配布しない）。"
            )
        if providers is None:
            avail = ort.get_available_providers()
            providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in avail]
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(self.model_path, sess_options=so, providers=providers)
        self.providers = self.sess.get_providers()
        self.inputs = self.sess.get_inputs()
        self.outputs = self.sess.get_outputs()
        self.threshold = threshold
        self._mel_basis = None
        # 入力が波形かメルかを自動判定する
        shp = list(self.inputs[0].shape)
        self.input_is_mel = any(
            (isinstance(d, int) and d == N_MELS) for d in shp
        ) or len(shp) >= 3
        self.cents_mapping = np.pad(CENTS_STEP * np.arange(N_BINS) + CENTS_BASE, (4, 4))

    # ------------------------------------------------------------ mel
    def mel_basis(self):
        if self._mel_basis is None:
            import librosa
            self._mel_basis = librosa.filters.mel(
                sr=SR, n_fft=N_FFT, n_mels=N_MELS, fmin=F_MIN, fmax=F_MAX
            )
        return self._mel_basis

    def log_mel(self, audio16k):
        """(128, T) の log-mel。RMVPE / RVC の MelSpectrogram と同じ定義。"""
        import librosa
        stft = librosa.stft(
            np.asarray(audio16k, dtype="float32"),
            n_fft=N_FFT,
            hop_length=HOP_LENGTH,
            win_length=WIN_LENGTH,
            window="hann",
            center=True,
            pad_mode="reflect",
        )
        mag = np.abs(stft)
        mel = self.mel_basis() @ mag
        return np.log(np.clip(mel, CLAMP, None)).astype("float32")

    # ------------------------------------------------------------ 推論
    def _run(self, mel):
        """mel: (128, T) → salience: (T, 360)"""
        n_frames = mel.shape[-1]
        pad_to = 32 * ((n_frames - 1) // 32 + 1)
        mel_p = np.pad(mel, ((0, 0), (0, pad_to - n_frames)), mode="constant")
        x = mel_p[None, ...]                     # (1, 128, T)
        name = self.inputs[0].name
        feeds = {name: x}
        try:
            hidden = self.sess.run(None, feeds)[0]
        except Exception:
            # (1, T, 128) を期待するエクスポートへのフォールバック
            feeds = {name: np.ascontiguousarray(x.transpose(0, 2, 1))}
            hidden = self.sess.run(None, feeds)[0]
        hidden = np.asarray(hidden)
        if hidden.ndim == 3:
            hidden = hidden[0]
        if hidden.shape[0] == N_BINS and hidden.shape[1] != N_BINS:
            hidden = hidden.T
        return hidden[:n_frames]

    # yxlllc/RMVPE の ONNX（release 230917）は入力 (waveform, threshold)、
    # 出力 (f0, uv[bool]) で、salience の生値は取り出せない。
    # 「確率」の代わりに threshold を掃いて、その frame が有声のまま残る最大の
    # threshold を記録する（salience 最大値の単調な代用値になる）。
    CONF_GRID = (0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12,
                 0.18, 0.25, 0.35, 0.5, 0.7, 0.9)

    def _run_wave(self, audio16k, threshold):
        name = self.inputs[0].name
        shp = list(self.inputs[0].shape)
        x = np.asarray(audio16k, dtype="float32")
        if len(shp) == 2:
            x = x[None, :]
        feeds = {name: x}
        for extra in self.inputs[1:]:
            feeds[extra.name] = np.array(threshold, dtype="float32").reshape(
                list(extra.shape) if all(isinstance(d, int) for d in extra.shape) else []
            )
        return [np.asarray(o) for o in self.sess.run(None, feeds)]

    def _sweep_confidence(self, audio16k, threshold):
        """(f0, conf) を返す。conf は有声のまま残る最大 threshold（0 = 常に無声）。"""
        f0 = None
        conf = None
        for thr in self.CONF_GRID:
            outs = self._run_wave(audio16k, thr)
            f = np.asarray(outs[0]).reshape(-1).astype("float64")
            v = f > 0
            if conf is None:
                conf = np.zeros(len(f))
            conf = np.where(v, np.maximum(conf, thr), conf)
            if abs(thr - threshold) < 1e-9 or (f0 is None and thr >= threshold):
                f0 = f
        if f0 is None:
            f0 = np.asarray(self._run_wave(audio16k, threshold)[0]).reshape(-1).astype("float64")
        return f0, conf

    def decode(self, salience, threshold=None):
        """salience (T, 360) → (f0 Hz, confidence)"""
        thr = self.threshold if threshold is None else threshold
        sal = np.asarray(salience, dtype="float64")
        conf = sal.max(axis=1)
        center = np.argmax(sal, axis=1) + 4
        pad = np.pad(sal, ((0, 0), (4, 4)))
        idx = center[:, None] + np.arange(-4, 5)[None, :]
        w = np.take_along_axis(pad, idx, axis=1)
        c = self.cents_mapping[idx]
        cents = (w * c).sum(axis=1) / np.maximum(w.sum(axis=1), 1e-12)
        f0 = 10.0 * (2.0 ** (cents / 1200.0))
        f0[conf <= thr] = 0.0
        return f0, conf

    # ------------------------------------------------------------ 公開 API
    def infer(self, audio, sr, threshold=None, sweep=True):
        """任意 SR のモノラル波形 → (f0[Hz], confidence)。10 ms ホップ。

        sweep=True のとき、salience を直接返さない ONNX では threshold 掃引で
        段階的な confidence を作る（CONF_GRID の 13 段）。
        """
        import soxr

        thr = self.threshold if threshold is None else threshold
        x = np.asarray(audio, dtype="float64")
        if x.ndim > 1:
            x = x.mean(axis=1)
        if sr != SR:
            x = soxr.resample(x, sr, SR, quality="VHQ")
        x = x.astype("float32")
        if self.input_is_mel:
            mel = self.log_mel(x)
            sal = self._run(mel)
            return self.decode(sal, thr)
        if sweep:
            return self._sweep_confidence(x, thr)
        outs = self._run_wave(x, thr)
        if len(outs) >= 2 and outs[0].size == outs[1].size:
            f0 = outs[0].reshape(-1).astype("float64")
            uv = outs[1].reshape(-1)
            conf = (~uv.astype(bool)).astype("float64") if uv.dtype == bool else uv.astype("float64")
            return f0, conf
        sal = outs[0]
        if sal.ndim == 3:
            sal = sal[0]
        if sal.shape[0] == N_BINS and sal.shape[1] != N_BINS:
            sal = sal.T
        return self.decode(sal, thr)

    def infer_file(self, path, threshold=None, sweep=True):
        import soundfile as sf
        x, sr = sf.read(path, always_2d=True, dtype="float64")
        return self.infer(x.mean(axis=1), sr, threshold, sweep=sweep)


if __name__ == "__main__":
    import sys
    r = RMVPE()
    print("providers:", r.providers)
    print("inputs:", [(i.name, i.shape, i.type) for i in r.inputs])
    print("outputs:", [(o.name, o.shape, o.type) for o in r.outputs])
    if len(sys.argv) > 1:
        f0, conf = r.infer_file(sys.argv[1])
        v = f0 > 0
        print("frames", len(f0), "voiced", int(v.sum()),
              "median f0", float(np.median(f0[v])) if v.any() else None)
