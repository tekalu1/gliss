# -*- coding: utf-8 -*-
"""WAV 書き出し — **元と同じものを返し、編集したところだけ差し替える**。

`render_preview` は「聞いて確かめる」ための切り出しなので、範囲も長さも自由でよい。
書き出しは DAW に戻すファイルなので、約束が違う:

  - **サンプルレート・ビット深度・チャンネル数・総サンプル数・開始位置が元と同じ。**
    曲頭 0:00 起点のまま出る。DAW のトラックに置き直せば位置がそのまま合う。
  - **編集していないところは元のサンプルがビット単位で同じ。**
    編集のかたまりごとに窓を作り、その中だけ再合成して差し替える。
    窓の端は**いちばん静かなところ**に取る（つなぎ目が聞こえないように）。
  - 量子化のずれを持ち込まないため、**整数のまま**差し替える
    （PCM_16 / 24 / 32 は `int32` で読み書きし、非編集区間は配列をコピーするだけ）。

返り値の `replaced_spans_sec` が「差し替えた区間」。**その外は元のファイルと
完全に一致する**（`engine/tests/test_export.py` がこれを検証している）。

DAW との往復（DAW 連携 段階 1。issue #4）:

  - 元の WAV の **`bext`（BWF の TimeReference ＝ DAW 上の位置）と `iXML` を書き戻す**
    （`bwf.py`）。DAW にドロップして「元の位置へ」で元の位置に揃う。クリップ（ソースの途中から）を
    クリップの長さで書き出すときは、TimeReference をクリップの開始（offset）ぶん進める。
  - 既定の書き先は**元ファイルの隣**の `<名前>_ve.wav`。**既存のファイルは上書きしない**
    （あれば `<名前>_ve(2).wav`、`(3)` … と空いている名前にする。DAW のバウンスと同じ付け方）。
"""
import os
import re
import time

import numpy as np
import soundfile as sf

from .. import bwf, log
from ..media import display_name
from ..render.base import resolve_backend_name

QUIET_SEARCH_SEC = 1.0          # 窓の端をさがす幅
QUIET_MARGIN_SEC = 0.06         # 編集からこれ以上は離す
QUIET_WIN_SEC = 0.03            # 静けさを測る窓
CLUSTER_GAP_SEC = 0.5           # これより近い編集は 1 つの窓にまとめる
MUTE_FADE_SEC = 0.005           # クリップで消した区間の前後のフェード（再生と同じ 5 ms）

