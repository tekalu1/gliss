# -*- coding: utf-8 -*-
"""段階2: 歌詞 → 音素 → 強制アラインメント → 音素単位のタイミング編集。

**素材と RMVPE の重みがあるのに HubertFA の重みが無いときは、スキップせずに fail させる**
（重みの置き場の `hubertfa/` に置く前提。無いなら入手元つきのメッセージが出る）。
"""
import numpy as np
import pytest

import materials as M
from conftest import CLIP_E, GUIDE, TAKE, needs_clips, needs_model

pytestmark = [needs_clips, needs_model, pytest.mark.usefixtures("rmvpe_f0")]   # ノートの ID・区切りは RMVPE の解析のもの

LYRICS_C = M.text("C.lyrics_marked")       # 素材 C の歌詞（区切りの「！」つき）


# ---------------------------------------------------------------- 重みの所在
def test_weights_are_present_with_a_useful_message():
    """重みが無いならスキップではなく落とす（入手元がメッセージに入っていること）。"""
    from vocal_engine.phoneme import hubertfa
    assert hubertfa.model_found(), hubertfa.missing_model_message()
    msg = hubertfa.missing_model_message()
    assert "github.com/wolfgitpr/HubertFA" in msg
    assert hubertfa.DOWNLOAD_SHA256 in msg
    assert hubertfa.HUBERTFA_DIR in msg


# ---------------------------------------------------------------- 歌詞 → 音素
def test_g2p_kana_and_phonemes():
    from vocal_engine.phoneme import g2p as G
    assert not G.run_self_tests()
    r = G.g2p("さくらいろ")
    assert r.kana == "サクライロ"
    assert " ".join(r.phoneme_seq) == "s a k u r a i r o"
    assert r.romaji_seq == ["sa", "ku", "ra", "i", "ro"]


def test_g2p_absorbs_long_vowel():
    """長音「ー」は直前の母音に吸収する（段階2 の最重要の実装指針）。

    `トマー` を辞書どおり `t o m a a` と書くと、HubertFA も SOFA も
    2 つ目の母音を 8〜10 ms に潰す（段階 2 の評価で実測）。
    """
    from vocal_engine.phoneme import g2p as G
    assert " ".join(G.g2p("トマー").phoneme_seq) == "t o m a"          # 既定で吸収
    assert " ".join(G.g2p("トマー", merge_long=False).phoneme_seq) == "t o m a a"
    assert " ".join(G.g2p("トーマトマ").phoneme_seq) == "t o m a t o m a"
    # 全角チルダ・半角ハイフンも長音として扱う
    for mark in ("〜", "～", "~", "-", "ー"):
        assert " ".join(G.g2p("カ" + mark).phoneme_seq) == "k a"
    # 吸収してもかなは残る（画面に「マー」と出せる）
    sy = G.g2p("トマー").syllables
    assert [s.kana for s in sy] == ["と", "まー"]


def test_g2p_kanji_via_pyopenjtalk():
    from vocal_engine.phoneme import g2p as G
    if not G.pyopenjtalk_available():
        pytest.fail("pyopenjtalk-plus が .venv に無い（engine/pyproject.toml の extras の g2p）")
    r = G.g2p("子ども")
    assert r.kana == "コドモ"
    r2 = G.g2p("青い空は晴れていますか〜！")
    assert "アオイ" in r2.kana and "ハレテ" in r2.kana
    assert r2.phoneme_seq[-1] == "a"                      # 「か〜」の長音は吸収済み


def test_labels_split_consonant_and_vowel():
    from vocal_engine.phoneme import g2p as G
    assert G.label_of("p") == "consonant" and G.detail_of("p") == "consonant_unvoiced"
    assert G.label_of("m") == "consonant" and G.detail_of("m") == "consonant_voiced"
    assert G.label_of("a") == "vowel"
    assert G.label_of("N") == "vowel" and G.detail_of("N") == "moraic_nasal"
    assert G.label_of("cl") == "silence" and G.detail_of("cl") == "closure"
    assert G.label_of("AP") == "breath"
    assert G.label_of("SP") == "silence"


# ---------------------------------------------------------------- アラインメント
@pytest.fixture(scope="module")
def aligned_C():
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono
    from vocal_engine.phoneme.analyze import align_lyrics
    x, sr = read_mono(TAKE)
    f0r = estimate_f0(x=x, sr=sr)
    return align_lyrics(x, sr, LYRICS_C, f0r=f0r), f0r


