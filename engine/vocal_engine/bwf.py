# -*- coding: utf-8 -*-
"""BWF（Broadcast Wave）の `bext` / `iXML` を読んで、書き出した WAV に書き戻す。

DAW との往復（DAW 連携 段階 1。issue #4）で**書き出したファイルを DAW に戻したとき元の位置に揃う**
ようにするため。DAW は WAV の置き場所（タイムライン上の位置）を `bext` の **TimeReference**
（録音を始めた位置。ファイルのサンプルレートでの「サンプル数」。64 bit）に書き、
「元の位置に戻す」系の操作でそれを読む。soundfile（libsndfile）は `bext` / `iXML` を
書き写さないので、書き出しの後にここで RIFF のチャンクを足す。

- 読む: `read_meta(path)` → `BwfMeta`（`bext` と `iXML` の中身のバイト列。無ければ None）。
- 書く: `write_meta(path, meta, shift_frames=0)` → 書き出した WAV（RIFF/WAVE）の `data` の前に
  `bext`、末尾に `iXML` を入れる。`shift_frames` は TimeReference をずらすサンプル数
  （**クリップ＝ソースの途中から**を書き出すとき、クリップの頭はソースの頭より offset だけ後ろ）。
  iXML の中の時刻（`BWF_TIME_REFERENCE_LOW/HIGH`・`TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO/HI`）も同じだけずらす。
- 元に `bext` が無ければ何もしない（ファイルはそのまま＝段階 0 までと同じバイト列）。
  ただしクリップ（offset ≠ 0）の書き出しは、元に `bext` が無くても TimeReference = offset の
  最小の `bext` を足す（`minimal_bext`。元のファイルが 0:00 起点なら、そのクリップの位置になる）。
- `bext` / `iXML` 以外のチャンク（`acid`・`cue `・`LIST` など）は書き戻さない（`data` の長さや
  中身に依存するものがあり、書き出した音と食い違うことがあるため）。
- RF64 / W64 などの RIFF でない形式には書き戻さない（`write_meta` が理由を返す）。
"""
import os
import re
import struct
from dataclasses import dataclass

BEXT_TREF_OFFSET = 338       # Description 256 + Originator 32 + OriginatorReference 32 + Date 10 + Time 8
BEXT_MIN_SIZE = 602          # 固定部（CodingHistory の前まで）


class BwfError(RuntimeError):
    pass


@dataclass
class BwfMeta:
    bext: bytes = None
    ixml: bytes = None
    form: str = None           # "RIFF" / "RF64" / None（WAV でない）

    @property
    def time_reference(self):
        """bext の TimeReference（サンプル数）。bext が無ければ None。"""
        if not self.bext or len(self.bext) < BEXT_TREF_OFFSET + 8:
            return None
        return struct.unpack_from("<Q", self.bext, BEXT_TREF_OFFSET)[0]

    def summary(self, sr=None):
        t = self.time_reference
        out = {"bext": self.bext is not None, "ixml": self.ixml is not None,
               "time_reference": t}
        if t is not None and sr:
            out["time_reference_sec"] = round(t / float(sr), 6)
        return out


def _iter_chunks(f, size):
    """(id, 中身の位置, 中身の長さ) を順に。壊れたチャンクで止まる。"""
    f.seek(0)
    head = f.read(12)
    if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in (b"RIFF", b"RF64"):
        return None, []
    form = head[:4].decode("ascii")
    out = []
    ds64_data = None
    pos = 12
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(8)
        cid, n = h[:4], struct.unpack("<I", h[4:])[0]
        if cid == b"ds64" and n >= 24:
            ds64_data = struct.unpack("<Q", f.read(24)[8:16])[0]
        if cid == b"data" and n == 0xFFFFFFFF and ds64_data is not None:
            n = ds64_data
        if pos + 8 + n > size:
            n = size - pos - 8           # 最後のチャンクが切れている（書きかけのファイルなど）
            out.append((cid, pos + 8, n))
            break
        out.append((cid, pos + 8, n))
        pos += 8 + n + (n & 1)
    return form, out


def read_meta(path):
    """`bext` と `iXML` を読む。WAV でない・読めないときは中身の無い `BwfMeta`。"""
    meta = BwfMeta()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            form, chunks = _iter_chunks(f, size)
            meta.form = form
            for cid, at, n in chunks:
                if cid == b"bext" and meta.bext is None:
                    f.seek(at)
                    meta.bext = f.read(n)
                elif cid == b"iXML" and meta.ixml is None:
                    f.seek(at)
                    meta.ixml = f.read(n)
    except OSError:
        return BwfMeta()
    return meta


def minimal_bext(time_reference, originator=b"Gliss"):
    """TimeReference だけを持つ bext（v1、CodingHistory 無し）。"""
    b = bytearray(BEXT_MIN_SIZE)
    b[256:256 + len(originator)] = originator[:32]
    struct.pack_into("<Q", b, BEXT_TREF_OFFSET, int(time_reference))
    struct.pack_into("<H", b, 346, 1)          # Version 1
    return bytes(b)


