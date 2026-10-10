# -*- coding: utf-8 -*-
"""歌詞を**区間ごと**に持つ（`phoneme/lyrics.py`）と、区間ごとのアラインメント。

曲全体のテイク（158 秒で歌うのはアウトロだけ）を扱うために入れた。
区間の外は音素を付けず `SP`（無音）で埋める、が守るべき約束。
"""
import pytest

import materials as M
from conftest import GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model]


# ---------------------------------------------------------------- データモデル
def test_normalize_accepts_string_and_array():
    from vocal_engine.phoneme import lyrics as LY
    assert LY.normalize("あいうえ") == [{"start_sec": None, "end_sec": None,
                                         "text": "あいうえ"}]
    assert LY.normalize("") == []
    assert LY.normalize(None) == []
    got = LY.normalize([{"start": 2.0, "end": 3.0, "text": "b"},
                        {"start_sec": 0.5, "end_sec": 1.5, "text": "a"}])
    assert [e["text"] for e in got] == ["a", "b"]       # 時間順に並ぶ
    assert got[0]["start_sec"] == 0.5


def test_normalize_rejects_overlap_and_mixing():
    from vocal_engine.phoneme import lyrics as LY
    with pytest.raises(LY.LyricsError):
        LY.normalize([{"start": 0.0, "end": 2.0, "text": "a"},
                      {"start": 1.0, "end": 3.0, "text": "b"}])
    with pytest.raises(LY.LyricsError):
        LY.normalize([{"start": 0.0, "end": 2.0, "text": "a"}, "全体"])
    with pytest.raises(LY.LyricsError):
        LY.normalize([{"start": 1.0, "text": "a"}])      # 片側だけ
    with pytest.raises(LY.LyricsError):
        LY.normalize([{"start": 1.0, "end": 1.01, "text": "a"}])   # 短すぎる
    assert LY.normalize([{"start": 0.0, "end": 2.0}]) == []        # text が空なら落ちる


def test_upsert_replaces_removes_and_adds():
    from vocal_engine.phoneme import lyrics as LY
    e = LY.normalize([{"start": 1.0, "end": 2.0, "text": "a"},
                      {"start": 3.0, "end": 4.0, "text": "b"}])
    # 重なる区間は消えて 1 件に置き換わる
    r = LY.upsert(e, "c", 1.5, 2.5)
    assert [(x["start_sec"], x["text"]) for x in r] == [(1.5, "c"), (3.0, "b")]
    # 空文字は削除
    r = LY.upsert(e, "", 0.9, 2.1)
    assert [x["text"] for x in r] == ["b"]
    # 範囲なしは全部を 1 件に置き換え
    assert LY.upsert(e, "z") == [{"start_sec": None, "end_sec": None, "text": "z"}]
    assert LY.upsert(e, "") == []
    # add は重なりを消さない（重なったらエラー）
    with pytest.raises(LY.LyricsError):
        LY.upsert(e, "c", 1.5, 2.5, mode="add")


def test_parse_text_file():
    from vocal_engine.phoneme import lyrics as LY
    r = LY.parse_text_file("# コメント\n144.30 148.77 さくら\n2:29.19 2:30.56 あいうえ\n")
    assert [(x["start_sec"], x["end_sec"], x["text"]) for x in r] == [
        (144.3, 148.77, "さくら"), (149.19, 150.56, "あいうえ")]
    assert LY.parse_text_file("あいうえ\nかき") == [
        {"start_sec": None, "end_sec": None, "text": "あいうえ かき"}]


# ---------------------------------------------------------------- プロジェクト
@pytest.fixture
def proj(tmp_path):
    from vocal_engine.project import Project
    p = Project.open(TAKE, project_dir=str(tmp_path / "proj"))
    p.ensure_analyzed()
    return p


# 素材 C は 3.84 秒。無音で 2 つのフレーズに割れている。歌詞は素材のフォルダの materials.json
R1 = (0.30, 1.60)      # 前半のフレーズ
R2 = (2.20, 3.52)      # 後半のフレーズ
P1, P2 = M.text("C.part1"), M.text("C.part2")
LYRICS = M.text("C.lyrics")             # P1 + " " + P2


