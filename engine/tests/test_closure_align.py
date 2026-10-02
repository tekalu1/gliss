# -*- coding: utf-8 -*-
"""issue #58: 促音「っ」（`cl`）で音素アラインが落ち、画面の操作のたびにやり直して失敗していた。

- `cl` は HubertFA の語彙では接頭辞なし・無音と同じクラス。`ja/cl` にしない。飛ばさない 1 状態として渡す
- 推定の読み・確定の歌詞に「っ」があっても落ちない
- 区間のアラインが失敗しても `analyze_take` や他の操作は落ちない（その区間は音素なしで続け、警告）
- 失敗は覚えておき、同じ入力（歌詞・音声）で操作のたびにやり直さない
"""
import json
import os
import random

import numpy as np
import pytest

import materials as M
from conftest import TAKE, needs_clips, needs_model
from vocal_engine.phoneme import analyze as AN
from vocal_engine.phoneme import g2p as G
from vocal_engine.phoneme import hubertfa as H
from vocal_engine.phoneme.hubertfa import HubertFA

needs_hfa = pytest.mark.skipif(not H.model_found(), reason="HubertFA の重みが無い")
LYRICS_CL = M.text("C.lyrics_tsu")      # 素材 C の歌詞に「っ」を 1 つ足したもの


# ---------------------------------------------------------------- 語彙（偽物の HubertFA で重み無しに確かめる）
def _fake_hfa(tmp_path, words=None):
    """vocab.json の形は v0.0.7 と同じ（silent_phonemes は接頭辞なしで id 0）。"""
    d = tmp_path / "hfa"
    d.mkdir()
    vocab = {"SP": 0, "cl": 0, "AP": 0, "": 0, "pau": 0,
             "ja/a": 1, "ja/k": 2, "ja/o": 3, "ja/g": 4, "ja/u": 5, "ja/N": 6}
    (d / "vocab.json").write_text(json.dumps({
        "vocab": vocab, "vocab_size": 7, "language_prefix": True,
        "silent_phonemes": ["", "AP", "cl", "pau", "SP"],
        "non_lexical_phonemes": ["AP", "EP"],
        "dictionaries": {"ja": "dict.txt"}}), encoding="utf-8")
    (d / "config.json").write_text(json.dumps({"mel_spec_config": {
        "sample_rate": 44100, "hop_size": 441}}), encoding="utf-8")
    (d / "model.onnx").write_bytes(b"")
    words = words or {"a": "a", "ka": "k a", "ko": "k o", "ga": "g a", "u": "u", "o": "o",
                      "n": "N", "cl": "cl", "zo": "z o"}      # ja/z は語彙に無い
    (d / "dict.txt").write_text("".join("%s\t%s\n" % kv for kv in words.items()), encoding="utf-8")
    return HubertFA(str(d))


def test_closure_is_passed_without_language_prefix(tmp_path):
    al = _fake_hfa(tmp_path)
    ph, words, _, unknown, used = al.build_sequence(["ga", "cl", "ko", "u"])
    assert ph == ["SP", "ja/g", "ja/a", "cl", "ja/k", "ja/o", "SP", "ja/u", "SP"]
    assert all(p in al.vocab["vocab"] for p in ph)
    assert words == ["ga", "cl", "ko", "u"] and used == [0, 1, 2, 3] and not unknown


def test_closure_at_the_edges_and_doubled(tmp_path):
    al = _fake_hfa(tmp_path)
    # 語尾の「あっ」: 閉鎖のあとに SP（末尾の無音を閉鎖にしない）
    assert al.build_sequence(["a", "cl"])[0] == ["SP", "ja/a", "cl", "SP"]
    # 頭の「っか」: 先頭の SP は残す
    assert al.build_sequence(["cl", "ka"])[0] == ["SP", "cl", "ja/k", "ja/a", "SP"]
    # 「あっっか」: 閉鎖は 1 つにまとめる（音節は 2 つとも使った扱い）
    ph, words, _, _, used = al.build_sequence(["a", "cl", "cl", "ka"])
    assert ph == ["SP", "ja/a", "cl", "ja/k", "ja/a", "SP"]
    assert used == [0, 1, 2, 3]


