# -*- coding: utf-8 -*-
"""DAW 連携 段階 1（issue #4）: 書き出しの往復。

1. BWF の `bext`（TimeReference）/ `iXML` を元から書き写す。あり・なし・クリップ（offset ぶん進める）。
2. 書き出しの既定の置き場は元ファイルの隣の `<名前>_ve.wav`。既存のファイルは上書きしない。
3. どちらでも「元と同じ長さ・非編集区間はサンプル単位で同じ」は崩さない。

素材の WAV に bext / iXML を足すのは、エンジンの `bwf.py` を使わずにこのファイルの中で組む
（書く側と読む側が同じ間違いをしても通ってしまわないように）。bext は `fmt ` より前に置く
（Logic などの並び。エンジンは `data` の直前に書くので、並びが違っても読めることも見る）。
"""
import os
import shutil
import struct

import numpy as np
import pytest
import soundfile as sf

from conftest import TAKE, needs_clips, needs_model

TREF = 4_771_683                      # Studio One の録音で見た値（44.1 kHz で 108 秒）
IXML = ('<?xml version="1.0" encoding="UTF-8"?>\r\n<BWFXML>\r\n\t<BEXT>\r\n'
        '\t\t<BWF_TIME_REFERENCE_HIGH>0</BWF_TIME_REFERENCE_HIGH>\r\n'
        '\t\t<BWF_TIME_REFERENCE_LOW>%d</BWF_TIME_REFERENCE_LOW>\r\n\t</BEXT>\r\n'
        '\t<PRESONUS><TEMPO_MAP><TEMPO_SEGMENT><TEMPO_SEGMENT_OFFSET>0</TEMPO_SEGMENT_OFFSET>'
        '</TEMPO_SEGMENT></TEMPO_MAP></PRESONUS>\r\n</BWFXML>\r\n')


# ------------------------------------------------------------------ RIFF を手で読む・組む
def _chunks(path):
    b = open(path, "rb").read()
    assert b[:4] == b"RIFF" and b[8:12] == b"WAVE"
    assert struct.unpack("<I", b[4:8])[0] == len(b) - 8          # RIFF の大きさが合っている
    out, i = [], 12
    while i + 8 <= len(b):
        cid, n = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        out.append((cid.decode("latin1"), b[i + 8:i + 8 + n]))
        i += 8 + n + (n & 1)
    assert i == len(b)
    return out


def _chunk(chs, cid):
    return next((body for c, body in chs if c == cid), None)


def _bext(tref, coding=b"A=PCM,F=48000,W=24,M=mono,T=test\r\n"):
    b = bytearray(602)
    b[0:11] = b"vocal take1"                                   # Description
    b[256:265] = b"Studio 1 "                                  # Originator
    struct.pack_into("<Q", b, 338, tref)                       # TimeReference
    struct.pack_into("<H", b, 346, 1)                          # Version
    return bytes(b) + coding


def _tref(bext):
    return struct.unpack_from("<Q", bext, 338)[0]


def _with_bwf(src, dst, bext=None, ixml=None):
    """src の WAV に bext（`fmt ` の前）と iXML（末尾）を足して dst に書く。"""
    body = b""
    if bext is not None:
        body += b"bext" + struct.pack("<I", len(bext)) + bext + (b"\0" if len(bext) & 1 else b"")
    for cid, data in _chunks(src):
        body += cid.encode("latin1") + struct.pack("<I", len(data)) + data
        body += b"\0" if len(data) & 1 else b""
    if ixml is not None:
        body += b"iXML" + struct.pack("<I", len(ixml)) + ixml + (b"\0" if len(ixml) & 1 else b"")
    with open(dst, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body)
    return dst


def _ints(path):
    return sf.read(path, dtype="int32", always_2d=True)


def _mask(n, sr, spans):
    m = np.ones(n, bool)
    for a, b in spans:
        m[int(round(a * sr)):int(round(b * sr))] = False
    return m


def _shift_first_note(p, cents=100.0):
    from vocal_engine.project.model import Target
    n = next(n for n in p.take_notes if n.kind == "note" and n.start_sec > 0.4)
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note(n.id),
                    "params": {"cents": cents}}], author="human")