INT_SUBTYPES = {"PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "PCM_U8": 8}
FLOAT_SUBTYPES = {"FLOAT": "float32", "DOUBLE": "float64"}


class ExportError(RuntimeError):
    pass


VE_SUFFIX = "_ve"
_VE_TAIL = re.compile(r"_ve(\(\d+\))?$")


def export_dir(project):
    """既定の書き出し先のディレクトリ。**元ファイルの隣**。

    元がサンプル列（`media.Samples`。実体はプロジェクトの `sources/`）のときは `<project>/export/`。
    元の場所に書けないとき（読み取り専用の共有など）は、書き出しの時点で `<project>/export/` に
    切り替える（`export_wav`。Windows の `os.access` はディレクトリの書き込み可否を当てにできない）。
    """
    take = project.take
    src = take.get("path") or ""
    d = os.path.dirname(os.path.abspath(src)) if src else ""
    if take.get("source_kind") == "samples" or not d or not os.path.isdir(d):
        return project.sub("export")
    return d


AUDIO_EXTS = {".wav": "WAV", ".wave": "WAV", ".bwf": "WAV", ".flac": "FLAC",
              ".aif": "AIFF", ".aiff": "AIFF", ".w64": "W64", ".rf64": "RF64"}


def export_ext(project):
    """書き出しの拡張子。**中身の形式は元と同じ**なので、拡張子も元に合わせる（FLAC なら .flac）。"""
    take = project.take
    if take.get("source_kind") == "samples":
        return ".wav"
    ext = os.path.splitext(take.get("path") or "")[1].lower()
    return ext if ext in AUDIO_EXTS else ".wav"


def unique_path(d, stem, ext=".wav"):
    """`<d>/<stem><ext>` が無ければそれ、あれば `<stem>(2)`、`(3)` … の空いている名前。"""
    p = os.path.join(d, stem + ext)
    k = 2
    while os.path.exists(p) or os.path.exists(p + ".tmp.wav"):
        p = os.path.join(d, "%s(%d)%s" % (stem, k, ext))
        k += 1
    return p


def _default_stem(project):
    stem = display_name(project.take)          # クリップなら「_<開始>-<終了>」が付く
    return (_VE_TAIL.sub("", stem) or stem) + VE_SUFFIX


def default_path(project):
    """元ファイルの隣の `<take名>_ve.wav`（既存なら `_ve(2)` …。上書きしない）。

    元が `vo_ve.wav`（前に書き出したもの）なら `vo_ve(2).wav` のように `_ve` を重ねない。
    クリップなら名前に「_<開始>-<終了>」が付く。拡張子は元に合わせる（中身の形式が元と同じなので）。
    """
    return unique_path(export_dir(project), _default_stem(project), export_ext(project))


def _same_file(a, b):
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def _read_source(path, subtype):
    """元の中身を**量子化のずれ無しに**読む。(data, dtype 名) を返す。"""
    if subtype in INT_SUBTYPES:
        return sf.read(path, dtype="int32", always_2d=True)[0], "int32"
    if subtype in FLOAT_SUBTYPES:
        return sf.read(path, dtype="float64", always_2d=True)[0], "float64"
    # 圧縮系（IMA_ADPCM など）は往復で同じにならないので float で扱う
    return sf.read(path, dtype="float64", always_2d=True)[0], "float64"


def _to_float(a, kind):
    return a.astype("float64") / 2147483648.0 if kind == "int32" else np.asarray(a, "float64")


def _to_store(y, kind):
    if kind != "int32":
        return np.asarray(y, "float64")
    v = np.rint(np.asarray(y, "float64") * 2147483648.0)
    return np.clip(v, -2147483648.0, 2147483647.0).astype("int32")


def _quiet_point(rms_db, hop, t, direction, lo, hi):
    """`t` から `direction`（−1 = 前 / +1 = 後ろ）へ進んで**いちばん静かな**時刻。"""
    a = t + direction * QUIET_MARGIN_SEC
    b = t + direction * QUIET_SEARCH_SEC
    s, e = (min(a, b), max(a, b))
    s, e = max(lo, s), min(hi, e)
    if e - s < 1e-3 or rms_db is None or not len(rms_db):
        return float(np.clip(t + direction * QUIET_MARGIN_SEC, lo, hi))
    i0 = int(max(0, round(s / hop)))
    i1 = int(min(len(rms_db), round(e / hop)))
    if i1 - i0 < 2:
        return float(np.clip(t + direction * QUIET_MARGIN_SEC, lo, hi))
    w = max(1, int(round(QUIET_WIN_SEC / hop)))
    seg = np.asarray(rms_db[i0:i1], dtype="float64")
    if len(seg) > w:
        k = np.convolve(seg, np.ones(w) / w, mode="valid")
        j = int(np.argmin(k)) + w // 2
    else:
        j = int(np.argmin(seg))
    return float(np.clip((i0 + j) * hop, lo, hi))


def _windows(project, segs, t0, t1):
    """編集のかたまり → 差し替える窓（重なりは畳む）。"""
    live = sorted([s for s in segs if not s.is_identity()
                   and s.end_sec > t0 and s.start_sec < t1],
                  key=lambda s: s.start_sec)
    if not live:
        return []
    clusters = [[max(t0, live[0].start_sec), min(t1, live[0].end_sec)]]
    for s in live[1:]:
        a, b = max(t0, s.start_sec), min(t1, s.end_sec)
        if a - clusters[-1][1] <= CLUSTER_GAP_SEC:
            clusters[-1][1] = max(clusters[-1][1], b)
        else:
            clusters.append([a, b])

    f0r = project.take_f0
    rms, hop = (f0r.rms_db, f0r.hop_s) if f0r is not None else (None, 0.01)
    wins = []
    for a, b in clusters:
        wa = _quiet_point(rms, hop, a, -1, t0, max(t0, a - QUIET_MARGIN_SEC))
        wb = _quiet_point(rms, hop, b, +1, min(t1, b + QUIET_MARGIN_SEC), t1)
        wins.append([wa, wb])
    out = [wins[0]]
    for w in wins[1:]:
        if w[0] <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], w[1])
        else:
            out.append(w)
    return out