def test_out_of_vocabulary_phoneme_skips_the_syllable_instead_of_keyerror(tmp_path):
    al = _fake_hfa(tmp_path)
    ph, words, _, unknown, used = al.build_sequence(["ka", "zo", "xx", "a"])
    assert "ja/z" not in ph and unknown == ["zo", "xx"] and used == [0, 3]
    assert al.unknown_words(["ka", "cl", "zo"]) == ["zo"]
    with pytest.raises(H.AlignerError):
        al.build_sequence(["cl"])                    # 閉鎖しか無い: 切るものが無い
    with pytest.raises(H.AlignerError):
        al._decode(np.zeros((1, 7, 5)), np.zeros((1, 5)), 0.05, ["SP", "ja/cl", "SP"])


def test_viterbi_keeps_closure_but_may_skip_sp(tmp_path):
    """無音のフレームが 1 つも無くても閉鎖は 1 フレーム以上取る（音節の数が歌詞と合う）。"""
    al = _fake_hfa(tmp_path)
    ph, *_ = al.build_sequence(["a", "cl", "ka"])
    T = 30
    logits = np.full((1, 7, T), -8.0)
    logits[0, 1, :15] = 8          # a
    logits[0, 2, 15:20] = 8        # k
    logits[0, 1, 20:] = 8          # a
    edge = np.full((1, T), -4.0)
    edge[0, [15, 20]] = 4.0
    spans, _, _ = al._decode(logits, edge, T * al.frame_sec, ph)
    texts = [s["text"] for s in spans if s["text"] != "SP"]
    assert texts == ["a", "cl", "k", "a"]
    cl = next(s for s in spans if s["text"] == "cl")
    assert cl["end"] - cl["start"] >= al.frame_sec * 0.5


def test_attach_text_follows_used_syllables_and_merged_closures():
    res = G.g2p("あっっか", use_pyopenjtalk=False)
    assert [s.romaji for s in res.syllables] == ["a", "cl", "cl", "ka"]
    raw = [{"start": 0.0, "end": 0.2, "text": "a"}, {"start": 0.2, "end": 0.3, "text": "cl"},
           {"start": 0.3, "end": 0.35, "text": "k"}, {"start": 0.35, "end": 0.6, "text": "a"}]
    spans = AN._attach_text(raw, res, used=[0, 1, 2, 3])
    assert [(p.text, p.kana, p.label) for p in spans] == [
        ("a", "あ", "vowel"), ("cl", "っ", "silence"), ("k", "か", "consonant"), ("a", "か", "vowel")]
    # 辞書に無くて飛ばした音節があっても、後ろの音節の文字がずれない
    res2 = G.g2p("かあか", use_pyopenjtalk=False)
    raw2 = [{"start": 0.0, "end": 0.1, "text": "k"}, {"start": 0.1, "end": 0.3, "text": "a"},
            {"start": 0.3, "end": 0.35, "text": "k"}, {"start": 0.35, "end": 0.6, "text": "a"}]
    spans2 = AN._attach_text(raw2, res2, used=[0, 2])
    assert [p.syllable_index for p in spans2] == [0, 0, 2, 2]


# ---------------------------------------------------------------- 閉鎖の区間（エネルギー）
class _F0:
    def __init__(self, rms_db, hop_s=0.01):
        self.rms_db = np.asarray(rms_db, dtype="float64")
        self.hop_s = hop_s


def _span(text, a, b):
    return AN._span({"start": a, "end": b, "text": text}, text, None, None, None)


