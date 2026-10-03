# -*- coding: utf-8 -*-
"""区間 → PCM の再合成 API（DAW 連携 段階 0 §2。`docs/daw-stage0.md`）。

`render_preview` は「聞いて確かめる」ための切り出しで、範囲の端が編集の途中でもそこで切って
再合成する（長さも編集で変わる）。DAW（ARA の PlaybackRenderer やキャッシュ）に渡す PCM は約束が違う:

  - **プロジェクト（クリップ）の時間軸のまま、長さが変わらない**（[a, b) を頼めば b − a サンプル）。
  - **中身は `export_wav` が書くものと同じ。** 編集のかたまりごとの窓（端は静かなところ。
    `render/export.py` の `_windows`）を丸ごと再合成して、頼まれた範囲を切り出す。
    窓の外は元のサンプルそのもの。
  - 編集を 1 回したら、**変わった窓だけ**を再合成すればよい（`dirty_windows`）。
    `EditCache` がその差分更新を持つ（プラグインのキャッシュ＝再生はそこを読むだけ、の形）。

`RegionRenderer` はチャンネルごとのレンダラ（バックエンドの下ごしらえ）を持ち回す。
下ごしらえ（Praat ならパルスと PitchTier、自前 psola なら素材全体の解析）は素材の長さに比例して
重いので、1 回作って使い回すこと。`export_wav` もこれを使う。
"""
import time

import numpy as np

from .. import media as M
from .base import resolve_backend_name
from .pipeline import Renderer


class RegionRenderer:
    """クリップの音（(n,) か (n, ch)）→ 区間ごとの再合成。

    多チャンネルで praat なら、モノラル化した音でパルスと PitchTier を 1 回だけ作って全チャンネルで
    共有し、音量合わせのゲインとクロスフェードの相関もモノラルで決めて全チャンネルにそろえる
    （左右のバランスを保つ。`export_wav` と同じ）。
    """

    def __init__(self, x, sr, f0r, backend=None):
        x = np.asarray(x, dtype="float64")
        if x.ndim == 1:
            x = x[:, None]
        self.x = x
        self.sr = int(sr)
        self.f0r = f0r
        self.n_frames, self.n_ch = x.shape
        self.backend = backend
        self.backend_name = resolve_backend_name(backend)
        self._rends = [None] * self.n_ch
        self._ref = None
        self._ref_done = False
        self.prepare_sec = 0.0          # 下ごしらえに掛かった秒（計測用）

    @classmethod
    def for_project(cls, project, backend=None, channels="mono"):
        """channels: "mono"（解析と同じモノラル。プレビュー・計測向け）/ "all"（素材のチャンネルそのまま）。"""
        project.ensure_analyzed()
        if channels == "mono":
            x, sr = project.audio("take")
        elif channels == "all":
            x, sr = M.read_clip(project.take)
        else:
            raise ValueError("channels は mono か all")
        return cls(x, sr, project.take_f0, backend=backend)

    def _get_ref(self):
        if not self._ref_done:
            self._ref_done = True
            if self.n_ch > 1 and self.backend_name == "praat":
                t = time.perf_counter()
                f = self.f0r
                self._ref = Renderer(self.x.mean(axis=1), self.sr, f.f0, f.voiced, f.hop_s,
                                     backend=self.backend)
                self.prepare_sec += time.perf_counter() - t
        return self._ref

    def _get(self, ch):
        if self._rends[ch] is None:
            ref = self._get_ref()
            t = time.perf_counter()
            f = self.f0r
            self._rends[ch] = Renderer(self.x[:, ch], self.sr, f.f0, f.voiced, f.hop_s,
                                       backend=self.backend, ref=ref)
            self.prepare_sec += time.perf_counter() - t
        return self._rends[ch]

    def prepare(self):
        """下ごしらえを先に済ませる（プラグインなら素材を受け取った時点で）。掛かった秒を返す。"""
        for ch in range(self.n_ch):
            self._get(ch)
        return self.prepare_sec

    @property
    def actual_backend(self):
        r = next((r for r in self._rends if r is not None), None)
        return r.backend_name if r is not None else self.backend_name

    def render_frames(self, ia, ib, segs):
        """[ia, ib)（クリップ内のサンプル）を**丸ごと**再合成して (y (ib−ia, ch), meta)。

        長さは必ず ib − ia。窓の端は静かなところに取ってあること（`windows_for`）。"""
        ia, ib = max(0, int(ia)), min(self.n_frames, int(ib))
        n = max(0, ib - ia)
        y = np.zeros((n, self.n_ch))
        warnings = []
        if n < 2:
            y[:] = self.x[ia:ib]
            return y, {"warnings": warnings, "backend": self.actual_backend}
        sr = self.sr
        want_sec = n / sr
        rhos = lags = None
        ref = self._get_ref()
        if ref is not None:                  # クロスフェードの相関・位相をそろえた量もモノラルで決めてそろえる
            ref_info = ref.render_range(ia / sr, ib / sr, segs, fit_out_sec=want_sec)[1]
            rhos, lags = ref_info["xfade_rhos"], ref_info["xfade_lags"]
        for ch in range(self.n_ch):
            r = self._get(ch)
            yc, meta = r.render_range(ia / sr, ib / sr, segs, fit_out_sec=want_sec,
                                      xfade_rhos=rhos, xfade_lags=lags)
            warnings += [w for w in meta["warnings"] if w not in warnings]   # チャンネル間の重複は 1 つに
            if len(yc) != n:                  # 念のため（fit_out_sec が効いていれば通らない）
                yc = yc[:n] if len(yc) > n else np.concatenate([yc, np.zeros(n - len(yc))])
            y[:, ch] = yc
        return y, {"warnings": warnings, "backend": self.actual_backend}


