# -*- coding: utf-8 -*-
"""F0 推定。10 ms ホップ。方式は 4 つ:

- "rmvpe": RMVPE（ONNX）。既定。重みは同梱せず、利用者が取得する（初回の画面で任意）。重みが無ければ Gliss のモデルで解析する
- "gliss": Gliss の F0 モデル。条件のはっきりした学習データだけで学習した小さなモデル
  （SwiftF0 と同じ構造。`models/gliss-f0.onnx`、同梱）。RMVPE の重みが無いときに代わりに使う。
  叫び・高い声への跳躍で 1 オクターブ上に誤ることがある（学習データに少ない種類の声）
- "praat": Praat（parselmouth）の自己相関法。引数と有声の判定を歌声向けに調整したもの。重みは要らない
- "fcpe": FCPE（torch。開発版だけ）

どの方式で解析するか（`resolve_estimator`）: トラックで明示した方式（`analyze_take(estimator=…)`。呼び出し側が name で渡す）→
利用者が選んだ方式（画面の「ピッチ検出の方式」・環境変数）→
曲を前に解析した方式（`recorded`。RMVPE の重みを後から取った・消したときや、既定を替えたときに、
解析・編集済みの曲の音符の区切りを変えない）→ 既定（RMVPE）。RMVPE になったのに重みが無ければ Gliss のモデル。

V/UV は段階0（方式の評価）の判定を踏襲する:
    有声 = 方式が F0 を出している かつ フレーム RMS > -55 dBFS
方式が F0 を出すかどうかは、RMVPE は salience >= 閾値、Gliss のモデルは確信度 >= 0.5（較正済み）、
Praat は strength のヒステリシス（`_praat_hysteresis`）。

RMVPE の ONNX（yxlllc/RMVPE release 230917）は salience の生値を返さず
(f0, uv) しか出さないので、confidence は「有声のまま残る最大の threshold」を
掃引して作る（段階0と同じ手口）。掃引は 13 回推論するぶん遅いので、
既定は 1 パス（confidence は 0/1 の代用値）。Gliss のモデルと Praat は本物の確度（0..1）を返す。
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

# Gliss の F0 モデル。エンジンと一緒に配る（135 KB。配布版は vocal-engine.spec が exe に入れる）
GLISS_F0_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "gliss-f0.onnx")

# 画面・MCP で選べる方式（並びは画面の並び）と、版（方式の中身を変えたら上げる。解析のキャッシュを分ける）。
# Gliss の F0 モデルの版は、同梱のモデルファイルの SHA-256（`estimator_version`。モデルを替えたら自動で変わる）
ESTIMATORS = ("rmvpe", "gliss", "praat")
DEFAULT_ESTIMATOR = "rmvpe"
ESTIMATOR_VERSIONS = {"praat": "1"}
_CHAINS = {"rmvpe": ["rmvpe"], "gliss": ["gliss"], "praat": ["praat"], "fcpe": ["fcpe"],
           "auto": ["rmvpe", "gliss"]}
ESTIMATOR_ENV = "GLISS_F0_ESTIMATOR"     # 利用者が選んだ方式（画面が起動時に渡す。画面は set_f0_estimator で選ぶ）

_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()     # 裏の準備（issue #63）と表が同時に初めて読むとき、2 回読まない
_preferred = None                  # set_preferred_estimator で選んだ方式（None なら環境変数）


class ModelMissingError(RuntimeError):
    pass


def check_estimator(name):
    """方式の名前を確かめて返す（知らない名前は ValueError）。"""
    if name not in _CHAINS:
        raise ValueError("estimator は %s のどれか（%r は知らない）" % (" / ".join(_CHAINS), name))
    return name


def chosen_estimator():
    """利用者が選んだ方式（`set_preferred_estimator` → 環境変数 GLISS_F0_ESTIMATOR）。選んでいなければ None。"""
    if _preferred:
        return _preferred
    env = (os.environ.get(ESTIMATOR_ENV) or "").strip().lower()
    return env if env in ESTIMATORS else None


def preferred_estimator():
    """これから解析する曲の方式（選んだ方式。選んでいなければ既定の "rmvpe"）。重みの有無は見ない（`resolve_estimator`）。"""
    return chosen_estimator() or DEFAULT_ESTIMATOR


def set_preferred_estimator(name):
    """画面で選んだ方式をエンジン全体の方式にする（解析・裏の準備が使う。前に解析した曲もこの方式で
    解析し直す）。None で戻す（曲ごとに前の方式・新しい曲は既定）。"""
    global _preferred
    if name is not None and name not in ESTIMATORS:
        raise ValueError("estimator は %s のどれか（%r は知らない）" % (" / ".join(ESTIMATORS), name))
    _preferred = name
    return resolve_estimator()


def rmvpe_available():
    return os.path.exists(RMVPE_PATH)


def resolve_estimator(name=None, recorded=None):
    """実際に使う方式。name を省くと、選んだ方式 → recorded（その曲を前に解析した方式）→ 既定 の順で決め、
    それが RMVPE なのに重みが無ければ Gliss のモデル。
    名前を指定したときは、そのまま使う（RMVPE の重みが無ければ ModelMissingError になる）。"""
    if name is None:
        name = chosen_estimator() or (recorded if recorded in ESTIMATORS else None) or DEFAULT_ESTIMATOR
        if name == "rmvpe" and not rmvpe_available():
            return "gliss"
        return name
    return check_estimator(name)


_GLISS_VERSION = {}


def _gliss_model_version():
    """同梱の Gliss の F0 モデルの版 = ファイルの SHA-256 の先頭 12 桁（ファイルが変わったときだけ取り直す）。"""
    try:
        st = os.stat(GLISS_F0_PATH)
        key = (GLISS_F0_PATH, st.st_size, st.st_mtime_ns)
    except OSError:
        return "missing"
    if _GLISS_VERSION.get("key") != key:
        import hashlib
        with open(GLISS_F0_PATH, "rb") as f:
            _GLISS_VERSION.update(key=key, version="m-" + hashlib.sha256(f.read()).hexdigest()[:12])
    return _GLISS_VERSION["version"]


def estimator_version(name):
    """方式の版（RMVPE・FCPE は None）。キャッシュの鍵と、保存した解析が今の方式のものかの判定に使う。
    Gliss はモデルファイルの SHA-256 から（モデルを替えたのに前の解析を使い回さない）。"""
    if name == "gliss":
        return _gliss_model_version()
    return ESTIMATOR_VERSIONS.get(name)


def same_estimator(result_estimator, result_version, wanted):
    """保存した解析（方式・版）が、wanted（`resolve_estimator` の結果）で作ったものと同じか。"""
    ok = _CHAINS.get(wanted, [wanted]) if wanted == "auto" else [wanted]
    return result_estimator in ok and result_version == estimator_version(result_estimator)


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

    estimator: "rmvpe"（既定） / "gliss"（Gliss の F0 モデル） / "praat" / "fcpe"（代替） /
               "auto"（rmvpe → 落ちたら gliss）。None なら `resolve_estimator()`（選んでいる方式・既定）
               （画面・MCP の解析は Project.analyze が曲ごとの方式を決めて渡す）
    sweep:     RMVPE の confidence を threshold 掃引で作る（13 倍遅い。ほかの方式では使わない）
    """
    if x is None:
        if path is None:
            raise ValueError("path か (x, sr) のどちらかが要る")
        x, sr = read_mono(path)
    x = np.asarray(x, dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    n_frames = int(np.floor(len(x) / sr / HOP_S)) + 1

    order = _CHAINS[resolve_estimator(estimator)]
    last_err = None
    for est in order:
        try:
            t0 = time.perf_counter()
            if est == "rmvpe":
                f0, conf = _estimate_rmvpe(x, sr, n_frames, sweep, model_path, threshold)
            elif est == "gliss":
                f0, conf = _estimate_gliss(x, sr, n_frames)
            elif est == "praat":
                f0, conf = _estimate_praat(x, sr, n_frames)
            else:
                f0, conf = _estimate_fcpe(x, sr, n_frames)
            elapsed = time.perf_counter() - t0
            rms = frame_rms_db(x, sr, n_frames, HOP_S)
            voiced = (f0 > 0) & (rms > energy_floor_db)
            f0 = np.where(voiced, f0, 0.0)
            conf = np.where(voiced, conf, 0.0) if est in ("gliss", "praat") else conf
            meta = {"vuv_rule": "%s f0>0 AND rms > %.1f dBFS" % (est, energy_floor_db),
                    "threshold": threshold, "sweep": bool(sweep)}
            if estimator_version(est) is not None:
                meta["version"] = estimator_version(est)
                meta["voicing"] = (GLISS_VOICING if est == "gliss" else PRAAT_VOICING)
            return F0Result(f0=f0, confidence=conf, voiced=voiced, rms_db=rms,
                            hop_s=HOP_S, sr=int(sr), estimator=est, elapsed_sec=elapsed,
                            meta=meta)
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


# ---------------------------------------------------------------- Gliss の F0 モデル
# 入出力と前処理・後処理は SwiftF0（MIT。https://github.com/lars76/swift-f0 ）の推論コードに従う:
# 16 kHz モノを入れると、256 サンプル（16 ms）ごとに F0（Hz）と較正済みの確信度（0..1）を返す。
# STFT・ログ周波数への変換・復号はすべてグラフの中。長い音は 1875 フレームずつ、前に 11・後ろに 10 フレームの
# のりしろを付けて流す（受容野の分。区切っても続けて流したのと同じ結果）。
GLISS_SR = 16000
GLISS_HOP = 256
GLISS_FMIN = 50.0                 # 探索範囲（モデルの範囲は 46.875〜2093.75 Hz）。RMVPE の上限に合わせる
GLISS_FMAX = 1100.0
GLISS_CONF = 0.5                  # 確信度がこれ以上なら有声（較正済み）
GLISS_SILENCE_PEAK = 1e-3         # 音のピークがこれ未満のフレームは確信度 0（デジタル無音で偽の有声を出す）
_GLISS_WINDOW = 1875
_GLISS_LEFT = 11
_GLISS_LOOKAHEAD = 10
GLISS_VOICING = "confidence >= %.2f (%g-%g Hz)" % (GLISS_CONF, GLISS_FMIN, GLISS_FMAX)


def _gliss_session():
    key = ("gliss", GLISS_F0_PATH)
    with _MODEL_LOCK:
        if key not in _MODEL_CACHE:
            import onnxruntime as ort
            if not os.path.exists(GLISS_F0_PATH):
                raise ModelMissingError("Gliss の F0 モデルが見つからない: %s（エンジンと一緒に配るファイル）"
                                        % GLISS_F0_PATH)
            so = ort.SessionOptions()
            so.log_severity_level = 3
            so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))   # 小さいモデル。これ以上は効かない
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            _MODEL_CACHE[key] = ort.InferenceSession(GLISS_F0_PATH, sess_options=so,
                                                     providers=["CPUExecutionProvider"])
        return _MODEL_CACHE[key]


