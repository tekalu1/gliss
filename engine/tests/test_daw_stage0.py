# -*- coding: utf-8 -*-
"""DAW 連携 段階 0（issue #3。`docs/daw-stage0.md`）。

1. 入力の一般化: ソース（ファイル／サンプル列）＋ソース内オフセット＋ソース ID
   - ファイルを開く = オフセット 0・ファイル全体（段階 2 までと同じ）
   - クリップの音 = ソースの切り出し。解析の秒はクリップの頭が 0
   - サンプル列で渡しても、同じ中身のファイルと同じ結果
   - クリップの書き出し（クリップの長さ／ソースと同じ長さ）で、差し替えた外は元のサンプル
   - 段階 2 までの project.json（schema 1）を開ける
2. 区間 → PCM: 長さが変わらない・中身は export_wav と同じ・編集ごとの差分更新も同じ
3. 編集リストのアーカイブ（UI と無関係）: JSON で往復して、同じ編集・同じ音に戻る
（重みの環境変数の統一もここで見る）
"""
import json
import os
import shutil

import numpy as np
import pytest
import soundfile as sf

import materials as MAT
from conftest import GUIDE, TAKE, needs_clips, needs_model, stem

pytestmark = [needs_clips, needs_model]

OFF_SEC, LEN_SEC = 0.5, 2.5


def _mcp(p):
    from vocal_engine import mcp_server as m
    m._state["project"] = p
    m._invalidate_renderer()
    return m


def _ints(x, subtype="PCM_24"):
    """float（int32 を 2^31 で割った値）→ export と同じ整数（同じ書式で書いて読み戻す）。"""
    import tempfile
    from vocal_engine.render.export import _to_store
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        sf.write(path, _to_store(x, "int32"), 48000, subtype=subtype)
        return sf.read(path, dtype="int32", always_2d=True)[0]
    finally:
        os.remove(path)


def _src_frames():
    return sf.info(TAKE).frames


# ================================================================ 1. 入力の一般化
def test_opening_a_file_is_the_whole_clip(tmp_path):
    from vocal_engine.project import Project
    p = Project.open(TAKE, project_dir=str(tmp_path / "p"))
    t = p.take
    assert t["offset_frames"] == 0 and t["offset_sec"] == 0.0
    assert t["frames"] == t["source_frames"] == _src_frames()
    assert t["source_kind"] == "file" and t["source_id"] == "sha256:" + t["sha256"][:16]
    assert t["source_name"] == os.path.basename(TAKE)


def test_default_project_dir_keeps_the_old_name_for_whole_files(tmp_path, monkeypatch):
    from vocal_engine import media as M
    from vocal_engine.audio import sha256_file
    from vocal_engine.project import store
    monkeypatch.setattr(store, "PROJECTS_ROOT", str(tmp_path))
    sha = sha256_file(TAKE)
    whole = store._default_project_dir(M.as_clip(TAKE), sha)
    assert os.path.basename(whole) == "%s-%s" % (stem(TAKE), sha[:8])          # 段階 2 までと同じ
    part = store._default_project_dir(M.Clip(TAKE, offset_sec=OFF_SEC, length_sec=LEN_SEC), sha)
    assert os.path.basename(part) == "%s-%s-o24000-n120000" % (stem(TAKE), sha[:8])


