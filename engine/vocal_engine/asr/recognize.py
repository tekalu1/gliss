# -*- coding: utf-8 -*-
"""区間の音声認識（issue #54）。結果は**候補**で、確定の歌詞は書き換えない。

認識器は差し替えられる（`set_recognizer_factory`。テストは偽の認識器を使う）。
既定は faster-whisper（CTranslate2。torch 不要）。GPU（CUDA）が使えなければ CPU に落とし、
遅いことを結果の `warnings` と `device` で知らせる。
"""
from __future__ import annotations

import glob
import importlib.util
import os
import re
import site
import sys
import threading
import time

import numpy as np

from .. import log
from ..audio import resample
from .catalog import AsrModel

SR = 16000
INSTALL_HINT = ("エンジンの Python に faster-whisper を入れる（例: uv pip install --python "
                "<.venv の python> faster-whisper。engine の extras `asr`）")


class AsrUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- 使えるか
def backend_available(model: AsrModel):
    """(使えるか, 理由)。faster-whisper は任意の依存なので、無ければ理由を返す。"""
    if model.backend == "fake":
        fake = model.extra.get("fake") or {}
        if fake.get("unavailable"):
            return False, fake["unavailable"]
        return True, None
    if model.backend == "faster-whisper":
        if importlib.util.find_spec("faster_whisper") is None:
            return False, "faster-whisper が入っていない。" + INSTALL_HINT
        return True, None
    return False, "未対応の認識器: %s" % model.backend


# ---------------------------------------------------------------- CUDA の DLL（Windows）
_dll_dirs_done = False


def _add_cuda_dll_dirs():
    """pip の nvidia-cublas-cu12 などが入っていれば、その DLL の場所を足す。

    CTranslate2 は cuBLAS（cublas64_12.dll）を推論の最初に LoadLibrary で読むので、
    `os.add_dll_directory` だけでなく PATH にも入れる（入れないと推論で失敗する）。
    """
    global _dll_dirs_done
    if _dll_dirs_done or sys.platform != "win32":
        return
    _dll_dirs_done = True
    roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    for root in roots:
        for d in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
            except OSError:
                continue
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _cuda_problem():
    """GPU を使えない理由（使えるなら None）。"""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return "CUDA の GPU が見つからない"
    except Exception as e:                  # noqa: BLE001
        return "CTranslate2 が GPU を調べられない: %s" % e
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.WinDLL("cublas64_12.dll")
        except OSError:
            return ("cuBLAS（CUDA 12、cublas64_12.dll）が無い。GPU で使うには "
                    "nvidia-cublas-cu12 を入れる（engine の extras `asr-cuda`）")
    return None


_probe = {}


def expected_device(model: AsrModel):
    """(使う見込みの device, CPU になる理由)。読み込み済みならその結果。"""
    ld = loaded(model.id)
    if ld is not None:
        return ld["device"], ld["device_note"]
    if model.backend == "fake":
        cfg = model.extra.get("fake") or {}
        return cfg.get("device", "cuda"), cfg.get("device_note")
    if os.environ.get("GLISS_ASR_DEVICE") == "cpu":
        return "cpu", "GLISS_ASR_DEVICE=cpu"
    if "cuda" not in _probe:
        _add_cuda_dll_dirs()
        _probe["cuda"] = _cuda_problem()
    problem = _probe["cuda"]
    return ("cpu", problem) if problem else ("cuda", None)