def test_align_C_has_31_phonemes(aligned_C):
    """C の歌詞 = 17 音節・31 音素（音素の並びと音節の数は素材のフォルダの materials.json）。"""
    res, _ = aligned_C
    voiced = [p for p in res.phonemes if p.label != "silence"]
    assert len(voiced) == M.data("C.phonemes"), [p.text for p in voiced]
    assert " ".join(p.text for p in voiced) == (
        M.text("C.part1_phonemes") + " " + M.text("C.part2_phonemes"))
    assert len(res.syllables) == M.data("C.syllables")
    assert [s["kana"] for s in res.syllables][:4] == M.data("C.first_syllables")
    # 時間は途切れずに並ぶ
    for a, b in zip(res.phonemes[:-1], res.phonemes[1:]):
        assert abs(a.end_sec - b.start_sec) < 1e-6
    assert res.phonemes[0].start_sec == 0.0
    assert res.aligner["name"] == "hubertfa"
    assert res.aligner["rtf"] < 0.5


def test_unvoiced_closures_land_on_energy_dips(aligned_C):
    """無声閉鎖（k / t / p）は隣の母音より 12 dB 以上落ちた場所にあること。

    段階2 の実測では HubertFA は 6 クリップで 32/33 が正しい位置だった
    （絶対しきい値ではなく**隣の母音に対する相対落差**で見るのが要点）。
    摩擦音（s / sh / h / f）は閉鎖ではなくエネルギーが残るので対象外。
    """
    from vocal_engine.phoneme.analyze import _neighbour_vowel_rms, _rms
    res, f0r = aligned_C
    plosives = {"k", "ky", "kw", "t", "ty", "p", "py", "cl"}
    near = _neighbour_vowel_rms(res.phonemes, f0r)
    checked, ok = 0, 0
    for p in res.phonemes:
        if p.text not in plosives:
            continue
        if p.index not in near or p.duration_sec < 0.03:
            continue
        checked += 1
        if _rms(f0r, p.start_sec, p.end_sec) - near[p.index] <= -12.0:
            ok += 1
    assert checked >= 2
    assert ok == checked, "無声閉鎖の位置が合っていない: %d/%d" % (ok, checked)
    # C の 2 つ目の p は 0.816 s 付近（評価の段階に手で付けた位置と同じ）
    ps = [p for p in res.phonemes if p.text == "p"]
    assert len(ps) == 2
    assert abs(ps[1].start_sec - 0.816) < 0.02


def test_boundaries_and_confidence(aligned_C):
    res, _ = aligned_C
    assert len(res.boundaries) == len(res.phonemes) + 1
    kinds = {b.kind for b in res.boundaries}
    assert "consonant_vowel" in kinds
    for b in res.boundaries:
        assert 0.0 <= b.confidence <= 1.0
    for p in res.phonemes:
        assert 0.0 <= p.confidence <= 1.0
        assert p.label in ("consonant", "vowel", "breath", "silence")
    assert 0.3 < res.confidence < 1.0
    # 発声塊の検算が aligner 情報に入る
    assert "blobs" in res.aligner and "blob_head_to_boundary_median_ms" in res.aligner
    assert res.aligner["blob_head_to_boundary_median_ms"] < 30.0


def test_blob_mismatch_lowers_confidence():
    """§7.6 の塊の数が合わないときは確信度を下げる（叫び素材 E）。"""
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono
    from vocal_engine.phoneme.analyze import align_lyrics
    x, sr = read_mono(CLIP_E)
    f0r = estimate_f0(x=x, sr=sr)
    res = align_lyrics(x, sr, M.text("E.lyrics"), f0r=f0r)
    assert res.aligner["blobs"] != res.aligner["voiced_groups"]
    assert res.aligner["confidence_multiplier"] < 1.0
    assert any(w["kind"] == "blob_count_mismatch" for w in res.warnings)
    # 長音を吸収しているので `o` は 1 つにまとまる（2 つ目が潰れない）
    os_ = [p for p in res.phonemes if p.text == "o"]
    assert len(os_) == 4 and min(p.duration_sec for p in os_) > 0.02