def test_clip_audio_is_the_source_slice_and_times_start_at_the_clip(tmp_path):
    from vocal_engine.audio import read_mono
    from vocal_engine.media import Clip
    from vocal_engine.project import Project
    whole = Project.open(TAKE, project_dir=str(tmp_path / "whole"))
    clip = Project.open(Clip(TAKE, offset_sec=OFF_SEC, length_sec=LEN_SEC, source_id="src-1"),
                        project_dir=str(tmp_path / "clip"))
    x, sr = read_mono(TAKE)
    off, n = int(OFF_SEC * sr), int(LEN_SEC * sr)
    cx, csr = clip.audio("take")
    assert csr == sr and len(cx) == n and np.array_equal(cx, x[off:off + n])
    assert clip.take["offset_frames"] == off and clip.take["frames"] == n
    assert clip.take["source_frames"] == len(x) and clip.take["source_id"] == "src-1"
    assert abs(clip.duration_sec - LEN_SEC) < 1e-9 and clip.source_sec(1.0) == OFF_SEC + 1.0

    # 解析の秒はクリップの頭が 0。ファイル全体の解析を OFF_SEC ずらしたものとほぼ同じ
    # （RMVPE は 10 ms ホップで、0.5 s はちょうど 50 フレーム。端のフレームだけ違ってよい）
    wf, cf = whole.take_f0, clip.take_f0
    k = int(round(OFF_SEC / wf.hop_s))
    m = min(cf.n_frames, wf.n_frames - k)
    inner = slice(10, m - 10)
    a, b = wf.f0[k:k + m][inner], cf.f0[:m][inner]
    both = (a > 0) & (b > 0)
    assert np.mean((a > 0) == (b > 0)) > 0.95
    cents = 1200 * np.abs(np.log2(a[both] / b[both]))
    assert np.median(cents) < 5.0
    # ノートもクリップの時間で並ぶ（ファイル全体のノートを OFF_SEC 引いた位置の近く）
    starts = [n.start_sec for n in whole.take_notes if n.kind == "note"
              and OFF_SEC + 0.1 < n.start_sec < OFF_SEC + LEN_SEC - 0.3]
    cstarts = [n.start_sec for n in clip.take_notes if n.kind == "note"]
    for s in starts:
        assert min(abs(s - OFF_SEC - c) for c in cstarts) < 0.03, s


def test_samples_source_gives_the_same_result_as_the_file(tmp_path):
    from vocal_engine.media import Samples
    from vocal_engine.project import Project
    whole = Project.open(TAKE, project_dir=str(tmp_path / "file"))
    a, sr = sf.read(TAKE, dtype="float32", always_2d=True)      # PCM_24 は float32 に丸めずに入る
    p = Project.open(Samples(a, sr, source_id="ara-audio-source-7", name="take.wav"),
                     project_dir=str(tmp_path / "samples"))
    assert p.take["source_kind"] == "samples" and p.take["source_id"] == "ara-audio-source-7"
    assert p.take["source_name"] == "take.wav"
    assert p.take["path"].startswith(os.path.join(p.dir, "sources"))
    assert sf.info(p.take["path"]).subtype == "FLOAT"
    assert np.array_equal(p.audio("take")[0], whole.audio("take")[0])
    assert [n.to_json() for n in p.take_notes] == [n.to_json() for n in whole.take_notes]
    # 同じサンプル列で開き直すと、同じプロジェクト（編集リストを捨てない）
    from vocal_engine.project.model import Target
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note("n005"),
                    "params": {"cents": 50}}], author="human")
    p2 = Project.open(Samples(a, sr, source_id="ara-audio-source-7"),
                      project_dir=str(tmp_path / "samples"))
    assert len(p2.edits) == 1


def test_clip_out_of_range_is_an_error(tmp_path):
    from vocal_engine.media import Clip
    from vocal_engine.project import Project, ProjectError
    with pytest.raises(ProjectError):
        Project.open(Clip(TAKE, offset_sec=3.5, length_sec=1.0), project_dir=str(tmp_path / "x"))
    with pytest.raises(ProjectError):
        Project.open(Clip(TAKE, offset_sec=-0.1), project_dir=str(tmp_path / "y"))


