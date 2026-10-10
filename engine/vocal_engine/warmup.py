# -*- coding: utf-8 -*-
"""起動直後の下ごしらえ（2026-10-10 承認）。

エンジンが起動したら、裏のスレッドで、初めての解析が待たされる部分を先に済ませておく:
librosa・scipy の読み込み、numba の JIT（DTW・オンセット・MFCC）、使う F0 の方式のモデル（RMVPE など）、
HubertFA のセッションと辞書。エンジンのロック（`mcp_server._lock`）は取らない。モデルの読み込みは
それぞれのロック（f0 の `_MODEL_LOCK`・hubertfa の `_load_lock`）が守るので、表の解析と重なっても 2 回読まない。

済んだら engine.log に 1 行書く（例 `engine start=2.4 warm=3.1 librosa=1.6 numba=0.1 onnx=0.5 hubertfa=0.7`）。
start は vocal_engine を読み始めてからサーバーが受け付けるまで、warm は下ごしらえ全体の秒。
`GLISS_WARMUP=0` で止める（テスト用）。失敗しても解析は普段どおり動く（その場で読む）ので、ログに残すだけ。
"""
import os
import threading
import time

from . import STARTED, log

_thread = None
_done = threading.Event()
_ready_at = None
times = {}          # 部分ごとの秒（engine_info・テストが見る）


def enabled():
    return (os.environ.get("GLISS_WARMUP") or "1").strip() != "0"


def mark_ready():
    """サーバーが受け付けを始めた（`engine start=` の終わり）。"""
    global _ready_at
    _ready_at = time.perf_counter()


def start():
    """下ごしらえのスレッドを起こす（2 回目からは何もしない）。止めてあれば None。"""
    global _thread
    if not enabled():
        _done.set()
        return None
    if _thread is None:
        _thread = threading.Thread(target=_run, name="gliss-warmup", daemon=True)
        _thread.start()
    return _thread


def wait(timeout=None):
    """下ごしらえが済むまで待つ（テスト・計測用）。済んだら True。"""
    return _done.wait(timeout)


def done():
    return _done.is_set()


def _lap(name, fn):
    t = time.perf_counter()
    try:
        fn()
    except Exception as e:                  # noqa: BLE001  下ごしらえの失敗は解析の時に同じ所で出る
        log.get().info("下ごしらえの %s を飛ばした: %s", name, e)
    times[name] = time.perf_counter() - t


def _librosa():
    import librosa
    # librosa は部分ごとに遅れて読む（lazy_loader）。解析が使う部分を読んでおく
    librosa.feature, librosa.onset, librosa.sequence, librosa.filters, librosa.effects  # noqa: B018
    import scipy.ndimage  # noqa: F401
    import scipy.signal  # noqa: F401
    import scipy.spatial.distance  # noqa: F401


def _numba():
    import numpy as np

    from .analysis import align, onsets
    sr = 44100
    x = (np.random.default_rng(0).standard_normal(sr) * 0.1).astype("float32")
    m = align._mfcc(x, sr)
    align._dtw_path(m, m)
    align._envelopes(x, sr)
    align.estimate_timeline(x, sr, x, sr)
    onsets.detect(x, sr)


def _onnx():
    import numpy as np

    from .analysis import f0
    est = f0.resolve_estimator()
    f0.load_model(est)
    if est in ("rmvpe", "gliss"):
        # 1 回目の推論はセッションの内部の確保で遅い。短い音で 1 回流しておく
        sr = 16000
        x = (np.random.default_rng(1).standard_normal(sr // 2) * 0.01).astype("float32")
        f0.estimate_f0(x=x, sr=sr, estimator=est)


def _hubertfa():
    from .phoneme import hubertfa
    if not hubertfa.model_found():
        return
    al = hubertfa.get_aligner()
    al.session()
    al.dictionary("ja")


def _run():
    t0 = time.perf_counter()
    try:
        _lap("librosa", _librosa)
        _lap("numba", _numba)
        _lap("onnx", _onnx)
        _lap("hubertfa", _hubertfa)
    finally:
        times["warm"] = time.perf_counter() - t0
        _done.set()
        start_sec = (_ready_at or t0) - STARTED
        log.get().info("engine start=%.1f warm=%.1f librosa=%.1f numba=%.1f onnx=%.1f hubertfa=%.1f",
                       start_sec, times["warm"], times.get("librosa", 0), times.get("numba", 0),
                       times.get("onnx", 0), times.get("hubertfa", 0))