# ---------------------------------------------------------------- プロジェクト
@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    import shutil
    from vocal_engine.project import Project
    d = tmp_path_factory.mktemp("ph") / "proj"
    p = Project.open(TAKE, GUIDE, project_dir=str(d), lyrics=LYRICS_C,
                     guide_lyrics=LYRICS_C)
    p.analyze()
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)


def test_set_lyrics_round_trips(proj):
    import json
    # 歌詞は**区間の配列**で持つ（範囲を省いた = 素材全体の 1 件）
    assert proj.lyrics["take"] == [{"start_sec": None, "end_sec": None, "text": LYRICS_C}]
    assert proj.lyrics_text("take") == LYRICS_C
    with open(proj.json_path, encoding="utf-8") as f:
        assert json.load(f)["lyrics"]["take"][0]["text"] == LYRICS_C
    assert proj.analysis["phonemes_take"]["supported"] is True
    assert proj.analysis["phonemes_take"]["n_phonemes"] == M.data("C.phonemes")


def test_get_phonemes_tool_shape(proj):
    from vocal_engine.analysis.phonemes import get_phonemes
    r = get_phonemes(proj, 0.0, 1.5)
    assert r["supported"] is True and r["phonemes"]
    p = r["phonemes"][0]
    assert set(("id", "start_sec", "end_sec", "text", "kana", "label", "confidence",
                "stretchable")) <= set(p)
    assert r["boundaries"] and "edited_sec" in r["boundaries"][0]
    assert all(0.0 <= b["time_sec"] <= 1.5 + 1e-6 for b in r["boundaries"])


def test_notes_carry_phonemes(proj):
    from vocal_engine.analysis.notes import notes_in_range
    res = proj.phonemes("take")
    for n in notes_in_range(proj.take_notes, None, None, ("note",))[:5]:
        ov = res.overlapping(n.start_sec, n.end_sec)
        assert ov, "ノート %s に重なる音素が無い" % n.id
        for p in ov:
            assert p.end_sec > n.start_sec and p.start_sec < n.end_sec


def test_export_view_data_has_phoneme_lane(proj, tmp_path):
    import json
    from vocal_engine.view.export_data import export_view_data
    r = export_view_data(proj, path=str(tmp_path / "v.json"))
    with open(r["path"], encoding="utf-8") as f:
        d = json.load(f)
    ph = d["phonemes"]
    assert ph["has_lyrics"] is True and ph["phonemes"] and ph["syllables"]
    assert ph["phonemes"][0]["kana"] is None or isinstance(ph["phonemes"][0]["kana"], str)
    assert any(p["kana"] for p in ph["phonemes"])
    for p in ph["phonemes"]:
        assert set(("edited_start_sec", "edited_end_sec", "label", "text")) <= set(p)
    # ピアノロールの境界線は**音素境界**を使う
    assert len(d["boundaries"]) == len(ph["boundaries"])
    assert d["boundaries"][0]["kind"] != "note"
    assert any(n.get("phonemes") for n in d["notes"])


# ---------------------------------------------------------------- 境界の移動
def test_move_boundary_applies_and_undoes(proj):
    from vocal_engine.phoneme.edit import move_boundary_spec
    from vocal_engine.project.model import Target
    res = proj.phonemes("take")
    n0 = len(proj.edits)
    # 両隣が 60 ms 以上ある境界を選ぶ（下限 20 ms に当たらないように）
    by = {p.index: p for p in res.phonemes}
    b = next(x for x in res.boundaries
             if x.before_index in by and x.after_index in by
             and by[x.before_index].duration_sec > 0.06
             and by[x.after_index].duration_sec > 0.06)
    left, right = by[b.before_index], by[b.after_index]
    L0 = proj.edited_length(left.start_sec, b.time_sec)
    R0 = proj.edited_length(b.time_sec, right.end_sec)
    total0 = proj.edited_length(left.start_sec, right.end_sec)

    spec, info = move_boundary_spec(proj, b.id, 7.0)
    assert info["clamped"] is False and abs(info["ms"] - 7.0) < 1e-6
    spec["target"] = Target.boundary(b.id, spec["params"]["left_sec"],
                                     spec["params"]["right_sec"])
    cs, edits = proj.apply_edits([spec], author="human")
    assert len(proj.edits) == n0 + 1
    assert edits[0].kind == "move_boundary"

    L1 = proj.edited_length(left.start_sec, b.time_sec)
    R1 = proj.edited_length(b.time_sec, right.end_sec)
    assert abs((L1 - L0) - 0.007) < 5e-4, "左の音素が 7 ms 伸びていない"
    assert abs((R1 - R0) + 0.007) < 5e-4, "右の音素が 7 ms 縮んでいない"
    assert abs(proj.edited_length(left.start_sec, right.end_sec) - total0) < 1e-6, \
        "2 つ合わせた長さは変わらないこと"

    proj.undo(cs.id)
    assert len(proj.edits) == n0
    assert abs(proj.edited_length(left.start_sec, b.time_sec) - L0) < 1e-9
    assert abs(proj.edited_length(b.time_sec, right.end_sec) - R0) < 1e-9