def test_clip_export_clip_length_and_full_source(tmp_path):
    from vocal_engine.media import Clip
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    p = Project.open(Clip(TAKE, offset_sec=OFF_SEC, length_sec=LEN_SEC),
                     project_dir=str(tmp_path / "clip"))
    m = _mcp(p)
    nid = next(n.id for n in p.take_notes if n.kind == "note" and n.start_sec > 0.6)
    assert m.shift_pitch(100.0, note_id=nid, author="human")["ok"]
    src, sr = sf.read(TAKE, dtype="int32", always_2d=True)
    off, n = int(OFF_SEC * sr), int(LEN_SEC * sr)

    r = export_wav(p, path=str(tmp_path / "clip.wav"))
    y, _ = sf.read(r["path"], dtype="int32", always_2d=True)
    assert r["frames"] == n and y.shape == (n, src.shape[1]) and r["full_source"] is False
    assert r["start_sec"] == pytest.approx(OFF_SEC) and r["source_offset_sec"] == pytest.approx(OFF_SEC)
    assert all(r["same_as_source"].values()) and r["replaced_spans_sec"]
    mask = np.ones(n, bool)
    for a, b in r["replaced_spans_sec"]:
        mask[int(round(a * sr)):int(round(b * sr))] = False
    assert np.array_equal(y[mask], src[off:off + n][mask])
    assert not np.array_equal(y, src[off:off + n])

    r2 = export_wav(p, path=str(tmp_path / "full.wav"), full_source=True)
    z, _ = sf.read(r2["path"], dtype="int32", always_2d=True)
    assert r2["frames"] == len(src) and r2["start_sec"] == 0.0 and r2["full_source"] is True
    assert all(r2["same_as_source"].values())
    assert np.array_equal(z[:off], src[:off]) and np.array_equal(z[off + n:], src[off + n:])
    assert np.array_equal(z[off:off + n], y)            # クリップの中はクリップの書き出しと同じ


def test_mcp_open_project_with_offset(tmp_path):
    from vocal_engine import mcp_server as m
    r = m.open_project(TAKE, project_dir=str(tmp_path / "mcp"), offset_sec=OFF_SEC,
                       length_sec=LEN_SEC, source_id="host-src")
    assert r["ok"], r
    assert r["take"]["offset_sec"] == pytest.approx(OFF_SEC)
    assert r["take"]["duration_sec"] == pytest.approx(LEN_SEC)
    assert r["take"]["source_id"] == "host-src"
    assert m.analyze_take(background=False)["ok"]
    rr = m.render_region(0.2, 1.2, channels="all", path=str(tmp_path / "r.wav"))
    assert rr["ok"], rr
    assert rr["frames"] == int(round(1.0 * rr["sr"]))
    assert rr["source_start_sec"] == pytest.approx(OFF_SEC + 0.2)
    assert sf.info(rr["path"]).frames == rr["frames"]


def test_schema1_project_json_still_opens(tmp_path):
    """段階 2 までの project.json（take にソースのキーが無い・schema 1）を開ける。"""
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    d = str(tmp_path / "old")
    p = Project.open(TAKE, GUIDE, project_dir=d)
    p.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.6, 0.9),
                    "params": {"cents": 100}}], author="human")
    with open(p.json_path, encoding="utf-8") as f:
        j = json.load(f)
    j["schema_version"] = 1
    for role in ("take", "guide"):
        for k in ("source_id", "source_kind", "source_name", "source_frames",
                  "source_duration_sec", "offset_frames", "offset_sec"):
            j[role].pop(k)
    with open(p.json_path, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)
    q = Project(d).load()
    assert q.take["offset_frames"] == 0 and q.take["source_frames"] == q.take["frames"]
    assert q.take["source_id"] == "sha256:" + q.take["sha256"][:16]
    q2 = Project.open(TAKE, GUIDE, project_dir=d)                    # 使い回す（編集が残る）
    assert len(q2.edits) == 1
    assert np.array_equal(q2.audio("take")[0], p.audio("take")[0])