def shift_bext(bext, shift_frames):
    """TimeReference を `shift_frames` だけ進めた bext。他のバイトはそのまま。"""
    if not shift_frames:
        return bext
    if len(bext) < BEXT_TREF_OFFSET + 8:
        raise BwfError("bext が短すぎる（%d バイト）" % len(bext))
    b = bytearray(bext)
    t = struct.unpack_from("<Q", b, BEXT_TREF_OFFSET)[0] + int(shift_frames)
    struct.pack_into("<Q", b, BEXT_TREF_OFFSET, max(0, t) & 0xFFFFFFFFFFFFFFFF)
    return bytes(b)


_PAIRS = (("BWF_TIME_REFERENCE_LOW", "BWF_TIME_REFERENCE_HIGH"),
          ("TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO", "TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI"),
          ("TIMESTAMP_SAMPLE_SINCE_MIDNIGHT_LO", "TIMESTAMP_SAMPLE_SINCE_MIDNIGHT_HI"))


def shift_ixml(ixml, shift_frames):
    """iXML の中の時刻（下位 32 bit・上位 32 bit の組）を `shift_frames` だけ進める。

    テキストとして該当する要素の数字だけを書き換え、他（PreSonus のテンポマップなど）は触らない。
    読めない（UTF-8 でない・組が片方だけ）ときは元のまま返す。
    """
    if not shift_frames or not ixml:
        return ixml
    try:
        text = ixml.decode("utf-8")
    except UnicodeDecodeError:
        return ixml
    for lo_tag, hi_tag in _PAIRS:
        lo_re = re.compile(r"(<%s>\s*)(\d+)(\s*</%s>)" % (lo_tag, lo_tag))
        hi_re = re.compile(r"(<%s>\s*)(\d+)(\s*</%s>)" % (hi_tag, hi_tag))
        lo_m = lo_re.search(text)
        if not lo_m:
            continue
        hi_m = hi_re.search(text)
        hi = int(hi_m.group(2)) if hi_m else 0
        v = max(0, ((hi << 32) | int(lo_m.group(2))) + int(shift_frames))
        text = lo_re.sub(lambda m: m.group(1) + str(v & 0xFFFFFFFF) + m.group(3), text, count=1)
        if hi_m:
            text = hi_re.sub(lambda m: m.group(1) + str(v >> 32) + m.group(3), text, count=1)
        elif v >> 32:
            return ixml            # 上位が要るのに要素が無い: 触らない
    return text.encode("utf-8")


def _chunk(cid, body):
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def write_meta(path, meta, shift_frames=0):
    """`path`（書き出したばかりの WAV）に `meta` の bext / iXML を入れる。

    返り値: {"bext": bool, "ixml": bool, "time_reference": int|None, "skipped": 理由|None,
            "warnings": [..]}（元の bext が壊れていれば写さずに warnings に書く）
    """
    bext = meta.bext if meta is not None else None
    ixml = meta.ixml if meta is not None else None
    notes = []
    if bext is not None and len(bext) < BEXT_TREF_OFFSET + 8:
        # 壊れた bext（TimeReference まで届かない）は写さない。書き出し自体は止めない
        notes.append("元の bext が壊れている（%d バイト）ので書き写していない" % len(bext))
        bext = None
    if bext is None and shift_frames:
        bext = minimal_bext(0)
    if bext is None and ixml is None:
        return {"bext": False, "ixml": False, "time_reference": None, "skipped": None,
                "warnings": notes}
    bext = shift_bext(bext, shift_frames) if bext is not None else None
    ixml = shift_ixml(ixml, shift_frames) if ixml is not None else None

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        form, chunks = _iter_chunks(f, size)
    if form != "RIFF":
        return {"bext": False, "ixml": False, "time_reference": None,
                "skipped": "RIFF の WAV でない（%s）ので bext / iXML を書き戻していない" % (form or "?"),
                "warnings": notes}
    body_len = 4                                               # "WAVE"
    keep = [(cid, at, n) for cid, at, n in chunks if cid not in (b"bext", b"iXML")]
    if not any(cid == b"data" for cid, _, _ in keep):
        raise BwfError("data チャンクが無い: %s" % path)
    extra = 0
    if bext is not None:
        extra += 8 + len(bext) + (len(bext) & 1)
    if ixml is not None:
        extra += 8 + len(ixml) + (len(ixml) & 1)
    for cid, at, n in keep:
        body_len += 8 + n + (n & 1)
    body_len += extra
    if body_len > 0xFFFFFFFF:
        return {"bext": False, "ixml": False, "time_reference": None,
                "skipped": "4 GB を超えるので bext / iXML を書き戻していない", "warnings": notes}

    tmp = path + ".bwf.tmp"
    try:
        _rewrite(path, tmp, keep, body_len, bext, ixml)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    t = struct.unpack_from("<Q", bext, BEXT_TREF_OFFSET)[0] if bext is not None else None
    return {"bext": bext is not None, "ixml": ixml is not None, "time_reference": t,
            "skipped": None, "warnings": notes}