# ---------------------------------------------------------------- 窓
def windows_for(project, segs, t0=0.0, t1=None):
    """編集のかたまり → 差し替える窓 [[a, b], ...]（秒。`export_wav` と同じ規則）。"""
    from .export import _windows
    t1 = project.duration_sec if t1 is None else t1
    return _windows(project, segs, t0, t1)


def _seg_key(s):
    return (round(s.start_sec, 9), round(s.end_sec, 9), round(s.cents, 9), round(s.ratio, 12),
            round(s.move_ms, 9), round(s.silence_sec, 9), round(s.gain, 9),
            getattr(s, "fade", None),
            None if not s.curve_points else tuple((round(float(t), 9), round(float(c), 9))
                                                  for t, c in s.curve_points))


def _merge(spans):
    spans = sorted([list(s) for s in spans])
    out = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def changed_spans(segs_before, segs_after):
    """編集の前後で中身が変わった Segment の範囲（秒）。"""
    kb = {_seg_key(s): s for s in segs_before if not s.is_identity()}
    ka = {_seg_key(s): s for s in segs_after if not s.is_identity()}
    diff = [kb[k] for k in kb.keys() - ka.keys()] + [ka[k] for k in ka.keys() - kb.keys()]
    return _merge([[s.start_sec, s.end_sec] for s in diff])


def _overlaps(w, spans):
    return any(w[0] <= b + 1e-9 and a <= w[1] + 1e-9 for a, b in spans)


def dirty_windows(project, segs_before, segs_after, windows_before=None, windows_after=None):
    """1 回の編集（か undo）で**差し替え直す範囲**（秒の [[a, b], ...]）。

    変わった Segment に掛かる窓（編集前の窓と編集後の窓の両方）から始めて、その範囲に重なる窓を
    どちらの側からも足していく（窓の端が動いた隣のかたまりも含める）。範囲の外は編集の前後で
    出力が同じ（元のサンプルか、同じ窓の同じ再合成）。範囲の中は、元のサンプルに戻してから
    **編集後の窓を 1 つずつ**再合成して置けば `export_wav` と同じ中身になる（`EditCache.update`）。"""
    ch = changed_spans(segs_before, segs_after)
    if not ch:
        return []
    wb = windows_before if windows_before is not None else windows_for(project, segs_before)
    wa = windows_after if windows_after is not None else windows_for(project, segs_after)
    cand = [list(w) for w in wb + wa]
    dirty = _merge(ch + [w for w in cand if _overlaps(w, ch)])
    while True:
        grown = _merge(dirty + [w for w in cand if _overlaps(w, dirty)])
        if grown == dirty:
            return dirty
        dirty = grown


