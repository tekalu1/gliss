# -*- coding: utf-8 -*-
"""再合成の回帰値: 素材 A / C / E に +3 半音・1.3 倍を掛け、F0 の誤差と声質の距離を測る。

回帰値は素材から作るので、素材のフォルダの `regression-baseline.json` に置く（リポジトリには入れない）。
`test_psola_regression.py` がそれと突き合わせる。

測り方（どの出力にも同じものを掛ける）:
  F0 誤差   RMVPE で出力の F0 を測り、目標（元の F0 × 2**(n/12)／元の F0 を時間軸で伸縮）との差をセントで。
            有声フレームの RMSE・|誤差|>50c の率・中央値。
  声質保持  元と出力のスペクトル包絡の距離（MCD, dB）。CheapTrick の包絡を 24 次にして比べる。

回帰値の作り直し（GLISS_TEST_MATERIALS と重みが要る）:
  python engine/tests/regression_measure.py            （--backend world で WORLD 版、--no-write で表示だけ）
"""
import argparse
import json
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import materials as M  # noqa: E402

OUT_DIR = os.path.join(tempfile.gettempdir(), "gliss-regression-out")
BASELINE_NAME = "regression-baseline.json"
SYMBOLS = ["A", "C", "E"]
CASES = [("pitch", {"semitones": 3}), ("stretch", {"ratio": 1.3, "voiced_only": False})]
MCEP_ORDER = 24
HOP_S = 0.010

_RMVPE = None
_CACHE = {}


def baseline_path():
    return M.path(BASELINE_NAME)


def _read(path):
    import soundfile as sf
    x, sr = sf.read(path, always_2d=True, dtype="float64")
    return x.mean(axis=1), sr


def _rmvpe():
    global _RMVPE
    if _RMVPE is None:
        from vocal_engine.analysis.rmvpe_onnx import RMVPE
        _RMVPE = RMVPE()
    return _RMVPE


def forget(path):
    """出力を書き直したときに、前の測定のキャッシュを捨てる。"""
    for kind in ("f0", "mcep"):
        _CACHE.pop((kind, os.path.abspath(path)), None)


def f0_of(path):
    """(f0, conf) を 10 ms ホップで。結果はキャッシュする。"""
    key = ("f0", os.path.abspath(path))
    if key not in _CACHE:
        _CACHE[key] = _rmvpe().infer_file(path, sweep=False)
    return _CACHE[key]


def mcep_of(path, f0):
    """CheapTrick 包絡 → 24 次のコード化包絡（MCD 用）。f0 は 10 ms の列。"""
    import pyworld as pw
    key = ("mcep", os.path.abspath(path))
    if key in _CACHE:
        return _CACHE[key]
    xm, sr = _read(path)
    xm = np.ascontiguousarray(xm)
    n = int(np.floor(len(xm) / sr / HOP_S)) + 1
    f0u = np.zeros(n)
    m = min(n, len(f0))
    f0u[:m] = f0[:m]
    t = np.arange(n) * HOP_S
    sp = pw.cheaptrick(xm, np.ascontiguousarray(f0u), np.ascontiguousarray(t), sr)
    mc = pw.code_spectral_envelope(sp, sr, MCEP_ORDER)
    _CACHE[key] = (mc, f0u, sr)
    return _CACHE[key]


def cents_err(out_f0, tgt_f0):
    v = (out_f0 > 0) & (tgt_f0 > 0)
    if v.sum() == 0:
        return {"n_voiced": 0, "rmse_cents": None, "over50c_pct": None,
                "median_abs_cents": None, "mean_cents": None}
    d = 1200.0 * np.log2(out_f0[v] / tgt_f0[v])
    return {
        "n_voiced": int(v.sum()),
        "rmse_cents": float(np.sqrt(np.mean(d ** 2))),
        "over50c_pct": float(100.0 * np.mean(np.abs(d) > 50.0)),
        "median_abs_cents": float(np.median(np.abs(d))),
        "mean_cents": float(np.mean(d)),
    }


def mcd(mc_ref, mc_out, idx_ref=None, idx_out=None):
    """MCD (dB)。0 次（パワー）は除く。"""
    if idx_ref is None:
        n = min(len(mc_ref), len(mc_out))
        a, b = mc_ref[:n, 1:], mc_out[:n, 1:]
    else:
        a = mc_ref[np.clip(idx_ref, 0, len(mc_ref) - 1), 1:]
        b = mc_out[np.clip(idx_out, 0, len(mc_out) - 1), 1:]
    d = a - b
    per_frame = (10.0 / np.log(10.0)) * np.sqrt(2.0 * np.sum(d ** 2, axis=1))
    return float(np.mean(per_frame)), per_frame