# ------------------------------------------------------------------ bwf.py 単体（解析なし）
def test_bwf_roundtrip_on_plain_wav(tmp_path):
    """素の WAV に bext / iXML を入れて読み戻す。サンプルは変わらない。"""
    from vocal_engine import bwf
    x = (np.sin(np.arange(4801) / 7.0) * 20000).astype("int16")          # 奇数サンプル数
    path = str(tmp_path / "a.wav")
    sf.write(path, x, 48000, subtype="PCM_16")
    assert bwf.read_meta(path).bext is None
    ixml = (IXML % TREF).encode("utf-8") + b"x"                           # 奇数バイト（詰め物が要る）
    r = bwf.write_meta(path, bwf.BwfMeta(bext=_bext(TREF), ixml=ixml))
    assert r["bext"] and r["ixml"] and r["time_reference"] == TREF and r["skipped"] is None
    chs = _chunks(path)
    assert [c for c, _ in chs].index("bext") < [c for c, _ in chs].index("data")
    assert _chunk(chs, "bext") == _bext(TREF) and _chunk(chs, "iXML") == ixml
    m = bwf.read_meta(path)
    assert m.time_reference == TREF and m.ixml == ixml
    y, sr = sf.read(path, dtype="int16")
    assert sr == 48000 and np.array_equal(x, y)
    # もう一度書いても bext / iXML は 1 つずつ（重ならない）
    bwf.write_meta(path, m)
    assert [c for c, _ in _chunks(path)].count("bext") == 1
    assert [c for c, _ in _chunks(path)].count("iXML") == 1


def test_bwf_shift_and_minimal(tmp_path):
    from vocal_engine import bwf
    b = bwf.shift_bext(_bext(TREF), 48000)
    assert _tref(b) == TREF + 48000 and b[:338] == _bext(TREF)[:338] and b[346:] == _bext(TREF)[346:]
    # iXML: 下位 32 bit があふれたら上位に繰り上がる。ほかの要素（テンポマップ）はそのまま
    x = (IXML % 0xFFFFFFF0).encode("utf-8")
    y = bwf.shift_ixml(x, 0x20).decode("utf-8")
    assert "<BWF_TIME_REFERENCE_LOW>16</BWF_TIME_REFERENCE_LOW>" in y
    assert "<BWF_TIME_REFERENCE_HIGH>1</BWF_TIME_REFERENCE_HIGH>" in y
    assert "<TEMPO_SEGMENT_OFFSET>0</TEMPO_SEGMENT_OFFSET>" in y
    z = b"<BWFXML><SPEED><TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>0</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>" \
        b"<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>100</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO></SPEED></BWFXML>"
    assert b"_LO>150<" in bwf.shift_ixml(z, 50)
    assert bwf.shift_ixml(b"\xff\xfe not utf8", 10) == b"\xff\xfe not utf8"
    assert _tref(bwf.minimal_bext(123)) == 123 and len(bwf.minimal_bext(0)) == 602
    assert bwf.minimal_bext(0)[256:288].rstrip(b"\0") == b"Gliss"      # Originator はアプリ名（issue #29）


def test_bwf_skips_rf64_and_plain_without_meta(tmp_path):
    from vocal_engine import bwf
    x = np.zeros(1000, "float32")
    rf = str(tmp_path / "a.wav")
    sf.write(rf, x, 48000, subtype="FLOAT", format="RF64")
    before = open(rf, "rb").read()
    r = bwf.write_meta(rf, bwf.BwfMeta(bext=_bext(TREF)))
    assert r["skipped"] and not r["bext"] and open(rf, "rb").read() == before
    plain = str(tmp_path / "b.wav")
    sf.write(plain, x, 48000, subtype="PCM_24")
    before = open(plain, "rb").read()
    r = bwf.write_meta(plain, bwf.BwfMeta())                             # 元に何も無い
    assert r == {"bext": False, "ixml": False, "time_reference": None, "skipped": None,
                 "warnings": []}
    assert open(plain, "rb").read() == before
    assert bwf.read_meta(str(tmp_path / "nothing.wav")).bext is None      # 無いファイル


# ------------------------------------------------------------------ 書き出し（解析が要る）
@pytest.fixture
def bwf_take(tmp_path):
    """bext（TimeReference = TREF）と iXML の付いたテイクを tmp の「DAW の Media フォルダ」に置く。"""
    d = tmp_path / "Media"
    d.mkdir()
    return _with_bwf(TAKE, str(d / "vo.wav"), bext=_bext(TREF),
                     ixml=(IXML % TREF).encode("utf-8"))