def test_closure_widens_to_the_quiet_frames():
    """アライナーが閉鎖を 1 フレームに潰して、無音を前の母音と次の子音に配った形（issue #58 の実測）。"""
    rms = np.full(100, -20.0)
    rms[40:55] = -60.0                                 # 本当の閉鎖 0.40〜0.55 s
    spans = [_span("a", 0.0, 0.45), _span("cl", 0.45, 0.46), _span("k", 0.46, 0.6),
             _span("a", 0.6, 1.0)]
    AN._refine_closures(spans, _F0(rms))
    a, cl, k = spans[0], spans[1], spans[2]
    assert (cl.start_sec, cl.end_sec) == (0.4, 0.55)
    assert a.end_sec == cl.start_sec and k.start_sec == cl.end_sec
    assert "closure_from_energy" in cl.flags
    # 落ちていなければそのまま
    spans = [_span("a", 0.0, 0.45), _span("cl", 0.45, 0.46), _span("k", 0.46, 0.6)]
    AN._refine_closures(spans, _F0(np.full(100, -20.0)))
    assert (spans[1].start_sec, spans[1].end_sec) == (0.45, 0.46)


# ---------------------------------------------------------------- 推定の読み（#48 の復号側）
@needs_hfa
def test_estimator_only_emits_syllables_the_aligner_knows():
    from vocal_engine.phoneme.auto_lyrics import KANA, GuessedSyllable, alignable
    al = H.get_aligner()
    kana = list(KANA.values()) + ["っ", "あー"]
    for k in kana:
        romaji = [s.romaji for s in G.kana_to_syllables(k, keep_punct_as=None)]
        assert romaji and not al.unknown_words(romaji), (k, romaji)
    # 実際の語彙で、読みの規則が作るすべての音節が組み立てられる（KeyError にならない）
    words = sorted({s.romaji for k in list(G._BASE) + list(G._DIGRAPH) + ["っ"]
                    for s in G.kana_to_syllables(k, keep_punct_as=None)})
    ph, *_ = al.build_sequence(words)
    assert all(p in al.vocab["vocab"] for p in ph)
    assert "cl" in ph and "ja/cl" not in ph

    class _Al:
        def unknown_words(self, words):
            return [w for w in words if w == "ka"]
    s = [GuessedSyllable("あ", 0, 0.1, 0.9, ("a",)), GuessedSyllable("か", 0.1, 0.2, 0.9, ("k", "a"))]
    assert [x.kana for x in alignable(s, _Al())] == ["あ"]


# ---------------------------------------------------------------- 実素材: 「っ」を含む歌詞
@needs_clips
@needs_model
@needs_hfa
def test_confirmed_and_estimated_lyrics_with_small_tsu_align(project):
    from vocal_engine.phoneme.lyrics import normalize
    project.set_lyrics(LYRICS_CL)
    r = project.phonemes("take")
    assert r is not None and not r.aligner.get("failed_entries")
    cl = [p for p in r.phonemes if p.text == "cl"]
    assert len(cl) == 1 and cl[0].label == "silence" and cl[0].detail == "closure"
    assert any(s["kana"] == "っ" for s in r.syllables)
    # 推定の読み（#48）に「っ」がある（ユーザーのプロジェクトで落ちた形）
    d = project.duration_sec
    project.set_lyrics_entries(normalize([
        {"start_sec": 0.0, "end_sec": round(d / 2, 3), "text": M.text("C.part1_tsu"),
         "origin": "estimated", "estimate": {"reading": M.text("C.part1_tsu"), "confidence": 0.5}},
        {"start_sec": round(d / 2, 3), "end_sec": round(d, 3), "text": M.text("C.part2_tsu"),
         "origin": "estimated", "estimate": {"reading": M.text("C.part2_tsu"), "confidence": 0.5}}]))
    r = project.phonemes("take")
    assert r is not None and not r.aligner.get("failed_entries")
    assert sum(1 for p in r.phonemes if p.text == "cl") == 2