# ---------------------------------------------------------------- 区間 → PCM
def render_region(project, start_sec=None, end_sec=None, backend=None, channels="mono",
                  renderer=None, segs=None):
    """編集を当てた [start, end)（プロジェクト＝クリップ内の秒）の PCM を返す。(y, info)

    y は (n, ch) の float64、n = 頼んだ範囲のサンプル数（**長さは変わらない**）。
    中身は `export_wav` が同じ範囲に書くものと同じ（掛かる窓を丸ごと再合成して切り出す）。
    info: sr・クリップ内とソース上の開始サンプル・再合成した窓・掛かった秒。
    """
    t_all = time.perf_counter()
    rr = renderer or RegionRenderer.for_project(project, backend=backend, channels=channels)
    sr = rr.sr
    ia = 0 if start_sec is None else max(0, int(round(float(start_sec) * sr)))
    ib = rr.n_frames if end_sec is None else min(rr.n_frames, int(round(float(end_sec) * sr)))
    if ib <= ia:
        raise ValueError("範囲が不正（start >= end）")
    t = time.perf_counter()
    if segs is None:
        from ..project.pitch import layered_segments
        segs = layered_segments(project)
    seg_sec = time.perf_counter() - t
    y = rr.x[ia:ib].copy()
    rendered, warnings = [], []
    prep0 = rr.prepare_sec
    t = time.perf_counter()
    for a, b in windows_for(project, segs):
        wa, wb = int(round(a * sr)), int(round(b * sr))
        if wb <= ia or wa >= ib or wb - wa < 2:
            continue
        yw, meta = rr.render_frames(wa, wb, segs)
        lo, hi = max(ia, wa), min(ib, wb)
        y[lo - ia:hi - ia] = yw[lo - wa:hi - wa]
        rendered.append([round(wa / sr, 4), round(wb / sr, 4)])
        warnings += [w for w in meta["warnings"] if w not in warnings]
    render_sec = time.perf_counter() - t - (rr.prepare_sec - prep0)
    off = int(project.take.get("offset_frames", 0) or 0)
    return y, {
        "sr": sr, "channels": rr.n_ch, "frames": int(ib - ia),
        "start_frame": int(ia), "source_start_frame": int(off + ia),
        "start_sec": round(ia / sr, 6), "source_start_sec": round((off + ia) / sr, 6),
        "rendered_windows_sec": rendered, "backend": rr.actual_backend,
        "timing_sec": {"segments": round(seg_sec, 4), "prepare": round(rr.prepare_sec - prep0, 4),
                       "render": round(render_sec, 4),
                       "total": round(time.perf_counter() - t_all, 4)},
        "warnings": warnings,
    }


def audition_segments(project, note_id, cents=0.0):
    """ノートを cents だけ動かした**つもり**の Segment 列（プロジェクトは書き換えない。issue #27）。

    画面でピッチをドラッグしている間のプレビュー音用。確定した編集に、そのノートの pitch_shift を
    1 つ足した列で層を当てる（離したときに当たる `shift_pitch` と同じ中身）。フェードの印も足す
    （pitch_shift は時間を動かさないので、確定した編集の時間のまま）。"""
    from ..project.model import Edit, Target
    from ..project.pitch import layered_segments
    project.note(note_id)                   # 無ければここで例外
    if abs(float(cents)) < 1e-6:
        return layered_segments(project)
    edits = list(project.edits) + [Edit(id="audition", kind="pitch_shift",
                                        target=Target.note(note_id),
                                        params={"cents": float(cents)}, author="human")]
    segs = layered_segments(project, edits)
    from ..project.fades import fade_segments
    return list(segs) + fade_segments(project)


