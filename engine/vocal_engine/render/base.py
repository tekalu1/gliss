# -*- coding: utf-8 -*-
"""レンダリングのバックエンド抽象。

既定は **Praat（praat-parselmouth）の TD-PSOLA**（`praat`。2026-09-23 から）。
段階0〜3の聴き比べで伸縮 1 位・ピッチ次点。このツールを GPLv3（GPL-3.0-or-later）で配布すると決めたので
GPL の parselmouth を製品に入れられるようになった（2026-09-23）。
自前の TD-PSOLA（`psola`）と WORLD（`world`）は選択肢として残す（MCP の `backend` 引数で選ぶ）。
parselmouth が import できない環境では `praat` を頼んでも自前 `psola` に落とし、警告を出す。

バックエンドの責務はただ一つ:
    render(range) -> wav（その区間だけの波形。前後のつなぎはパイプライン側の仕事）
"""
from abc import ABC, abstractmethod

import numpy as np


class RenderBackend(ABC):
    name = "base"
    license = ""

    @abstractmethod
    def prepare(self, x, sr, f0, voiced, hop_s=0.010):
        """解析結果を受け取り、レンダリング用の状態（context）を返す。"""

    @abstractmethod
    def render(self, ctx, start_sec, end_sec, cents=0.0, ratio=1.0, curve_points=None):
        """区間を編集して (波形(n,), info dict) を返す。"""

    def info(self):
        return {"backend": self.name, "license": self.license}


class PsolaBackend(RenderBackend):
    name = "psola"
    license = "自前実装（このリポジトリ。本体と同じ GPL-3.0-or-later）"

    def prepare(self, x, sr, f0, voiced, hop_s=0.010):
        from .psola import PsolaAnalysis
        return PsolaAnalysis(x, sr, f0, voiced, hop_s)

    def render(self, ctx, start_sec, end_sec, cents=0.0, ratio=1.0, curve_points=None):
        from .psola import render_segment
        return render_segment(ctx, start_sec, end_sec, cents=cents, ratio=ratio,
                              curve_points=curve_points)


class WorldBackend(RenderBackend):
    """段階0（方式の評価）の WORLD の書き出しの移植（薄く）。

    解析 F0 は RMVPE のものを 5 ms グリッドへ載せ替えて使う（段階0で実測改善を確認済み）。
    """
    name = "world"
    license = "WORLD: Modified BSD-3 / pyworld-prebuilt: MIT"
    FRAME_PERIOD = 5.0

    def prepare(self, x, sr, f0, voiced, hop_s=0.010):
        import pyworld as pw
        x = np.ascontiguousarray(np.asarray(x, dtype="float64"))
        step = self.FRAME_PERIOD / 1000.0
        n_frames = int(len(x) / sr / step) + 1
        t = np.arange(n_frames) * step
        src_t = np.arange(len(f0)) * hop_s
        v = np.asarray(f0) > 0
        wf0 = np.zeros(n_frames)
        if v.any():
            wf0 = np.exp(np.interp(t, src_t[v], np.log(np.asarray(f0)[v])))
        near = np.clip(np.round(t / hop_s).astype(int), 0, len(f0) - 1)
        wf0[~v[near]] = 0.0
        wf0 = np.ascontiguousarray(wf0)
        t = np.ascontiguousarray(t)
        sp = pw.cheaptrick(x, wf0, t, sr)
        ap = pw.d4c(x, wf0, t, sr)
        return {"x": x, "sr": int(sr), "f0": wf0, "t": t, "sp": sp, "ap": ap}

    def render(self, ctx, start_sec, end_sec, cents=0.0, ratio=1.0, curve_points=None):
        import pyworld as pw
        step = self.FRAME_PERIOD / 1000.0
        sr = ctx["sr"]
        f0, sp, ap = ctx["f0"], ctx["sp"], ctx["ap"]
        i0 = int(np.floor(start_sec / step))
        i1 = int(np.ceil(end_sec / step))
        i0 = max(0, i0)
        i1 = min(len(f0), max(i0 + 1, i1))
        n_in = i1 - i0
        n_out = max(1, int(round(n_in * ratio)))
        src = np.clip(np.arange(n_out) / float(ratio), 0, n_in - 1)
        a = np.floor(src).astype(int)
        b = np.clip(a + 1, 0, n_in - 1)
        w = (src - a)[:, None]
        spA, apA, f0A = sp[i0:i1], ap[i0:i1], f0[i0:i1]
        sp_o = np.exp((1 - w) * np.log(np.maximum(spA[a], 1e-16))
                      + w * np.log(np.maximum(spA[b], 1e-16)))
        ap_o = np.clip((1 - w) * apA[a] + w * apA[b], 1e-16, 1.0)
        v0, v1 = f0A[a] > 0, f0A[b] > 0
        wv = w[:, 0]
        lf = np.zeros(n_out)
        both = v0 & v1
        lf[both] = (1 - wv[both]) * np.log(f0A[a][both]) + wv[both] * np.log(f0A[b][both])
        lf[v0 & ~v1] = np.log(f0A[a][v0 & ~v1])
        lf[~v0 & v1] = np.log(f0A[b][~v0 & v1])
        f0_o = np.where(v0 | v1, np.exp(lf), 0.0)
        if curve_points:
            tt = np.array([float(t) for t, _ in curve_points])
            ff = np.array([2.0 ** (float(c) / 1200.0) for _, c in curve_points])
            t_rel = np.arange(n_out) * step / max(ratio, 1e-6)
            f0_o = f0_o * np.interp(t_rel, tt, ff)
        else:
            f0_o = f0_o * (2.0 ** (float(cents) / 1200.0))
        y = pw.synthesize(np.ascontiguousarray(f0_o), np.ascontiguousarray(sp_o),
                          np.ascontiguousarray(ap_o), sr, self.FRAME_PERIOD)
        y = np.asarray(y, dtype="float64")
        want = int(round((end_sec - start_sec) * sr * ratio))
        if len(y) > want:
            y = y[:want]
        elif len(y) < want:
            y = np.concatenate([y, np.zeros(want - len(y))])
        return y, {"backend": "world", "frames": n_out}