def apply_mutes(out, sr, mutes, kind, offset_frames=0):
    """クリップで消した区間（トラックの頭が 0 の秒）を 0 にし、前後 `MUTE_FADE_SEC` をフェードにする。

    区間の**外側**にフェードを置くので、区間の中は完全な 0、フェードより外は元のサンプルのまま。
    返り値は実際に 0 にした区間（秒）。`out` を書き換える。"""
    n_frames = out.shape[0]
    fade = max(1, int(round(MUTE_FADE_SEC * sr)))
    spans = []
    for a, b in mutes or []:
        ia = max(0, min(n_frames, offset_frames + int(round(float(a) * sr))))
        ib = max(0, min(n_frames, offset_frames + int(round(float(b) * sr))))
        if ib - ia < 1:
            continue
        spans.append([ia, ib])
    for ia, ib in spans:
        f0 = max(0, ia - fade)
        if ia > f0:                        # 手前: 1 → 0
            g = np.linspace(1.0, 0.0, ia - f0 + 1)[:-1]
            seg = _to_float(out[f0:ia], kind) * g[:, None]
            out[f0:ia] = _to_store(seg, kind)
        f1 = min(n_frames, ib + fade)
        if f1 > ib:                        # 後ろ: 0 → 1
            g = np.linspace(0.0, 1.0, f1 - ib + 1)[1:]
            seg = _to_float(out[ib:f1], kind) * g[:, None]
            out[ib:f1] = _to_store(seg, kind)
        out[ia:ib] = 0
    return [[round(ia / sr, 4), round(ib / sr, 4)] for ia, ib in spans]