def test_move_boundary_keeps_20ms_floor(proj):
    from vocal_engine.phoneme.edit import move_boundary_spec
    res = proj.phonemes("take")
    by = {p.index: p for p in res.phonemes}
    b = next(x for x in res.boundaries
             if x.before_index in by and x.after_index in by
             and by[x.after_index].duration_sec > 0.15)
    spec, info = move_boundary_spec(proj, b.id, 5000.0)     # 極端に動かす
    assert info["clamped"] is True
    assert info["right"]["ms_after"] >= 20.0 - 1e-6
    assert info["left"]["ms_after"] >= 20.0 - 1e-6


def test_move_boundary_rejects_the_edges(proj):
    from vocal_engine.phoneme.edit import BoundaryEditError, move_boundary_spec
    res = proj.phonemes("take")
    with pytest.raises(BoundaryEditError):
        move_boundary_spec(proj, res.boundaries[0].id, 5.0)     # 素材の頭
    with pytest.raises(BoundaryEditError):
        move_boundary_spec(proj, res.boundaries[-1].id, 5.0)    # 素材の尻


def test_stretch_accepts_a_phoneme(proj):
    from vocal_engine.project.model import Target
    res = proj.phonemes("take")
    v = max((p for p in res.phonemes if p.label == "vowel"),
            key=lambda p: p.duration_sec)
    n0 = len(proj.edits)
    before = proj.edited_length(v.start_sec, v.end_sec)
    cs, _ = proj.apply_edits([{"kind": "stretch",
                               "target": Target.range(v.start_sec, v.end_sec,
                                                      phoneme_id=v.id),
                               "params": {"ratio": 1.25}}], author="human")
    assert abs(proj.edited_length(v.start_sec, v.end_sec) - before * 1.25) < 1e-3
    proj.undo(cs.id)
    assert len(proj.edits) == n0


# ---------------------------------------------------------------- ガイドへ寄せる
def test_boundary_deviations_use_guide_lyrics(proj):
    devs, meta = proj.boundary_deviations()
    assert meta["source"] == "guide_phonemes"     # ガイドにも歌詞があるので直接アライン
    assert meta["matched"] > 20
    vals = [abs(d.timing_ms) for d in devs if d.timing_ms is not None]
    assert float(np.median(vals)) < 40.0
    for d in devs:
        assert d.boundary_id.startswith("b")