def _rewrite(path, tmp, keep, body_len, bext, ixml):
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(b"RIFF" + struct.pack("<I", body_len) + b"WAVE")
        for cid, at, n in keep:
            if cid == b"data" and bext is not None:
                dst.write(_chunk(b"bext", bext))
            dst.write(cid + struct.pack("<I", n))
            src.seek(at)
            _copy(src, dst, n)
            if n & 1:
                dst.write(b"\0")
        if ixml is not None:
            dst.write(_chunk(b"iXML", ixml))


def _copy(src, dst, n, buf=1 << 20):
    left = n
    while left > 0:
        b = src.read(min(buf, left))
        if not b:
            break
        dst.write(b)
        left -= len(b)
    if left:
        raise BwfError("読み切れなかった（残り %d バイト）" % left)


# ---------------------------------------------------------------- テンポマップ（issue #18）
# Fender Studio Pro（Studio One）のミックスダウン・バウンスの iXML には PreSonus のテンポマップが入る:
#
#   <BWFXML><PRESONUS><TEMPO_MAP>
#     <TEMPO_SEGMENT><TEMPO_SEGMENT_OFFSET>0</…><TEMPO_SEGMENT_VALUE>0.3508794…</…></TEMPO_SEGMENT> …
#
# 手元の Studio One のソングの 14 本（2026-09-25）で確かめた読み方:
#   - OFFSET = ファイルの頭からのサンプル数（最後の OFFSET がファイルの長さの手前に来る。48 kHz で 12000 = 0.25 秒ごと）
#   - VALUE = 1 拍（4 分音符）の秒数（0.35088 → 60 / 0.35088 = 171.0 BPM）。一定のテンポでも 1e-8 程度ずつ揺れて並ぶ
#   - テンポが途中で変わる曲は、変わっていく区間だけ 0.25 秒ごとの段で並ぶ（175 → 176.4 … など）
# 拍子と 1 小節目の位置は入っていない。1 小節目はソングの 0:00（Studio One の既定）とみなし、
# bext の TimeReference（ファイルの頭のソング上の位置）から引いて求める。
_TEMPO_SEG_RE = re.compile(
    r"<TEMPO_SEGMENT>\s*<TEMPO_SEGMENT_OFFSET>\s*([-+0-9.eE]+)\s*</TEMPO_SEGMENT_OFFSET>\s*"
    r"<TEMPO_SEGMENT_VALUE>\s*([-+0-9.eE]+)\s*</TEMPO_SEGMENT_VALUE>\s*</TEMPO_SEGMENT>")


def _round_bpm(v):
    """表示と入力に使う BPM（0.01 刻み。整数から 0.01 以内なら整数）。"""
    r = round(float(v), 2)
    return float(round(r)) if abs(r - round(r)) <= 0.01 + 1e-9 else r


def parse_tempo_map(ixml):
    """iXML（バイト列か文字列）→ [(ファイルの頭からのサンプル数, BPM), ...]。テンポマップが無ければ []。"""
    if not ixml:
        return []
    text = ixml.decode("utf-8", "replace") if isinstance(ixml, (bytes, bytearray)) else str(ixml)
    if "TEMPO_MAP" not in text:
        return []
    out = []
    for off, val in _TEMPO_SEG_RE.findall(text):
        try:
            o, v = float(off), float(val)
        except ValueError:
            continue
        if v > 0 and 20.0 <= 60.0 / v <= 999.0:
            out.append((o, 60.0 / v))
    out.sort(key=lambda p: p[0])
    return out


def read_tempo(path):
    """WAV の iXML から、画面のグリッドに使う 1 つのテンポを読む。無ければ None。

    返り値: {"bpm": ファイルの頭のテンポ（0.01 刻み）, "bpm_range": [最小, 最大], "varies": 途中で変わるか,
            "time_reference_sec": bext の TimeReference（秒。無ければ None）, "segments": 段の数}
    テンポが途中で変わる曲も、グリッドは頭のテンポ 1 つで引く（テンポの変化には追従しない）。
    """
    meta = read_meta(path)
    segs = parse_tempo_map(meta.ixml)
    if not segs:
        return None
    try:
        import soundfile as sf
        sr = float(sf.info(path).samplerate)
    except Exception:                          # noqa: BLE001
        sr = None
    bpms = [b for _, b in segs]
    lo, hi = min(bpms), max(bpms)
    tr = meta.time_reference
    return {"bpm": _round_bpm(bpms[0]), "bpm_range": [_round_bpm(lo), _round_bpm(hi)],
            "varies": (hi - lo) > 0.05, "segments": len(segs),
            "time_reference_sec": (round(tr / sr, 6) if tr is not None and sr else None)}


__all__ = ["BwfMeta", "BwfError", "read_meta", "write_meta", "shift_bext", "shift_ixml",
           "minimal_bext", "parse_tempo_map", "read_tempo"]