def test_adding_or_replacing_the_guide_keeps_the_edits(tmp_path):
    """同じテイクにガイドを後から開く／別のガイドに差し替えると、編集リストと歌詞は残り、
    ガイドの解析だけ作り直す（段階 2 までは作り直していて、画面の「ガイドを開く」で編集が消えた）。"""
    from conftest import CLIP_A
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    d = str(tmp_path / "p")
    p = Project.open(TAKE, project_dir=d, lyrics=MAT.text("C.lyrics"))
    p.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.6, 0.9),
                    "params": {"cents": 100}}], author="human")
    q = Project.open(TAKE, GUIDE, project_dir=d)
    assert len(q.edits) == 1 and q.guide["sha256"] and q.has_lyrics("take")
    q.analyze()
    assert q.alignment is not None and os.path.exists(os.path.join(d, "cache", "alignment.json"))
    before = q.alignment.to_json()
    r = Project.open(TAKE, CLIP_A, project_dir=d)                    # 別のガイドに差し替え
    assert len(r.edits) == 1 and r.guide["path"] == os.path.abspath(CLIP_A)
    assert not os.path.exists(os.path.join(d, "cache", "alignment.json"))   # 前のガイドの DTW は捨てる
    r.analyze()
    assert r.alignment.to_json() != before
    assert len(Project.open(TAKE, project_dir=d).edits) == 1       # ガイドを省いて開いても残る


def test_models_dir_env_is_unified(monkeypatch):
    from vocal_engine import config
    monkeypatch.delenv("VOCAL_ENGINE_MODELS_DIR", raising=False)
    monkeypatch.setenv("VOCAL_ENGINE_MODELS", r"C:\legacy")
    assert config.models_dir() == os.path.normpath(r"C:\legacy")     # 旧名も読む
    monkeypatch.setenv("VOCAL_ENGINE_MODELS_DIR", r"C:\new")
    assert config.models_dir() == os.path.normpath(r"C:\new")        # 新しい名前が優先
    monkeypatch.delenv("VOCAL_ENGINE_MODELS_DIR")
    monkeypatch.delenv("VOCAL_ENGINE_MODELS")
    assert config.models_dir() == os.path.join(config.user_data_dir(), "models")


# ================================================================ 2. 区間 → PCM
def _edit_mix(p):
    """いろいろな編集（ピッチ・鉛筆・分割・伸縮・つなぎ・undo）を順に当てる。各段で yield。"""
    m = _mcp(p)
    assert m.shift_pitch(100.0, note_id="n005", author="human")["ok"]
    yield "shift"
    n = p.note("n009")
    ts = np.arange(n.start_sec + 0.03, n.end_sec - 0.03, 0.01)
    base = n.pitch_midi
    assert m.set_pitch_curve(points=[[float(t), float(base + 0.5)] for t in ts], mode="draw",
                             author="human")["ok"]
    yield "draw"
    n6 = p.note("n006")
    assert m.split_note(sec=round(0.5 * (n6.start_sec + n6.end_sec), 3), author="human")["ok"]
    yield "split"
    assert m.stretch(1.2, note_id="n007", author="human")["ok"]
    yield "stretch"
    assert m.set_transition(0.0, note_a="n004", note_b="n005", author="human")["ok"]
    yield "transition"
    assert m.undo()["ok"]
    yield "undo"


def test_render_region_has_the_same_length_and_matches_export(tmp_path):
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    from vocal_engine.render.region import RegionRenderer, render_region
    p = Project.open(TAKE, project_dir=str(tmp_path / "p"))
    for _ in _edit_mix(p):
        pass
    r = export_wav(p, path=str(tmp_path / "e.wav"))
    ex, sr = sf.read(r["path"], dtype="int32", always_2d=True)
    rr = RegionRenderer.for_project(p, channels="all")
    y, info = render_region(p, renderer=rr)                         # 全体
    assert y.shape == ex.shape and np.array_equal(_ints(y), ex)
    for a, b in ((0.2, 1.1), (1.37, 2.9), (0.0, 0.3)):             # 窓の途中で切っても同じ
        y, info = render_region(p, a, b, renderer=rr)
        ia, ib = int(round(a * sr)), int(round(b * sr))
        assert info["frames"] == ib - ia == len(y)
        assert np.array_equal(_ints(y), ex[ia:ib]), (a, b)