def export_wav(project, path=None, start_sec=None, end_sec=None, backend=None,
               full_source=False, position_shift_sec=0.0, mutes=None, cancel=None, progress=None,
               commit=None):
    """編集を当てた WAV を書く。**元と同じ長さ・開始位置**で、編集区間だけ差し替える。

    テイクがソースの一部（クリップ。`media.py`）なら、既定では**クリップの長さ**の WAV を書く
    （返り値の `source_offset_sec` がソース上の開始位置）。`full_source=True` なら
    **ソースと同じ長さ**の WAV を書き、クリップの範囲だけを差し替える（DAW のトラックにそのまま戻せる）。
    ファイル全体を開いたとき（段階 2 まで）は両者は同じ。

    `position_shift_sec`: トラックの位置をずらした量（セッションの `offset_sec`。issue #7）。中身・長さは
    変えず、BWF の TimeReference（DAW 上の位置）だけをこの量だけ動かす（元に bext が無ければ足す）。
    前へずらして 0 より前になるときは 0 にして警告を返す（DAW では手で合わせる）。

    `mutes`: クリップで消した区間 [[始め, 終わり]…]（セッションのトラックの `mutes`。トラックの頭＝クリップの頭が 0 の秒）。
    その区間を 0 にし、前後 5 ms をフェードにする（聞こえているとおりに書く）。返り値の `muted_spans_sec`。
    """
    from .region import RegionRenderer
    def advance(value):
        if progress is not None:
            progress(value)
        elif cancel is not None and cancel.is_set():
            raise InterruptedError("書き出しを取り消した")

    advance(0.0)
    project.ensure_analyzed()
    take = project.take
    src_path = take["path"]
    if not os.path.exists(src_path):
        raise ExportError("元の音声が見つからない: %s" % src_path)
    if path is not None and _same_file(path, src_path):
        raise ExportError("元の音声と同じパスには書き出さない: %s" % path)
    info = sf.info(src_path)
    sr = int(info.samplerate)
    dur = project.duration_sec
    t0 = 0.0 if start_sec is None else max(0.0, float(start_sec))
    t1 = dur if end_sec is None else min(dur, float(end_sec))
    if t1 <= t0:
        raise ExportError("範囲が不正（start %.3f >= end %.3f）" % (t0, t1))

    began = time.perf_counter()
    full, kind = _read_source(src_path, info.subtype)
    off = int(take.get("offset_frames", 0) or 0)
    n_clip = int(take.get("frames", full.shape[0]))
    if off + n_clip > full.shape[0]:
        raise ExportError("クリップ（%d + %d サンプル）がソース（%d サンプル）を越えている"
                          % (off, n_clip, full.shape[0]))
    data = full[off:off + n_clip]
    n_frames, n_ch = data.shape
    out = data.copy()
    advance(0.12)

    from ..project.pitch import layered_segments
    segs = layered_segments(project)          # つなぎのなだらかさ・鉛筆まで当てる
    wins = _windows(project, segs, t0, t1)
    warnings = []
    replaced = []
    # レンダラはチャンネルごとに 1 つだけ作る（PSOLA の下ごしらえが素材の長さぶん掛かるため）。
    # F0 と V/UV は解析済みのもの（モノラル化して測ったもの）を全チャンネルで共有する。
    # praat で 2 チャンネル以上なら、モノラル化した音でパルスと PitchTier を 1 回だけ作って全チャンネルで
    # 共有し、音量合わせのゲインもモノラルの再合成で決めて全チャンネルにそろえる（左右のバランスを保つ）。
    # （render/region.py の RegionRenderer。区間 → PCM の API と同じもの）
    rr = None
    if wins:
        rr = RegionRenderer(np.stack([_to_float(data[:, c], kind) for c in range(n_ch)], axis=1),
                            sr, project.take_f0, backend=backend)

    for k, (wa, wb) in enumerate(wins):
        advance(0.18 + 0.66 * k / max(1, len(wins)))
        ia = max(0, int(round(wa * sr)))
        ib = min(n_frames, int(round(wb * sr)))
        if ib - ia < 2:
            continue
        y, meta = rr.render_frames(ia, ib, segs)
        warnings += [w for w in meta["warnings"] if w not in warnings]
        for ch in range(n_ch):
            out[ia:ib, ch] = _to_store(y[:, ch], kind)
        replaced.append([round(ia / sr, 4), round(ib / sr, 4)])
    muted = apply_mutes(out, sr, mutes, kind)
    advance(0.84)

    clip = off != 0 or n_clip != full.shape[0]
    full_source = bool(full_source) or not clip     # ファイル全体を開いたときはどちらでも同じ
    if full_source and clip:
        whole = full.copy()
        whole[off:off + n_clip] = out
        out = whole
    if muted:
        warnings.append("クリップで消した区間 %d か所を 0 にして書いた" % len(muted))
    start = 0.0 if full_source else off / sr
    expect_frames = out.shape[0]

    auto = path is None                   # 既定の置き場（元の隣・上書きしない）
    path = os.path.abspath(path or default_path(project))
    if _same_file(path, src_path):
        raise ExportError("元の音声と同じパスには書き出さない: %s" % path)
    meta = bwf.read_meta(src_path)
    # BWF の bext（DAW 上の位置）と iXML を元から書き写す。クリップの長さで書くときは
    # クリップの開始ぶん TimeReference を進める（ソースと同じ長さなら元のまま）。WAV のときだけ。
    shift = off if not full_source else 0
    pos_shift = int(round(float(position_shift_sec or 0.0) * sr))
    shift += pos_shift
    is_wav = info.format in ("WAV", "WAVEX")

    def _write(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        t = target + ".tmp.wav"
        try:
            sf.write(t, out, sr, subtype=info.subtype, format=info.format)
            b = (bwf.write_meta(t, meta, shift_frames=shift) if is_wav else
                 {"bext": False, "ixml": False, "time_reference": None, "skipped": None})
        except BaseException:
            try:
                os.remove(t)
            except OSError:
                pass
            raise
        return t, b

    try:
        tmp, bw = _write(path)
    except PermissionError:
        if not auto:
            raise
        # 元の場所に書けない（読み取り専用など）: プロジェクトの export/ に書く
        path = unique_path(project.sub("export"), _default_stem(project), export_ext(project))
        warnings.append("元の場所に書けないので %s に書き出した" % os.path.dirname(path))
        tmp, bw = _write(path)
    def before_commit(value):
        try:
            advance(value)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    before_commit(0.96)
    warnings += [w for w in bw.get("warnings", []) if w not in warnings]
    if pos_shift and is_wav and (meta.time_reference or 0) > 0:
        # DAW で途中から録ったテイク（TimeReference = 録音を始めた位置）。トラックビューでは 0 に置いているので、
        # 画面で伴奏に合わせるためにずらした量は、DAW 上の位置に二重に足される
        warnings.append("元の WAV には DAW 上の位置（%.3f 秒）が入っている。トラックビューではファイルの頭を 0 に"
                        "置くので、DAW 上の位置はそれにずらした %.3f 秒を足した。伴奏に合わせるためにずらしたのなら、"
                        "DAW では元の位置に置いてから手で合わせる" % (meta.time_reference / sr, pos_shift / sr))
    if pos_shift and is_wav:
        base_tr = (meta.time_reference or 0) + (off if not full_source else 0)
        if base_tr + pos_shift < 0:
            warnings.append("位置を前へずらした量（%.3f 秒）が元の位置より大きいので、DAW 上の位置は 0 にした"
                            "（DAW では手で合わせる）" % (-pos_shift / sr))
    elif pos_shift and not is_wav:
        warnings.append("WAV ではないので DAW 上の位置（bext）を書けない。ずらした %.3f 秒は DAW で手で合わせる"
                        % (pos_shift / sr))
    if bw.get("skipped"):
        warnings.append(bw["skipped"])
    if auto and os.path.exists(path):
        # 名前を決めてから書き終わるまでの間に同じ名前ができた（別の書き出しと重なった）: 上書きしない
        d, name = os.path.split(path)
        path = unique_path(d, os.path.splitext(name)[0], os.path.splitext(name)[1])
    before_commit(0.99)
    try:
        if commit is not None:
            commit(lambda: os.replace(tmp, path))
        else:
            os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    after = sf.info(path)
    same = {"sr": after.samplerate == sr, "channels": after.channels == n_ch,
            "subtype": after.subtype == info.subtype, "frames": after.frames == expect_frames}
    if not all(same.values()):
        warnings.append("元と揃っていない項目がある: %s"
                        % ", ".join(k for k, v in same.items() if not v))
    replaced_sec = sum(b - a for a, b in replaced)
    log.get().info("書き出し: %s（%d 区間 / %.2f 秒を差し替え / %.1f s）",
                   path, len(replaced), replaced_sec, time.perf_counter() - began)
    return {
        "path": path,
        "source_path": src_path,
        "sr": sr, "channels": n_ch, "subtype": info.subtype,
        "frames": int(after.frames), "duration_sec": round(after.frames / sr, 6),
        "start_sec": round(start, 6),
        "same_as_source": same,
        "edits": len(project.edits),
        "replaced_spans_sec": replaced,
        "replaced_sec": round(replaced_sec, 3),
        "muted_spans_sec": muted,
        "range_sec": [round(t0, 3), round(t1, 3)],
        "source_id": take.get("source_id"),
        "source_offset_sec": round(off / sr, 6),
        "full_source": full_source,
        "bwf": {"bext": bw["bext"], "ixml": bw["ixml"], "time_reference": bw["time_reference"],
                "time_reference_sec": (round(bw["time_reference"] / sr, 6)
                                       if bw["time_reference"] is not None else None),
                "source_time_reference": meta.time_reference},
        "backend": (rr.actual_backend if rr is not None else resolve_backend_name(backend)),
        "elapsed_sec": round(time.perf_counter() - began, 2),
        "warnings": warnings,
        "note": ("曲頭 0:00 起点のまま。replaced_spans_sec の外は元のファイルと完全に同じサンプル"
                 if not clip else
                 ("ソースと同じ長さ（0:00 起点）。クリップ（ソースの %.3f 秒から）の中の "
                  "replaced_spans_sec（クリップ内の秒）だけ差し替え、他は元のサンプル" % (off / sr)
                  if full_source else
                  "クリップの長さ（ソースの %.3f 秒から = start_sec）。replaced_spans_sec"
                  "（クリップ内の秒）の外は元のサンプル" % (off / sr))),
    }