@needs_clips
@needs_model
def test_export_keeps_bext_ixml_and_samples(bwf_take, tmp_path):
    from vocal_engine import bwf
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    p = Project.open(bwf_take, project_dir=str(tmp_path / "proj"))
    _shift_first_note(p)
    r = export_wav(p)                                             # 既定の置き場
    assert r["path"] == os.path.join(os.path.dirname(bwf_take), "vo_ve.wav")
    assert r["bwf"]["bext"] and r["bwf"]["ixml"] and r["bwf"]["time_reference"] == TREF
    assert r["bwf"]["source_time_reference"] == TREF
    assert r["bwf"]["time_reference_sec"] == pytest.approx(TREF / r["sr"])
    chs = _chunks(r["path"])
    assert _chunk(chs, "bext") == _bext(TREF)                     # bext はバイト単位でそのまま
    assert _chunk(chs, "iXML") == (IXML % TREF).encode("utf-8")
    src, sr = _ints(bwf_take)
    y, _ = _ints(r["path"])
    assert y.shape == src.shape and all(r["same_as_source"].values()) and r["replaced_spans_sec"]
    m = _mask(len(src), sr, r["replaced_spans_sec"])
    assert np.array_equal(y[m], src[m]) and not np.array_equal(y, src)
    # bext 付きの元から、bext の無い元と同じ音が出る（メタデータで音が変わらない）
    q = Project.open(TAKE, project_dir=str(tmp_path / "plain"))
    _shift_first_note(q)
    r0 = export_wav(q, path=str(tmp_path / "plain.wav"))
    assert np.array_equal(_ints(r0["path"])[0], y)
    assert bwf.read_meta(r0["path"]).bext is None and r0["bwf"]["bext"] is False


@needs_clips
@needs_model
def test_export_without_bext_is_unchanged(tmp_path):
    """元に bext が無ければ、書き出しにも足さない（段階 0 までと同じバイト列）。"""
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    take = shutil.copy(TAKE, str(tmp_path / "vo.wav"))
    p = Project.open(take, project_dir=str(tmp_path / "proj"))
    r = export_wav(p)
    assert r["path"] == str(tmp_path / "vo_ve.wav")
    assert r["bwf"] == {"bext": False, "ixml": False, "time_reference": None,
                        "time_reference_sec": None, "source_time_reference": None}
    assert not {"bext", "iXML"} & {c for c, _ in _chunks(r["path"])}
    x, sr = _ints(take)
    y, _ = _ints(r["path"])
    assert np.array_equal(x, y)
    # 同じ中身を soundfile で書いたものとバイト単位で同じ（BWF の処理が何も足していない）
    ref = str(tmp_path / "ref.wav")
    sf.write(ref, x, sr, subtype=sf.info(take).subtype, format=sf.info(take).format)
    assert open(ref, "rb").read() == open(r["path"], "rb").read()


@needs_clips
@needs_model
def test_default_path_never_overwrites(bwf_take, tmp_path):
    from vocal_engine.project import Project
    from vocal_engine.render.export import ExportError, default_path, export_wav
    d = os.path.dirname(bwf_take)
    p = Project.open(bwf_take, project_dir=str(tmp_path / "proj"))
    _shift_first_note(p)
    a = export_wav(p)["path"]
    first = open(a, "rb").read()
    b = export_wav(p)["path"]
    c = export_wav(p)["path"]
    assert [os.path.basename(x) for x in (a, b, c)] == ["vo_ve.wav", "vo_ve(2).wav", "vo_ve(3).wav"]
    assert open(a, "rb").read() == first                          # 前のファイルは触らない
    assert default_path(p) == os.path.join(d, "vo_ve(4).wav")
    assert not [f for f in os.listdir(d) if "tmp" in f]           # 一時ファイルが残らない
    # 書き出したものを開き直して書き出すと `_ve` を重ねない
    q = Project.open(a, project_dir=str(tmp_path / "proj2"))
    assert default_path(q) == os.path.join(d, "vo_ve(4).wav")
    # 元の音声そのものへは書かない
    with pytest.raises(ExportError):
        export_wav(p, path=bwf_take)
    # path を明示したとき（画面の「名前を付けて書き出し」。上書きの確認はダイアログが出す）は上書きする
    assert export_wav(p, path=b)["path"] == b