def test_edit_cache_updates_only_the_changed_windows_and_matches_export(tmp_path):
    from vocal_engine.project import Project
    from vocal_engine.render.export import export_wav
    from vocal_engine.render.region import EditCache
    p = Project.open(TAKE, project_dir=str(tmp_path / "p"))
    cache = EditCache(p, channels="all")
    cache.prepare()
    assert cache.update()["dirty_sec"] == 0.0                       # 編集なし: 何もしない
    for i, step in enumerate(_edit_mix(p)):
        u = cache.update()
        if step == "split":
            assert u["dirty_sec"] == 0.0                              # 分割だけでは音は変わらない
        else:
            assert u["dirty_sec"] > 0, step
        assert u["dirty_sec"] < p.duration_sec, step                 # 全体を作り直していない
        r = export_wav(p, path=str(tmp_path / ("e%d.wav" % i)))
        ex, _ = sf.read(r["path"], dtype="int32", always_2d=True)
        assert np.array_equal(_ints(cache.pcm), ex), step


# ================================================================ 3. アーカイブ
FORBIDDEN = ("dir", "analysis", "updated_at", "view", "selection", "zoom", "tool")


def _walk_keys(d, out):
    if isinstance(d, dict):
        for k, v in d.items():
            out.add(k)
            _walk_keys(v, out)
    elif isinstance(d, list):
        for v in d:
            _walk_keys(v, out)
    return out


def test_archive_roundtrip_restores_the_same_edits_and_sound(tmp_path):
    from vocal_engine.media import Clip, Samples
    from vocal_engine.project import Project
    from vocal_engine.project.pitch import layered_segments
    from vocal_engine.render.region import render_region
    p = Project.open(Clip(TAKE, offset_sec=0.0, length_sec=3.8, source_id="src-A"), GUIDE,
                     project_dir=str(tmp_path / "orig"))
    for _ in _edit_mix(p):
        pass
    arc = p.to_archive()
    text = json.dumps(arc, ensure_ascii=False, sort_keys=True)
    assert len(text.encode("utf-8")) < 200_000
    keys = _walk_keys(arc, set())
    assert not (keys & set(FORBIDDEN)), keys & set(FORBIDDEN)       # UI・マシンの状態は入らない
    assert arc["take"]["source_id"] == "src-A" and arc["take"]["frames"] == int(3.8 * 48000)

    arc2 = json.loads(text)
    # 別のディレクトリ・同じ素材（パスで）
    q = Project.from_archive(arc2, take=TAKE, guide=GUIDE, project_dir=str(tmp_path / "restored"))
    assert [e.to_json() for e in q.edits] == [e.to_json() for e in p.edits]
    assert [c.to_json() for c in q.changesets] == [c.to_json() for c in p.changesets]
    assert q.take["source_id"] == "src-A" and q.take["frames"] == p.take["frames"]
    assert json.dumps(q.to_archive(), ensure_ascii=False, sort_keys=True) == \
        text.replace(json.dumps(p.take["path"]), json.dumps(q.take["path"]))
    assert [(s.start_sec, s.end_sec, s.cents, s.ratio, s.curve_points) for s in layered_segments(q)] \
        == [(s.start_sec, s.end_sec, s.cents, s.ratio, s.curve_points) for s in layered_segments(p)]
    y0, _ = render_region(p)
    y1, _ = render_region(q)
    assert np.array_equal(y0, y1)
    assert q.can_redo() == p.can_redo()                             # 取り消し履歴ごと戻る
    assert q.redo() and len(q.edits) == len(p.edits) + 1

    # サンプル列で（ARA ならホストのオーディオソース）。範囲はアーカイブのものを使う
    a, sr = sf.read(TAKE, dtype="float32", always_2d=True)
    s = Project.from_archive(arc2, take=Samples(a, sr), guide=GUIDE,
                             project_dir=str(tmp_path / "from-samples"))
    assert s.take["frames"] == p.take["frames"] and s.take["source_id"] == "src-A"
    assert [e.to_json() for e in s.edits] == [e.to_json() for e in p.edits]
    assert np.array_equal(render_region(s)[0], y0)