def _ph(res):
    """音素・境界・音節（時刻・確信度・番号まで）。"""
    return ([p.to_json() for p in res.phonemes], [b.to_json() for b in res.boundaries], res.syllables)


def test_changed_phrase_is_realigned_alone_and_boundaries_stay(proj, tmp_path, monkeypatch):
    """歌詞を直したら、変わった区間だけを合わせ直す。結果（区間の境目の音素も）は全体を合わせ直したものと
    同じ（2026-10-10 承認）。"""
    from vocal_engine.phoneme import analyze as A
    from vocal_engine.project import Project
    real, done = A.align_lyrics_ranges, []

    def counting(*a, on_part=None, **k):
        def note(i, key, part):
            done.append(i)
            if on_part is not None:
                on_part(i, key, part)
        return real(*a, on_part=note, **k)
    monkeypatch.setattr(A, "align_lyrics_ranges", counting)
    proj.set_lyrics(P1, start_sec=R1[0], end_sec=R1[1])
    proj.set_lyrics(P2, start_sec=R2[0], end_sec=R2[1])
    first = proj.analyze_phonemes("take", compute=True)
    assert sorted(done) == [0, 1]
    # 後半の歌詞だけを変える: 後半だけを計算する
    done.clear()
    proj.set_lyrics(P1, start_sec=R2[0], end_sec=R2[1])
    inc = proj.analyze_phonemes("take", compute=True)
    assert done == [1]
    assert not inc.aligner.get("pending_entries")
    # 新しい作業場所で全体を合わせたものと同じ
    fresh = Project.open(TAKE, project_dir=str(tmp_path / "fresh"))
    fresh.ensure_analyzed()
    fresh.set_lyrics(P1, start_sec=R1[0], end_sec=R1[1])
    fresh.set_lyrics(P1, start_sec=R2[0], end_sec=R2[1])
    full = fresh.analyze_phonemes("take", force=True)
    assert _ph(inc) == _ph(full)
    # 前半の音素（後半との境目まで）の位置・種類は、前の結果から変わらない（確信度は結果全体で掛ける係数が
    # 変わるので比べない。全体を合わせ直しても同じ値になることは上で見た）
    def head(res):
        return [(x["label"], x["text"], x["start_sec"], x["end_sec"], x["syllable_index"])
                for x in _ph(res)[0] if x["end_sec"] <= R2[0]]
    assert len(head(first)) > 2 and head(first) == head(inc)
    # 同じ歌詞で開き直すと、part から組む（計算しない）
    done.clear()
    again = Project.open(TAKE, project_dir=proj.dir)
    again.ensure_analyzed()
    assert _ph(again.analyze_phonemes("take", compute=True)) == _ph(inc) and done == []


def test_unaligned_phrase_is_pending_without_compute(proj):
    """計算しない（手動の設定・裏で解析する前）なら、変わった区間は未解析のまま、ほかの区間の音素は出す。"""
    proj.set_lyrics(P1, start_sec=R1[0], end_sec=R1[1])
    proj.analyze_phonemes("take", compute=True)
    proj.set_lyrics(P2, start_sec=R2[0], end_sec=R2[1])
    res = proj.analyze_phonemes("take", compute=False)
    assert res.aligner["pending_entries"] == [1]
    assert [e.get("pending", False) for e in res.entries] == [False, True]
    voiced = [p for p in res.phonemes if p.label != "silence"]
    assert voiced and all(p.end_sec <= R2[0] for p in voiced)
    assert proj.phonemes_pending("take")
    assert proj.phonemes("take", complete=True) is not None      # 自動の設定で裏が無いので、ここで計算する
    assert not proj.phonemes_pending("take")