# ---------------------------------------------------------------- 認識器
class WhisperRecognizer:
    """faster-whisper（CTranslate2）。GPU は FP16、CPU は INT8。"""

    def __init__(self, model: AsrModel, model_dir: str, device="auto"):
        self.model = model
        self.model_dir = model_dir
        self.want = device
        self.device = None
        self.device_note = None
        self._m = None

    def load(self):
        if self._m is not None:
            return
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel
        problem = None if self.want == "cpu" else _cuda_problem()
        t0 = time.perf_counter()
        if self.want != "cpu" and problem is None:
            try:
                self._m = WhisperModel(self.model_dir, device="cuda", compute_type="float16")
                self.device = "cuda"
            except Exception as e:          # noqa: BLE001
                problem = "GPU で読み込めない: %s" % e
        if self._m is None:
            self._m = WhisperModel(self.model_dir, device="cpu", compute_type="int8",
                                   cpu_threads=min(8, os.cpu_count() or 4))
            self.device = "cpu"
            self.device_note = problem
        log.get("asr").info("%s を %s で読み込んだ（%.1f 秒）%s", self.model.id, self.device,
                            time.perf_counter() - t0, (" / " + problem) if problem else "")

    def _run(self, y, progress):
        segments, _info = self._m.transcribe(
            y, language="ja", beam_size=5, condition_on_previous_text=False,
            vad_filter=False, word_timestamps=True)
        dur = max(len(y) / SR, 1e-6)
        segs = []
        for s in segments:                   # 1 つずつ出てくる（ここで進み具合と取り消し）
            segs.append({"start": float(s.start), "end": float(s.end), "text": s.text,
                         "avg_logprob": float(s.avg_logprob),
                         "no_speech_prob": float(s.no_speech_prob),
                         "words": [{"start": float(w.start), "end": float(w.end),
                                    "text": w.word, "probability": float(w.probability)}
                                   for w in (s.words or [])]})
            if progress is not None:
                progress(min(1.0, float(s.end) / dur))
        return segs

    def transcribe(self, y, progress=None):
        self.load()
        try:
            return self._run(y, progress)
        except RuntimeError as e:
            # 読み込めても推論で CUDA のライブラリが足りないことがある → CPU でやり直す
            if self.device != "cuda" or not re.search(r"cuda|cublas|cudnn", str(e), re.I):
                raise
            log.get("asr").warning("GPU の推論に失敗したので CPU でやり直す: %s", e)
            self._m = None
            self.want = "cpu"
            self.load()
            self.device_note = "GPU の推論に失敗: %s" % e
            return self._run(y, progress)


class FakeRecognizer:
    """テスト用: 決めた文を、決めた時間をかけて返す（`GLISS_ASR_FAKE` の JSON）。"""

    def __init__(self, model: AsrModel, model_dir: str, device="auto"):
        self.model = model
        self.cfg = model.extra.get("fake") or {}
        self.device = self.cfg.get("device", "cuda")
        self.device_note = self.cfg.get("device_note")

    def load(self):
        pass

    def transcribe(self, y, progress=None):
        text = self.cfg.get("text", "あいうえお")
        dur = len(y) / SR
        steps = 10
        for i in range(steps):
            time.sleep(float(self.cfg.get("delay_sec", 0.2)) / steps)
            if progress is not None:
                progress((i + 1) / steps)
        chars = [c for c in text if not c.isspace()]
        n = max(1, len(chars))
        words = [{"start": dur * i / n, "end": dur * (i + 1) / n, "text": c,
                  "probability": float(self.cfg.get("probability", 0.9))}
                 for i, c in enumerate(chars)]
        return [{"start": 0.0, "end": dur, "text": text, "avg_logprob": -0.2,
                 "no_speech_prob": 0.01, "words": words}]


def _default_factory(model, model_dir):
    if model.backend == "fake":
        return FakeRecognizer(model, model_dir)
    return WhisperRecognizer(model, model_dir, device=os.environ.get("GLISS_ASR_DEVICE", "auto"))


_factory = _default_factory
_loaded = {}                 # model.id -> 認識器（読み込みは重いので使い回す）
_run_lock = threading.Lock()  # 1 つの認識器を同時に 2 本走らせない


def set_recognizer_factory(fn=None):
    """認識器を差し替える（テスト）。None で既定に戻す。読み込み済みのものは捨てる。"""
    global _factory
    _factory = fn or _default_factory
    _loaded.clear()


def loaded(model_id):
    r = _loaded.get(model_id)
    return None if r is None else {"device": r.device, "device_note": r.device_note}


def recognizer(model: AsrModel, model_dir: str):
    r = _loaded.get(model.id)
    if r is None:
        _loaded.clear()                      # 別のモデルに替えたら前のは手放す（メモリ）
        r = _factory(model, model_dir)
        _loaded[model.id] = r
    return r


def to_16k(x, sr):
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    y = resample(x, sr, SR) if sr != SR else x
    return np.ascontiguousarray(y, dtype=np.float32)


def run(model: AsrModel, model_dir: str, x, sr, progress=None):
    """音声（区間を切り出したもの）→ 認識の生の結果 (segments, device, device_note, 秒)。"""
    y = to_16k(x, sr)
    with _run_lock:
        r = recognizer(model, model_dir)
        t0 = time.perf_counter()
        segs = r.transcribe(y, progress=progress)
        return segs, r.device, r.device_note, time.perf_counter() - t0