def test_archive_refuses_other_material(tmp_path):
    from conftest import CLIP_A
    from vocal_engine.project import Project, ProjectError
    p = Project.open(TAKE, project_dir=str(tmp_path / "p"))
    arc = p.to_archive()
    with pytest.raises(ProjectError):
        Project.from_archive(arc, take=CLIP_A, project_dir=str(tmp_path / "q"))
    with pytest.raises(ProjectError):
        Project.from_archive(dict(arc, format="something-else"), project_dir=str(tmp_path / "r"))


def test_archive_does_not_clobber_another_history(tmp_path):
    from vocal_engine.project import Project, ProjectError
    from vocal_engine.project.model import Target
    a = Project.open(TAKE, project_dir=str(tmp_path / "a"))
    a.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.6, 0.9),
                    "params": {"cents": 100}}], author="human")
    arc = a.to_archive()
    b = Project.open(TAKE, project_dir=str(tmp_path / "b"))           # 別の編集をしてある
    b.apply_edits([{"kind": "pitch_shift", "target": Target.range(1.0, 1.2),
                    "params": {"cents": -50}}], author="human")
    with pytest.raises(ProjectError):
        Project.from_archive(arc, take=TAKE, project_dir=b.dir)
    assert len(Project(b.dir).load().edits) == 1 and         Project(b.dir).load().edits[0].params["cents"] == -50
    q = Project.from_archive(arc, take=TAKE, project_dir=b.dir, overwrite=True)
    assert [e.to_json() for e in q.edits] == [e.to_json() for e in a.edits]
    # 同じ履歴（戻し直し）なら上書きの指定は要らない
    assert Project.from_archive(arc, take=TAKE, project_dir=a.dir).edits[0].params["cents"] == 100


def test_archive_of_an_offset_clip_and_range_mismatch(tmp_path):
    """オフセット ≠ 0 のクリップで往復する。同じファイルでも**別の範囲**を渡したらエラー。"""
    from vocal_engine.media import Clip
    from vocal_engine.project import Project, ProjectError
    from vocal_engine.render.region import render_region
    p = Project.open(Clip(TAKE, offset_sec=OFF_SEC, length_sec=LEN_SEC, source_id="src-B"),
                     project_dir=str(tmp_path / "a"))
    nid = next(n.id for n in p.take_notes if n.kind == "note" and n.start_sec > 0.3)
    assert _mcp(p).shift_pitch(100.0, note_id=nid, author="human")["ok"]
    arc = json.loads(json.dumps(p.to_archive()))
    q = Project.from_archive(arc, take=TAKE, project_dir=str(tmp_path / "b"))   # 範囲はアーカイブから
    assert q.take["offset_frames"] == p.take["offset_frames"] and q.take["frames"] == p.take["frames"]
    assert np.array_equal(render_region(q)[0], render_region(p)[0])
    with pytest.raises(ProjectError):
        Project.from_archive(arc, take=Clip(TAKE, offset_sec=0.2, length_sec=LEN_SEC),
                             project_dir=str(tmp_path / "c"))


def test_newer_schema_is_refused(tmp_path):
    from vocal_engine.project import Project, ProjectError
    p = Project.open(TAKE, project_dir=str(tmp_path / "p"))
    with open(p.json_path, encoding="utf-8") as f:
        j = json.load(f)
    j["schema_version"] = 99
    with open(p.json_path, "w", encoding="utf-8") as f:
        json.dump(j, f)
    with pytest.raises(ProjectError):
        Project(p.dir).load()