def test_two_ranges_align_independently(proj):
    proj.set_lyrics(" %s " % P1, start_sec=R1[0], end_sec=R1[1])
    proj.set_lyrics(P2, start_sec=R2[0], end_sec=R2[1])
    assert len(proj.lyrics_entries("take")) == 2
    assert proj.lyrics_text("take") == LYRICS

    res = proj.analyze_phonemes("take", force=True)
    assert len(res.entries) == 2
    assert res.aligner["entries"] == 2
    voiced = [p for p in res.phonemes if p.label != "silence"]
    assert " ".join(p.text for p in voiced) == \
        M.text("C.part1_phonemes") + " " + M.text("C.part2_phonemes")
    # 区間の間は無音で埋まっていて、音素は立たない
    between = [p for p in res.phonemes
               if p.end_sec > R1[1] + 0.02 and p.start_sec < R2[0] - 0.02]
    assert between and all(p.label in ("silence", "breath") for p in between)
    # 素材の頭と尻も無音で埋まっている（0 秒から素材の終わりまで切れ目が無い）
    assert res.phonemes[0].start_sec == 0.0
    assert abs(res.phonemes[-1].end_sec - proj.duration_sec) < 1e-3
    for a, b in zip(res.phonemes[:-1], res.phonemes[1:]):
        assert abs(a.end_sec - b.start_sec) < 1e-3
    # 音節の通し番号と id は区間をまたいで一意
    assert len({p.id for p in res.phonemes}) == len(res.phonemes)
    assert [s["index"] for s in res.syllables] == list(range(len(res.syllables)))


def test_removing_one_range_keeps_the_other(proj):
    proj.set_lyrics(P1, start_sec=R1[0], end_sec=R1[1])
    proj.set_lyrics(P2, start_sec=R2[0], end_sec=R2[1])
    proj.set_lyrics("", start_sec=R1[0], end_sec=R1[1])          # 前半だけ消す
    ent = proj.lyrics_entries("take")
    assert [e["text"] for e in ent] == [P2]
    res = proj.analyze_phonemes("take", force=True)
    voiced = [p for p in res.phonemes if p.label != "silence"]
    assert " ".join(p.text for p in voiced) == M.text("C.part2_phonemes")
    assert all(p.start_sec >= R2[0] - 0.25 for p in voiced)


def test_cache_is_keyed_on_the_ranges(proj):
    """区間を変えたらキャッシュを使い回さない。"""
    proj.set_lyrics(P1, start_sec=R1[0], end_sec=R1[1])
    a = proj.analyze_phonemes("take")
    proj.set_lyrics(P1, start_sec=R1[0], end_sec=R1[1] - 0.15)
    b = proj.analyze_phonemes("take")
    assert a is not b
    assert b.entries[0]["end_sec"] != a.entries[0]["end_sec"]


def test_old_string_format_still_loads(tmp_path):
    """段階2 までの `{"take": "歌詞"}` も読める（素材全体の 1 区間になる）。"""
    import json

    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "old"))
    with open(p.json_path, encoding="utf-8") as f:
        d = json.load(f)
    d["lyrics"] = {"take": LYRICS}
    with open(p.json_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    q = Project(p.dir).load()
    assert q.lyrics_entries("take") == [
        {"start_sec": None, "end_sec": None, "text": LYRICS}]
    assert q.has_lyrics("take")


def test_mcp_set_lyrics_takes_an_array(tmp_path):
    from vocal_engine import mcp_server as m
    r = m.open_project(TAKE, project_dir=str(tmp_path / "mcp"))
    assert r["ok"], r
    m.analyze_take(background=False)
    r = m.set_lyrics(entries=[{"start_sec": R1[0], "end_sec": R1[1], "text": P1},
                              {"start_sec": R2[0], "end_sec": R2[1], "text": P2}])
    assert r["ok"], r
    assert r["n_entries"] == 2
    assert len(r["per_entry"]) == 2
    assert r["phonemes"] == M.data("C.phonemes")
    g = m.get_phonemes(start_sec=R2[0], end_sec=R2[1])
    assert g["ok"] and g["phonemes"]
    # 範囲なしの呼び方（段階2 までと同じ）も通る
    r = m.set_lyrics(LYRICS)
    assert r["ok"] and r["n_entries"] == 1
    assert r["entries"][0]["start_sec"] is None
    # 歌詞ファイルの中身をそのまま渡せる
    r = m.set_lyrics(from_text="0.30 1.60 %s\n2.20 3.52 %s\n" % (P1, P2))
    assert r["ok"] and r["n_entries"] == 2
    assert m.set_lyrics("")["n_entries"] == 0