def test_correct_to_guide_timing_keeps_consonant_length(proj):
    """**母音（と息）だけ伸縮し、子音の長さは保つ**（設計の方針）。

    段階2 の `timing_specs` は `project/timing.py: plan_guide` に置き換えた
    （画面のプレビューと確定が同じ計画を使う）。子音の規則はそのまま。"""
    from vocal_engine.project import timing as TM
    res = proj.phonemes("take")
    n0 = len(proj.edits)
    before = {p.id: proj.edited_length(p.start_sec, p.end_sec) for p in res.phonemes}

    plan = TM.plan_guide(proj)
    cs, info = TM.apply_plan(proj, plan, 0.7)
    assert cs is not None, "タイミングの編集が 1 つも出ない"
    notes = TM.pitched_notes(proj)
    inside = [(n.start_sec, n.end_sec) for n in notes]

    changed_vowels = 0
    for p in res.phonemes:
        after = proj.edited_length(p.start_sec, p.end_sec)
        # ノートの中に丸ごと入っている子音だけを見る（ノートの外の子音は隙間の端で切られうる）
        whole = any(a - 1e-6 <= p.start_sec and p.end_sec <= b + 1e-6 for a, b in inside)
        if p.label == "consonant":
            assert abs(after - before[p.id]) < 1e-4,                 "子音 %s(%s) の長さが変わった: %.1f -> %.1f ms" % (
                    p.id, p.text, before[p.id] * 1000, after * 1000)
        elif p.label == "vowel" and abs(after - before[p.id]) > 1e-4:
            changed_vowels += 1
    assert changed_vowels > 0, "母音側が 1 つも動いていない"
    # 子音の**位置**は動いてよい（前の母音が伸び縮みするため）
    cons = [p for p in res.phonemes if p.label == "consonant"]
    moved = sum(1 for p in cons
                if abs(proj.edited_sec(p.start_sec) - p.start_sec) > 1e-4)
    assert moved > 0
    # 後ろはずれない（素材の末尾は元の位置）
    src, out = proj.time_map()
    assert abs(out[-1] - src[-1]) < 1e-9
    proj.undo(cs.id)
    assert len(proj.edits) == n0


def test_correct_to_guide_timing_survives_rendering(proj):
    """境界の移動が TD-PSOLA の時間マップにそのまま出ること。"""
    from vocal_engine.phoneme.edit import move_boundary_spec
    from vocal_engine.project.model import Target
    from vocal_engine.render.pipeline import Renderer, edits_to_segments
    res = proj.phonemes("take")
    by = {p.index: p for p in res.phonemes}
    b = next(x for x in res.boundaries
             if x.before_index in by and x.after_index in by
             and by[x.before_index].duration_sec > 0.08
             and by[x.after_index].duration_sec > 0.08)
    left, right = by[b.before_index], by[b.after_index]
    spec, _ = move_boundary_spec(proj, b.id, 25.0)
    spec["target"] = Target.boundary(b.id, spec["params"]["left_sec"],
                                     spec["params"]["right_sec"])
    cs, _ = proj.apply_edits([spec], author="human")

    x, sr = proj.audio("take")
    f0r = proj.take_f0
    r = Renderer(x, sr, f0r.f0, f0r.voiced, f0r.hop_s, backend="psola")
    t0, t1 = left.start_sec, right.end_sec
    segs = edits_to_segments(proj.edits_for(t0, t1), proj.edit_span)
    assert len(segs) == 2, "境界の移動は前後 2 区間の伸縮に展開される"
    y, info = r.render_range(t0, t1, segs)
    assert not info["warnings"]
    assert abs(len(y) / sr - (t1 - t0)) < 0.002, "全体の長さは変わらない"
    proj.undo(cs.id)


def test_band_skips_consonants_when_lyrics_exist(proj, tmp_path):
    """歌詞があれば、帯の高さ（平均の音程）から子音のフレームを除く（v3 §3）。"""
    import json
    from vocal_engine.view.export_data import (band_center, consonant_mask, envelope,
                                               export_view_data)
    from vocal_engine.analysis.f0 import hz_to_midi
    r = export_view_data(proj, path=str(tmp_path / "v.json"))
    with open(r["path"], encoding="utf-8") as f:
        d = json.load(f)
    f0r = proj.take_f0
    midi = hz_to_midi(f0r.f0)
    midi = np.where(np.asarray(f0r.voiced, dtype=bool) & (np.asarray(f0r.f0) > 0), midi, np.nan)
    x, sr = proj.audio("take")
    env = envelope(x, sr, f0r.times, f0r.hop_s)
    cons = consonant_mask(proj.phonemes("take"), f0r.times)
    assert cons is not None and cons.any()
    hop = f0r.hop_s
    differs = 0
    for n in d["notes"]:
        if n["kind"] != "note":
            continue
        a = int(round(n["start_sec"] / hop))
        b = int(round(n["end_sec"] / hop))
        want = band_center(midi, env, a, b, cons, n["edited_pitch_midi"])
        assert n["band_midi"] == pytest.approx(want, abs=1e-3)
        with_cons = band_center(midi, env, a, b, None, n["edited_pitch_midi"])
        if abs(with_cons - want) > 0.01:
            differs += 1
    assert differs > 0          # 有声の子音を含むノートでは、除いたぶん高さが変わる