def test_samples_with_similar_ids_do_not_share_a_file(tmp_path):
    from vocal_engine.media import Samples
    from vocal_engine.project import Project
    a, sr = sf.read(TAKE, dtype="float32", always_2d=True)
    g, _ = sf.read(GUIDE, dtype="float32", always_2d=True)
    p = Project.open(Samples(a, sr, source_id="ara.src.1"), Samples(g, sr, source_id="ara-src-1"),
                     project_dir=str(tmp_path / "p"))
    assert p.take["path"] != p.guide["path"]
    assert np.array_equal(p.audio("take")[0], a[:, 0].astype("float64"))
    assert np.array_equal(p.audio("guide")[0], g[:, 0].astype("float64"))


def test_float_samples_reopen_keeps_the_edits(tmp_path):
    """float のサンプル列は書くたびに WAV の SHA-256 が変わる（PEAK チャンクの時刻）。
    ID を変えて渡し直しても、sources/ の WAV が消えていても、中身が同じなら編集リストを残す。"""
    import time
    from vocal_engine.media import Samples
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    a, sr = sf.read(TAKE, dtype="float32", always_2d=True)
    d = str(tmp_path / "p")
    p = Project.open(Samples(a, sr, source_id="x1"), project_dir=d)
    p.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.6, 0.9),
                    "params": {"cents": 100}}], author="human")
    time.sleep(1.1)
    q = Project.open(Samples(a, sr, source_id="x2"), project_dir=d)
    assert len(q.edits) == 1 and q.take["source_id"] == "x2"
    os.remove(q.take["path"])
    time.sleep(1.1)
    r = Project.open(Samples(a, sr, source_id="x2"), project_dir=d)
    assert len(r.edits) == 1 and os.path.exists(r.take["path"])
    assert np.array_equal(r.audio("take")[0], a[:, 0].astype("float64"))


def test_broken_project_json_is_kept_aside(tmp_path):
    from vocal_engine.project import Project
    from vocal_engine.project.model import Target
    p = Project.open(TAKE, project_dir=str(tmp_path / "p"))
    p.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.6, 0.9),
                    "params": {"cents": 100}}], author="human")
    with open(p.json_path, "rb") as f:
        raw = f.read()
    with open(p.json_path, "wb") as f:
        f.write(raw[:-5])
    q = Project.open(TAKE, project_dir=p.dir)
    assert len(q.edits) == 0
    baks = [x for x in os.listdir(p.dir) if x.startswith("project.json.broken-")]
    assert len(baks) == 1
    with open(os.path.join(p.dir, baks[0]), "rb") as f:
        assert f.read() == raw[:-5]


def test_samples_dtype_is_checked(tmp_path):
    from vocal_engine.media import Samples
    from vocal_engine.project import Project, ProjectError
    with pytest.raises(ProjectError):
        Project.open(Samples(np.zeros((48000, 1), dtype=np.int64), 48000),
                     project_dir=str(tmp_path / "p"))


def test_archive_with_other_material_does_not_touch_the_directory(tmp_path):
    """overwrite=True でも、素材が違えば既存のプロジェクトに触らない（照合を先にする）。"""
    from conftest import CLIP_A
    from vocal_engine.project import Project, ProjectError
    from vocal_engine.project.model import Target
    a = Project.open(TAKE, project_dir=str(tmp_path / "a"))
    arc = a.to_archive()
    b = Project.open(CLIP_A, project_dir=str(tmp_path / "b"))
    b.apply_edits([{"kind": "pitch_shift", "target": Target.range(0.6, 0.9),
                    "params": {"cents": 100}}], author="human")
    with pytest.raises(ProjectError):
        Project.from_archive(arc, take=CLIP_A, project_dir=b.dir, overwrite=True)
    again = Project(b.dir).load()
    assert len(again.edits) == 1 and again.take["path"] == os.path.abspath(CLIP_A)