def _gliss_run(sess, audio):
    pitch, conf = sess.run(["pitch", "confidence"], {
        "audio": audio[None, :], "fmin": np.asarray(GLISS_FMIN, dtype=np.float32),
        "fmax": np.asarray(GLISS_FMAX, dtype=np.float32)})
    pitch = np.asarray(pitch[0], dtype="float64")
    conf = np.asarray(conf[0], dtype="float64")
    n = len(conf)
    if len(audio) >= GLISS_HOP:
        peak = np.abs(audio[:n * GLISS_HOP].reshape(n, GLISS_HOP)).max(axis=1)
    else:
        peak = np.full(n, np.abs(audio).max() if len(audio) else 0.0)
    conf[peak < GLISS_SILENCE_PEAK] = 0.0
    return pitch, conf


def gliss_raw(x, sr):
    """Gliss の F0 モデルの生の出力。-> 時刻（秒。フレームの中心）, F0（Hz）, 確信度（0..1）。16 ms 刻み。"""
    sess = _gliss_session()
    sig = np.asarray(x, dtype="float32")
    if int(sr) != GLISS_SR:
        import soxr
        sig = soxr.resample(sig, int(sr), GLISS_SR).astype("float32")
    if sig.size == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    n = max(1, len(sig) // GLISS_HOP)
    pitch, conf = [], []
    for start in range(0, n, _GLISS_WINDOW):
        end = min(start + _GLISS_WINDOW, n)
        left = max(0, start - _GLISS_LEFT)
        win = (sig[left * GLISS_HOP:(end + _GLISS_LOOKAHEAD) * GLISS_HOP] if end < n
               else sig[left * GLISS_HOP:])
        p, c = _gliss_run(sess, np.ascontiguousarray(win))
        pitch.append(p[start - left:end - left])
        conf.append(c[start - left:end - left])
    pitch, conf = np.concatenate(pitch), np.concatenate(conf)
    return np.arange(len(pitch)) * (GLISS_HOP / GLISS_SR), pitch, conf


def _estimate_gliss(x, sr, n_frames):
    """_estimate_rmvpe と同じ戻り値: (10 ms 格子の F0, 確信度)。"""
    ts, f, c = gliss_raw(x, sr)
    voiced = (c >= GLISS_CONF) & (f > 0)
    f0g = _to_grid(ts, np.where(voiced, f, 0.0), n_frames)
    confg = _to_grid(ts, c, n_frames, is_f0=False) if len(ts) else np.zeros(n_frames)
    confg = np.where(f0g > 0, np.clip(confg, 0.0, 1.0), 0.0)
    return f0g, confg


# ---------------------------------------------------------------- Praat（歌声向けに調整したもの）
# 歌声の評価用データ（歌い手単位で分けた調整用の半分）で選んだ設定。Praat 自身の有声の判定はほぼ無効にして
# （voicing threshold 0.05）、strength のヒステリシスで決める（囁きを有声にしにくく、有声を取りこぼしにくい）。
PRAAT_FLOOR = 75.0              # Hz。窓 = 6/floor（very accurate）= 80 ms
PRAAT_CEIL = 1100.0             # Hz。RMVPE と同じ上限
PRAAT_OCTAVE_COST = 0.1         # 既定 0.01。低いオクターブへの引きを抑える（最も効いた引数）
PRAAT_OCTAVE_JUMP_COST = 1.2
PRAAT_VUV_COST = 0.14
PRAAT_SILENCE_THR = 0.01
PRAAT_VOICING_THR = 0.05
PRAAT_VUV_HI = 0.8              # 有声の区間は strength >= 0.8 の点を 1 つ以上含み、
PRAAT_VUV_LO = 0.5              # strength >= 0.5 の範囲に広がり、
PRAAT_VUV_MINRUN = 3            # 3 フレーム（30 ms）以上続く
PRAAT_VOICING = "praat strength hysteresis (hi %.2f / lo %.2f / minrun %d)" % (
    PRAAT_VUV_HI, PRAAT_VUV_LO, PRAAT_VUV_MINRUN)


def _praat_hysteresis(strength, ok, hi=PRAAT_VUV_HI, lo=PRAAT_VUV_LO, minrun=PRAAT_VUV_MINRUN):
    from scipy import ndimage
    m = ok & (strength >= lo)
    lab, n = ndimage.label(m)
    if n == 0:
        return m
    idx = np.arange(1, n + 1)
    mx = ndimage.maximum(strength, lab, index=idx)
    ln = ndimage.sum(m, lab, index=idx)
    keep = np.concatenate([[False], (mx >= hi) & (ln >= minrun)])
    return keep[lab]


def praat_raw(x, sr):
    """Praat の 1 回の呼び出し。-> 時刻, F0（Hz。Praat が無声と見た所は 0）, strength（0..1）"""
    import parselmouth
    snd = parselmouth.Sound(np.asarray(x, dtype="float64"), sampling_frequency=float(sr))
    p = snd.to_pitch_ac(time_step=HOP_S, pitch_floor=PRAAT_FLOOR, max_number_of_candidates=15,
                        very_accurate=True, silence_threshold=PRAAT_SILENCE_THR,
                        voicing_threshold=PRAAT_VOICING_THR, octave_cost=PRAAT_OCTAVE_COST,
                        octave_jump_cost=PRAAT_OCTAVE_JUMP_COST, voiced_unvoiced_cost=PRAAT_VUV_COST,
                        pitch_ceiling=PRAAT_CEIL)
    a = p.selected_array
    return (np.asarray(p.xs(), dtype="float64"), np.asarray(a["frequency"], dtype="float64"),
            np.asarray(a["strength"], dtype="float64"))


def _estimate_praat(x, sr, n_frames):
    """_estimate_rmvpe と同じ戻り値: (10 ms 格子の F0, 確信度 = strength)。"""
    if len(x) < int(sr * 6.0 / PRAAT_FLOOR) + 1:
        return np.zeros(n_frames), np.zeros(n_frames)     # 窓 1 つ分より短い: Praat は解析できない
    ts, f, s = praat_raw(x, sr)
    if len(ts) == 0:
        return np.zeros(n_frames), np.zeros(n_frames)
    voiced = _praat_hysteresis(s, f > 0)
    f0g = _to_grid(ts, np.where(voiced, f, 0.0), n_frames)
    conf = np.interp(np.arange(n_frames) * HOP_S, ts, np.where(voiced, s, 0.0))
    conf = np.where(f0g > 0, np.clip(conf, 0.0, 1.0), 0.0)
    return f0g, conf


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