class PraatBackend(RenderBackend):
    """Praat の Manipulation（TD-PSOLA）。段階0（方式の評価）の Praat の書き出しと同じ設定。"""
    name = "praat"
    license = ("Praat / praat-parselmouth: GPL-3.0-or-later"
               "（このツール本体は GPL-3.0-or-later）")

    def prepare(self, x, sr, f0, voiced, hop_s=0.010, ref=None):
        """ref: 同じ素材をモノラル化した音の context（ステレオ書き出しで解析とゲインを共有する）。"""
        from .praat import PraatContext
        return PraatContext(x, sr, f0, voiced, hop_s, ref=ref)

    def render(self, ctx, start_sec, end_sec, cents=0.0, ratio=1.0, curve_points=None):
        from .praat import render_segment
        return render_segment(ctx, start_sec, end_sec, cents=cents, ratio=ratio,
                              curve_points=curve_points)


DEFAULT_BACKEND = "praat"
FALLBACK_BACKEND = "psola"
_BACKENDS = {"praat": PraatBackend, "psola": PsolaBackend, "world": WorldBackend}
_warned = set()


def _available(name):
    if name == "praat":
        from .praat import available
        return available()
    if name == "world":
        try:
            import pyworld  # noqa: F401
            return True
        except Exception:
            return False
    return True


def resolve_backend_name(name=None):
    """実際に使うバックエンド名。None は既定（praat）。praat が使えない環境では psola に落とす。"""
    name = DEFAULT_BACKEND if name is None else str(name)
    if name not in _BACKENDS:
        raise ValueError("未知のバックエンド: %r（使えるのは %s）"
                         % (name, ", ".join(sorted(_BACKENDS))))
    if name == "praat" and not _available("praat"):
        if name not in _warned:
            _warned.add(name)
            msg = ("praat-parselmouth を import できないので、Praat の代わりに自前の TD-PSOLA（psola）で"
                   "再合成する。`uv pip install praat-parselmouth` で入れると既定の Praat に戻る")
            import warnings
            warnings.warn(msg, RuntimeWarning, stacklevel=3)
            from .. import log
            log.get().warning(msg)
        return FALLBACK_BACKEND
    return name


def get_backend(name=None):
    return _BACKENDS[resolve_backend_name(name)]()


def list_backends():
    return [{"name": k, **v().info(), "default": k == DEFAULT_BACKEND, "available": _available(k)}
            for k, v in _BACKENDS.items()]