def audition(project, note_id, cents=0.0, start_sec=None, end_sec=None, renderer=None,
             backend=None):
    """つかんだノートのプレビュー音: ノートを cents だけ動かしたつもりで [start, end) を再合成する。(y, info)

    範囲の既定はノートの編集前の範囲（タイミングを動かしたノートは画面が編集後の範囲を渡す）。
    中身は `render_region` と同じ（掛かる窓を丸ごと再合成して切り出す）。"""
    n = project.note(note_id)
    t0 = n.start_sec if start_sec is None else float(start_sec)
    t1 = n.end_sec if end_sec is None else float(end_sec)
    segs = audition_segments(project, note_id, cents)
    y, info = render_region(project, t0, t1, backend=backend, renderer=renderer, segs=segs)
    info = dict(info, note_id=note_id, cents=round(float(cents), 3))
    return y, info


class EditCache:
    """クリップ全体の「編集を当てた PCM」を持ち、**編集ごとに変わった窓だけ**を再合成して差し替える。

    ARA の PlaybackRenderer がキャッシュを読むだけにする形（VST3 / ARA の調査の段階 3）の試作。
    `update()` を編集（か undo / redo）のたびに呼ぶ。返り値の `timing_sec` が
    「1 回の編集の再合成時間」（Segment の組み直し＋窓の再合成）。
    """

    def __init__(self, project, backend=None, channels="mono"):
        self.project = project
        self.rr = RegionRenderer.for_project(project, backend=backend, channels=channels)
        self.sr = self.rr.sr
        self.pcm = self.rr.x.copy()
        self.segs = []
        self.windows = []
        self.prepare_sec = None

    def prepare(self):
        self.prepare_sec = self.rr.prepare()
        return self.prepare_sec

    def update(self):
        from ..project.pitch import layered_segments
        t_all = time.perf_counter()
        t = time.perf_counter()
        segs = layered_segments(self.project)
        seg_sec = time.perf_counter() - t
        t = time.perf_counter()
        wins = windows_for(self.project, segs)
        dirty = dirty_windows(self.project, self.segs, segs, self.windows, wins)
        dirty_sec = time.perf_counter() - t
        prep0 = self.rr.prepare_sec
        t = time.perf_counter()
        warnings = []
        sr, n = self.sr, self.rr.n_frames
        spans, rendered = [], []
        for a, b in dirty:                        # いったん元のサンプルに戻す
            da, db = max(0, int(round(a * sr))), min(n, int(round(b * sr)))
            self.pcm[da:db] = self.rr.x[da:db]
            spans.append([round(da / sr, 4), round(db / sr, 4)])
        for w in wins:                            # 掛かる窓を 1 つずつ（export_wav と同じ切り方）
            if not _overlaps(w, dirty):
                continue
            wa, wb = max(0, int(round(w[0] * sr))), min(n, int(round(w[1] * sr)))
            if wb - wa < 2:
                continue
            yw, meta = self.rr.render_frames(wa, wb, segs)
            self.pcm[wa:wb] = yw
            rendered.append([round(wa / sr, 4), round(wb / sr, 4)])
            warnings += [x for x in meta["warnings"] if x not in warnings]
        render_sec = time.perf_counter() - t - (self.rr.prepare_sec - prep0)
        self.segs, self.windows = segs, wins
        return {
            "dirty_sec_ranges": spans,
            "dirty_sec": round(sum(b - a for a, b in spans), 4),
            "rendered_windows_sec": rendered,
            "rendered_sec": round(sum(b - a for a, b in rendered), 4),
            "timing_sec": {"segments": round(seg_sec, 4), "dirty": round(dirty_sec, 4),
                           "prepare": round(self.rr.prepare_sec - prep0, 4),
                           "render": round(render_sec, 4),
                           "total": round(time.perf_counter() - t_all, 4)},
            "backend": self.rr.actual_backend,
            "warnings": warnings,
        }