# ---------------------------------------------------------------- 失敗しても落とさない・やり直さない
class _FakeAligner:
    """`get_aligner()` の代わり。`fail` に入った歌詞（かな）の区間は例外にする。呼んだ回数を数える。"""

    providers = ["CPUExecutionProvider"]
    folder = "fake"

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = 0

    def align(self, seg, sr, romaji, detect_breath=True):
        self.calls += 1
        if any(f in "".join(romaji) for f in self.fail):
            raise KeyError("ja/cl")                  # issue #58 と同じ落ち方
        n = len(seg) / float(sr)
        step = n / (len(romaji) + 2)
        spans, t = [], step
        for w in romaji:
            for ph in G._ROMAJI_TO_PHONEMES.get(w, [w]):
                spans.append({"start": t, "end": t + step / 2, "text": ph, "confidence": 0.8})
                t += step / 2
        return {"phonemes": spans, "breaths": [], "total_confidence": 0.8, "elapsed_sec": 0.0,
                "rtf": 0.0, "unknown_syllables": [], "used_words": list(range(len(romaji)))}


@pytest.fixture
def fake_aligner(monkeypatch):
    fa = _FakeAligner()
    monkeypatch.setattr(AN, "get_aligner", lambda: fa)
    return fa


def _two_entries(project, second="ほしぞら"):
    d = project.duration_sec
    project.set_lyrics_entries([{"start_sec": 0.0, "end_sec": round(d / 2, 3), "text": "さくら"},
                                {"start_sec": round(d / 2, 3), "end_sec": round(d, 3),
                                 "text": second}])


@needs_clips
@needs_model
def test_failed_entry_does_not_break_other_operations(project, fake_aligner):
    from vocal_engine import mcp_server as m
    fake_aligner.fail = {"zora"}
    _two_entries(project)
    m._state["project"] = project
    try:
        r = m.analyze_take(background=False)
        assert r["ok"], r
        assert r["phonemes"]["failed_entries"] == [1]
        res = project.phonemes("take")
        assert [w["entry_index"] for w in res.warnings if w["kind"] == "align_failed"] == [1]
        assert "ja/cl" in res.warnings[0]["message"] or any(
            "ja/cl" in w["message"] for w in res.warnings)
        # 失敗した区間は音素なし（無音）、もう片方は音素がある
        half = project.duration_sec / 2
        assert all(p.label == "silence" for p in res.phonemes if p.start_sec >= half + 0.25)
        assert any(p.label != "silence" for p in res.phonemes if p.end_sec <= half)
        n = fake_aligner.calls
        for _ in range(3):
            assert m.export_view_data()["ok"]
            assert project.phonemes("take") is res
        ids = [x.id for x in project.take_notes if x.kind == "note"][:2]
        assert m.reset_to_original(note_ids=ids, author="human")["ok"]
        plan = m.plan_edit(op="guide", note_ids=ids)
        assert plan["ok"], plan
        assert m.apply_plan(plan_id=plan["plan_id"], x=1.0, pitch=1.0)["ok"]
        assert fake_aligner.calls == n                  # 操作のたびにアラインし直さない
    finally:
        m._state["project"] = None


@needs_clips
@needs_model
def test_all_entries_failed_is_reported_in_view_data(project, fake_aligner):
    """すべての区間が失敗して音素が 1 つも無いとき、画面には「歌詞が無い」ではなく理由を出す。"""
    from vocal_engine.view.export_data import _no_phonemes
    fake_aligner.fail = {"sakura", "zora"}
    _two_entries(project)
    res = project.phonemes("take")
    assert res is not None and res.aligner["failed_entries"] == [0, 1]
    block = _no_phonemes(project, res)
    assert block["error"] == "'ja/cl'" and "切れなかった" in block["reason"]
    assert "error" not in _no_phonemes(project, None)


