# -*- coding: utf-8 -*-
"""曲全体（158 秒）の実素材での確認。素材があるときだけ走る。

段階2 の完了の目安（1 フレーズを実制作で使える）を、実際のテイクで通す:

  (c) アウトロの 4 区間に歌詞を付けて区間ごとにアラインできる
  (d) 2 番目の区間を +1 半音、4 番目の区間を 60 ms 前へ → `export_wav` が
      **元と同じ長さ・開始位置**で、編集区間以外は 1 サンプルも違わない

素材（記号 SONG）は読み取り専用で使う。**書き込みは projects/ 配下だけ**。区間と歌詞は素材のフォルダの
materials.json（`SONG.lyrics`）、期待するかなは `SONG.kana`（どちらも素材が無ければ skip）。
"""
import os

import numpy as np
import pytest
import soundfile as sf

import materials as M
from conftest import ROOT, needs_model

SONG = M.clip("SONG")
PROJECT = os.path.join(ROOT, "projects", "_test-production-song")

# 4 区間（秒と歌詞）。区間の位置は素材に合わせたもの
LYRICS = M.data("SONG.lyrics", [])

pytestmark = [
    needs_model,
    pytest.mark.skipif(not M.available("SONG"),
                       reason="テスト素材 SONG（曲全体 158 秒）が無い（GLISS_TEST_MATERIALS）"),
]


@pytest.fixture(scope="module")
def song():
    """解析キャッシュを残す（2 回目からは数秒で開く。これも確認項目のうち）。"""
    from vocal_engine.project import Project
    p = Project.open(SONG, project_dir=PROJECT)        # ガイド無し（DTW を省く）
    p.ensure_analyzed()
    return p


def test_song_is_the_expected_material(song):
    assert song.take["sr"] == 48000
    assert song.take["channels"] == 1
    assert song.take["subtype"] == "PCM_24"
    assert abs(song.duration_sec - M.data("SONG.duration_sec")) < 0.01


def test_lyrics_align_per_range(song):
    """4 区間に歌詞を付ける。**区間の外には音素が付かない**。"""
    song.set_lyrics_entries(LYRICS, "take")
    res = song.analyze_phonemes("take", force=True)
    assert len(res.entries) == 4
    voiced = [p for p in res.phonemes if p.label != "silence"]
    assert len(voiced) > 80                             # 4 フレーズぶん
    for e in res.entries:
        assert e["n_phonemes"] > 0
        # アラインの結果は区間 ±200 ms（切り出しの余白）に収まっている
        assert e["aligned_start_sec"] >= e["start_sec"] - 0.21
        assert e["aligned_end_sec"] <= e["end_sec"] + 0.21
    # 区間の外は無音だけ（歌詞の無い 0〜141 秒に音素は立たない）
    lo = min(e["start_sec"] for e in LYRICS) - 0.25
    assert all(p.start_sec >= lo for p in voiced)
    assert any(p.label == "silence" and p.duration_sec > 100 for p in res.phonemes)
    # 音節は 4 区間ぶんが通し番号になっている
    assert [s["index"] for s in res.syllables] == list(range(len(res.syllables)))
    # かなは区間ごとに区切って持つ
    assert all(k in res.kana for k in M.data("SONG.kana"))


def test_edit_and_export_is_sample_exact_outside_the_edits(song, tmp_path):
    """+1 半音と 60 ms 前へ → 書き出し → **編集区間以外の差分が 0**。"""
    from vocal_engine.project.model import Target
    from vocal_engine.render.export import export_wav

    for c in song.changesets:
        c.undone = True
    song._replay()
    song.save()
    second, fourth = LYRICS[1], LYRICS[3]
    song.apply_edits([{"kind": "pitch_shift",
                       "target": Target.range(second["start_sec"], second["end_sec"]),
                       "params": {"cents": 100.0}}],
                     author="human", label="区間 2 を +1 半音")
    song.apply_edits([{"kind": "move", "target": Target.range(fourth["start_sec"], fourth["end_sec"]),
                       "params": {"ms": -60.0}}],
                     author="human", label="区間 4 を 60 ms 前へ")

    out = str(tmp_path / "song_edit.wav")
    r = export_wav(song, path=out)

    src, dst = sf.info(SONG), sf.info(out)
    assert (dst.samplerate, dst.channels, dst.subtype, dst.frames) == \
           (src.samplerate, src.channels, src.subtype, src.frames)
    assert r["start_sec"] == 0.0                        # 曲頭 0:00 起点のまま
    assert r["warnings"] == []

    a, sr = sf.read(SONG, dtype="int32", always_2d=True)
    b, _ = sf.read(out, dtype="int32", always_2d=True)
    mask = np.ones(len(a), dtype=bool)
    for s, e in r["replaced_spans_sec"]:
        mask[int(round(s * sr)):int(round(e * sr))] = False
    assert int((a[mask] != b[mask]).sum()) == 0         # ← 差分ゼロ
    assert int((a[~mask] != b[~mask]).sum()) > 0
    # 158 秒のうち差し替えたのは編集の近傍だけ
    assert r["replaced_sec"] < 8.0
    assert all(s > 140.0 for s, _ in r["replaced_spans_sec"])
    assert r["backend"] == "praat"                      # 既定は Praat（2026-09-23 から）


def test_praat_render_is_fast_on_the_whole_song(song):
    """Praat の下ごしらえ（158 秒全体の To Manipulation）と区間ごとの再合成が実用的な速さ。

    実測（Praat を既定にしたとき）: prepare 0.27 s、1.37 s の区間 0.013 s、158 s 全体 0.27 s。
    ここでは遅いマシン・並列実行でも落ちない程度の上限で見る。"""
    import time
    from vocal_engine.render.pipeline import Renderer, Segment
    x, sr = song.audio("take")
    f0r = song.take_f0
    t = time.perf_counter()
    r = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s)
    prep = time.perf_counter() - t
    assert r.backend_name == "praat"
    t = time.perf_counter()
    y, info = r.render_range(148.90, 150.96, [Segment(149.19, 150.56, cents=100.0)])
    one = time.perf_counter() - t
    assert len(y) == int(round(150.96 * sr)) - int(round(148.90 * sr))
    assert prep < 5.0, "prepare %.2f s" % prep
    assert one < 1.0, "1.37 秒の区間の再合成 %.2f s" % one