def measure(ref_path, out_path, edit, params):
    """1 ファイルぶんの測定。edit は pitch か stretch（全体を一様に伸縮）。"""
    f0_ref, _ = f0_of(ref_path)
    f0_out, _ = f0_of(out_path)
    mc_ref, f0r, sr = mcep_of(ref_path, f0_ref)
    mc_out, _, _ = mcep_of(out_path, f0_out)
    m = {"edit": edit, "params": params}
    if edit == "pitch":
        semi = float(params["semitones"])
        n = min(len(f0_ref), len(f0_out))
        tgt = f0_ref[:n] * (2.0 ** (semi / 12.0))
        m["f0"] = cents_err(f0_out[:n], tgt)
        m["mcd_db"], _ = mcd(mc_ref, mc_out)
    elif edit == "stretch":
        if params.get("voiced_only"):
            raise ValueError("有声区間だけの伸縮は測らない")
        ratio = float(params["ratio"])
        n_in = len(_read(ref_path)[0])
        n_out = int(round(n_in * ratio))
        t_out = np.arange(len(f0_out)) * HOP_S
        src_sample = np.interp(t_out * sr, [0.0, float(n_out)], [0.0, float(n_in)])
        i = np.clip(np.round(src_sample / sr / HOP_S).astype(int), 0, len(f0_ref) - 1)
        tgt = f0_ref[i]                     # 目標 = 元の F0 を時間軸で伸縮したもの
        m["f0"] = cents_err(f0_out, tgt)
        m["mcd_db"], _ = mcd(mc_ref, mc_out, idx_ref=i, idx_out=np.arange(len(i)))
    else:
        raise ValueError(edit)
    return m


def render_case(symbol, kind, params, backend="psola"):
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono, write_wav
    from vocal_engine.render.pipeline import Renderer, Segment

    path = M.clip(symbol)
    x, sr = read_mono(path)
    f0r = estimate_f0(x=x, sr=sr, estimator="rmvpe")
    r = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s, backend=backend)
    dur = len(x) / sr
    if kind == "pitch":
        seg = Segment(0.0, dur, cents=params["semitones"] * 100.0)
        name = "%s_pitch+%d" % (symbol, params["semitones"])
    else:
        seg = Segment(0.0, dur, ratio=params["ratio"])
        name = "%s_stretch%s" % (symbol, params["ratio"])
    y, info = r.render_range(0.0, dur, [seg])
    out = os.path.join(OUT_DIR, backend, name + ".wav")
    write_wav(out, y, sr)
    forget(out)
    return out, path, name, info, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="psola")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    if not M.available(*SYMBOLS):
        sys.exit("テスト素材 %s が無い（GLISS_TEST_MATERIALS）" % ", ".join(SYMBOLS))

    rows = []
    for sym in SYMBOLS:
        for kind, params in CASES:
            out, ref, name, info, r = render_case(sym, kind, params, a.backend)
            m = measure(ref, out, kind, params)
            row = {
                "clip": sym, "edit": name, "kind": kind, "params": params,
                "backend": a.backend,
                "f0_rmse_cents": round(m["f0"]["rmse_cents"], 2),
                "f0_median_abs_cents": round(m["f0"]["median_abs_cents"], 2),
                "f0_over50c_pct": round(m["f0"]["over50c_pct"], 2),
                "n_voiced": m["f0"]["n_voiced"],
                "mcd_db": round(m["mcd_db"], 3),
                "out_samples": info["out_samples"],
            }
            if hasattr(r, "ctx") and hasattr(r.ctx, "stats"):
                row["marks"] = r.ctx.stats()
            rows.append(row)
            print("%-16s F0 RMSE %7.1f c  median %6.1f c  >50c %5.1f%%  MCD %5.2f dB"
                  % (name, row["f0_rmse_cents"], row["f0_median_abs_cents"],
                     row["f0_over50c_pct"], row["mcd_db"]), flush=True)

    if not a.no_write:
        p = baseline_path()
        old = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                old = json.load(f)
        old[a.backend] = rows
        with open(p, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
        print("wrote", p)
    return rows


if __name__ == "__main__":
    main()