@needs_clips
@needs_model
def test_default_path_for_samples_goes_to_project(tmp_path):
    """サンプル列（実体はプロジェクトの sources/）は隣ではなく <project>/export/。"""
    from vocal_engine.media import Samples
    from vocal_engine.project import Project
    from vocal_engine.render.export import default_path
    x, sr = sf.read(TAKE, dtype="float32")
    p = Project.open(Samples(x, sr, "host-src"), project_dir=str(tmp_path / "proj"))
    d = default_path(p)
    assert os.path.dirname(d) == os.path.join(p.dir, "export") and d.endswith("_ve.wav")


OFF_SEC, LEN_SEC = 0.5, 2.5


@needs_clips
@needs_model
def test_clip_export_advances_time_reference(bwf_take, tmp_path):
    """クリップ（ソースの途中から）をクリップの長さで書き出すと TimeReference = 元 + offset。
    ソースと同じ長さなら元のまま。"""
    from vocal_engine.media import Clip
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    p = Project.open(Clip(bwf_take, offset_sec=OFF_SEC, length_sec=LEN_SEC),
                     project_dir=str(tmp_path / "clip"))
    _shift_first_note(p)
    src, sr = _ints(bwf_take)
    off, n = int(OFF_SEC * sr), int(LEN_SEC * sr)

    r = export_wav(p)
    assert os.path.basename(r["path"]) == "vo_0.500-3.000_ve.wav"
    assert r["bwf"]["time_reference"] == TREF + off
    chs = _chunks(r["path"])
    bext = _chunk(chs, "bext")
    assert _tref(bext) == TREF + off and bext[:338] == _bext(TREF)[:338] \
        and bext[346:] == _bext(TREF)[346:]                       # TimeReference 以外はそのまま
    assert (IXML % (TREF + off)).encode("utf-8") == _chunk(chs, "iXML")
    y, _ = _ints(r["path"])
    assert y.shape[0] == n
    m = _mask(n, sr, r["replaced_spans_sec"])
    assert np.array_equal(y[m], src[off:off + n][m])

    r2 = export_wav(p, full_source=True)
    assert os.path.basename(r2["path"]) == "vo_0.500-3.000_ve(2).wav"
    assert r2["bwf"]["time_reference"] == TREF
    assert _chunk(_chunks(r2["path"]), "bext") == _bext(TREF)
    z, _ = _ints(r2["path"])
    assert np.array_equal(z[:off], src[:off]) and np.array_equal(z[off + n:], src[off + n:])
    assert np.array_equal(z[off:off + n], y)


@needs_clips
@needs_model
def test_clip_export_without_bext_gets_offset(tmp_path):
    """元に bext が無くても、クリップの長さで書き出すなら TimeReference = offset の bext を足す
    （元が 0:00 起点なら、そのクリップの位置）。ソースと同じ長さなら足さない。"""
    from vocal_engine.media import Clip
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    take = shutil.copy(TAKE, str(tmp_path / "vo.wav"))
    p = Project.open(Clip(take, offset_sec=OFF_SEC, length_sec=LEN_SEC),
                     project_dir=str(tmp_path / "clip"))
    sr = sf.info(take).samplerate
    r = export_wav(p)
    bext = _chunk(_chunks(r["path"]), "bext")
    assert bext is not None and _tref(bext) == int(OFF_SEC * sr)
    assert _chunk(_chunks(r["path"]), "iXML") is None
    r2 = export_wav(p, full_source=True)
    assert _chunk(_chunks(r2["path"]), "bext") is None


@needs_clips
@needs_model
def test_mcp_export_returns_bwf(bwf_take, tmp_path):
    from vocal_engine import mcp_server as m
    assert m.open_project(bwf_take, project_dir=str(tmp_path / "mcp"))["ok"]
    r = m.export_wav(background=False)
    assert r["ok"] and r["path"].endswith("vo_ve.wav") and r["bwf"]["time_reference"] == TREF
    v = m.export_view_data()
    import json
    data = json.load(open(v["path"], encoding="utf-8"))
    assert data["export_default_path"].endswith("vo_ve(2).wav")