@needs_clips
@needs_model
def test_total_failure_is_remembered_and_not_retried(project, monkeypatch):
    from vocal_engine import mcp_server as m
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("壊れたアライナー")
    monkeypatch.setattr(AN, "get_aligner", broken)
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    project.analyze()                                  # 例外にしない（AlignerSetupError 以外）
    project.set_lyrics("さくら")
    m._state["project"] = project
    try:
        assert project.phonemes("take") is None
        assert project.phoneme_error("take") == "壊れたアライナー"
        n = len(calls)
        for _ in range(3):
            assert project.phonemes("take") is None
            v = m.export_view_data()
            assert v["ok"], v
        r = m.analyze_take(background=False)
        assert r["ok"], r
        assert r["phonemes"]["supported"] is False and "壊れたアライナー" in r["phonemes"]["error"]
        assert len(calls) == n                          # 同じ歌詞・音声ではやり直さない
        project.set_lyrics("さくらさくら")               # 歌詞が変われば取り直す
        assert project.phonemes("take") is None and len(calls) == n + 1
        with pytest.raises(RuntimeError):
            project.analyze_phonemes("take", force=True)    # force は取り直す
        assert len(calls) == n + 2
    finally:
        m._state["project"] = None


@needs_clips
@needs_model
def test_setup_error_is_still_raised_by_analyze(project, monkeypatch):
    """重みが無いなど環境の問題は黙って飛ばさない（今までどおり）。"""
    def missing():
        raise H.AlignerSetupError("重みが無い")
    monkeypatch.setattr(AN, "get_aligner", missing)
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    project.analyze()
    project.set_lyrics("さくら")
    with pytest.raises(H.AlignerSetupError):
        project.analyze()
    assert project.analysis["phonemes_take"]["supported"] is False
    assert project.phonemes("take") is None          # 画面の操作は落ちない


@needs_clips
@needs_model
def test_failed_result_of_older_revision_is_realigned(project, fake_aligner):
    fake_aligner.fail = {"zora"}
    _two_entries(project)
    res = project.phonemes("take")
    assert res.aligner["failed_entries"] == [1]
    assert res.aligner["revision"] == AN.ALIGN_REVISION
    # 同じ版のキャッシュは使う（開き直してもやり直さない）
    from vocal_engine.project import Project
    n = fake_aligner.calls
    p2 = Project(project.dir).load()
    assert p2.phonemes("take").aligner["failed_entries"] == [1] and fake_aligner.calls == n
    # 失敗を含む前の版のキャッシュ（issue #58 の前の版）は取り直す
    for path in (p2._cache_path("take-phonemes.json"),
                 p2._keyed_phoneme_cache("take", p2.lyrics_entries("take"))):
        d = json.load(open(path, encoding="utf-8"))
        d["aligner"]["revision"] = 1
        json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    fake_aligner.fail = set()
    p3 = Project(p2.dir).load()
    assert not p3.phonemes("take").aligner["failed_entries"] and fake_aligner.calls > n


# ---------------------------------------------------------------- 鉛筆の索引（重さの測定で見つけたもの）
def test_pencil_lookup_by_time_gives_the_same_values():
    from vocal_engine.project.pitch import Layered

    class _Base:
        def at(self, t, side="right"):
            return 10.0 * np.sin(t)

    class _Draw:
        def __init__(self, lo, hi, v):
            self.lo, self.hi, self.v = lo, hi, v

        def w(self, t):
            if t <= self.lo or t >= self.hi:
                return 0.0
            return 0.5 - 0.5 * np.cos(np.pi * (t - self.lo) / (self.hi - self.lo))

        def target(self, t, side):
            return self.v

    rnd = random.Random(58)
    drs = []
    for _ in range(120):
        a = rnd.uniform(0, 60)
        drs.append(_Draw(a, a + rnd.uniform(0.05, 3.0), rnd.uniform(-300, 300)))
    lay = Layered(_Base(), [], drs)
    for t in np.linspace(-1, 65, 2000):
        k = 1.0
        v = 10.0 * np.sin(t)
        for d in drs:                                # 全件を回す元の式
            w = d.w(t)
            k *= 1.0 - w
            if w > 0:
                v = (1.0 - w) * v + w * d.target(t, "right")
        assert lay.keep(t) == pytest.approx(k, abs=0, rel=0)
        assert lay.at(t) == pytest.approx(v, abs=1e-12)