# ------------------------------------------------------------------ セルフレビューで見つけたもの
@needs_clips
@needs_model
def test_broken_bext_does_not_break_export(tmp_path):
    """元の bext が短い（壊れている）と、クリップの書き出しが例外で止まっていた。
    今は壊れた bext は写さず警告にし、クリップなら TimeReference = offset の bext を足す。"""
    from vocal_engine.media import Clip
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    take = _with_bwf(TAKE, str(tmp_path / "vo.wav"), bext=b"short bext")
    p = Project.open(Clip(take, offset_sec=OFF_SEC, length_sec=LEN_SEC),
                     project_dir=str(tmp_path / "clip"))
    r = export_wav(p)
    assert any("壊れている" in w for w in r["warnings"])
    assert _tref(_chunk(_chunks(r["path"]), "bext")) == int(OFF_SEC * sf.info(take).samplerate)
    q = Project.open(take, project_dir=str(tmp_path / "whole"))
    r2 = export_wav(q)
    assert _chunk(_chunks(r2["path"]), "bext") is None and r2["bwf"]["bext"] is False
    assert np.array_equal(_ints(r2["path"])[0], _ints(take)[0])


@needs_clips
@needs_model
def test_flac_source_keeps_extension(tmp_path):
    """FLAC を開くと、中身は FLAC のまま名前だけ `_ve.wav` になっていた。拡張子を元に合わせる。"""
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    x, sr = sf.read(TAKE, dtype="int32")
    src = str(tmp_path / "vo.flac")
    sf.write(src, x, sr, subtype="PCM_24", format="FLAC")
    p = Project.open(src, project_dir=str(tmp_path / "proj"))
    r = export_wav(p)
    assert r["path"] == str(tmp_path / "vo_ve.flac") and sf.info(r["path"]).format == "FLAC"
    assert r["warnings"] == [] and r["bwf"]["bext"] is False


@needs_clips
@needs_model
def test_unwritable_dir_falls_back_to_project(bwf_take, tmp_path, monkeypatch):
    """元の場所に書けない（PermissionError）ときは <project>/export/ に書く（path を省いたときだけ）。"""
    import vocal_engine.render.export as ex
    from vocal_engine.project import Project
    p = Project.open(bwf_take, project_dir=str(tmp_path / "proj"))
    media = os.path.dirname(bwf_take)
    real = sf.write

    def deny(path, *a, **k):
        if os.path.dirname(os.path.abspath(path)) == media:
            raise PermissionError("read only")
        return real(path, *a, **k)

    monkeypatch.setattr(ex.sf, "write", deny)
    r = ex.export_wav(p)
    assert os.path.dirname(r["path"]) == os.path.join(p.dir, "export")
    assert os.path.basename(r["path"]) == "vo_ve.wav" and r["bwf"]["time_reference"] == TREF
    assert any("書けない" in w for w in r["warnings"])
    assert not [f for f in os.listdir(media) if f != "vo.wav"]      # 一時ファイルも残らない
    with pytest.raises(PermissionError):
        ex.export_wav(p, path=os.path.join(media, "named.wav"))     # 明示したときは切り替えない


@needs_clips
@needs_model
def test_default_path_race_does_not_overwrite(bwf_take, tmp_path, monkeypatch):
    """名前を決めてから書き終わるまでに同じ名前ができても上書きしない。"""
    from vocal_engine import bwf
    import vocal_engine.render.export as ex
    from vocal_engine.project import Project
    p = Project.open(bwf_take, project_dir=str(tmp_path / "proj"))
    target = os.path.join(os.path.dirname(bwf_take), "vo_ve.wav")
    real = bwf.write_meta

    def racing(path, *a, **k):
        with open(target, "wb") as f:          # 別の書き出しが先に同じ名前を取った
            f.write(b"other")
        return real(path, *a, **k)

    monkeypatch.setattr(ex.bwf, "write_meta", racing)
    r = ex.export_wav(p)
    assert r["path"].endswith("vo_ve(2).wav")
    assert open(target, "rb").read() == b"other"


@needs_clips
@needs_model
def test_same_path_is_rejected_before_rendering(bwf_take, tmp_path):
    """元と同じパス（大文字小文字違いも）は、再合成の前にエラー。元は書き換えない。"""
    from vocal_engine.project import Project
    from vocal_engine.render.export import ExportError, export_wav
    p = Project.open(bwf_take, project_dir=str(tmp_path / "proj"))
    _shift_first_note(p)
    before = open(bwf_take, "rb").read()
    for x in (bwf_take, bwf_take.upper()):
        with pytest.raises(ExportError):
            export_wav(p, path=x)
    assert open(bwf_take, "rb").read() == before
