# -*- coding: utf-8 -*-
"""譜面ガイド（`score_guide.py`・MCP の `make_score_guide`）と、同じ時間軸の素材の帯の制約（`align.py`）。

素材は使わない: 譜面（MIDI）を書き、その音符を合成音で「歌った」テイクを作る。テイクは音符の頭を少しずつ
ずらし、ビブラートを付け、歌わない区間を挟む（曲全体のうち一部だけ録ったファイルの形）。
"""
import os
import struct

import numpy as np
import pytest
import soundfile as sf

SR = 48000
BPM = 132.0
START = 3.0                     # 譜面の 0 拍を置くタイムラインの秒（答え）
SONG_SEC = 44.0
SING = [(0.0, 13.0), (22.0, 41.0)]   # テイクが歌う区間（タイムラインの秒）


def _melody(seed=7, n=150):
    """[(拍, 長さの拍, 高さ)]。8 分音符と 4 分音符・休符を混ぜた旋律。"""
    rng = np.random.default_rng(seed)
    out, beat, p = [], 0.0, 64
    for _ in range(n):
        d = float(rng.choice([0.5, 0.5, 1.0]))
        if rng.random() < 0.12:
            beat += 0.5                                   # 休符
        p = int(np.clip(p + rng.choice([-2, -1, 0, 0, 1, 2, 3]), 57, 74))
        out.append((beat, d, p))
        beat += d
    return out


def _vlq(v):
    b = [v & 0x7f]
    v >>= 7
    while v:
        b.append(0x80 | (v & 0x7f))
        v >>= 7
    return bytes(reversed(b))


def write_midi(path, tracks, bpm=None, ppq=480):
    """tracks = [(名前, [(拍, 長さ, 高さ)])]。bpm=None ならテンポのメタイベントを入れない。"""
    def chunk(events):
        ev = sorted(events, key=lambda e: (e[0], e[1]))
        out, last = b"", 0
        for t, _, data in ev:
            out += _vlq(t - last) + data
            last = t
        out += _vlq(0) + b"\xff\x2f\x00"
        return b"MTrk" + struct.pack(">I", len(out)) + out
    body = []
    cond = []
    if bpm is not None:
        us = int(round(60_000_000 / bpm))
        cond.append((0, 0, b"\xff\x51\x03" + us.to_bytes(3, "big")))
    body.append(chunk(cond))
    for name, notes in tracks:
        ev = [(0, 0, b"\xff\x03" + _vlq(len(name.encode())) + name.encode())]
        for b, d, p in notes:
            on, off = int(round(b * ppq)), int(round((b + d) * ppq))
            ev.append((on, 2, bytes([0x90, p, 100])))
            ev.append((off, 1, bytes([0x80, p, 0])))
        body.append(chunk(ev))
    head = b"MThd" + struct.pack(">IHHH", 6, 1, len(body), ppq)
    with open(path, "wb") as f:
        f.write(head + b"".join(body))


def sing(notes, start, bpm, seed=3, transpose=0):
    """譜面の音符を「歌った」テイク: 頭を ±25 ms ずらし、5.5 Hz のビブラート、歌う区間の外は無音。"""
    rng = np.random.default_rng(seed)
    n = int(SONG_SEC * SR)
    y = np.zeros(n)
    spb = 60.0 / bpm
    for b, d, p in notes:
        s = start + b * spb + rng.uniform(-0.025, 0.025)
        e = start + (b + d) * spb - 0.04
        if not any(a <= s and e <= z for a, z in SING) or e - s < 0.05:
            continue
        i0, i1 = int(s * SR), int(e * SR)
        t = np.arange(i1 - i0) / SR
        f = 440.0 * 2 ** ((p + transpose - 69) / 12.0) * (1 + 0.006 * np.sin(2 * np.pi * 5.5 * t))
        ph = 2 * np.pi * np.cumsum(f) / SR
        tone = sum(np.sin(k * ph) / k ** 1.3 for k in range(1, 12))
        env = np.minimum(1.0, np.minimum(t / 0.02, (t[-1] - t) / 0.03 + 1e-9))
        y[i0:i1] += 0.2 * tone * env
    return y.astype("float32")


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    d = tmp_path_factory.mktemp("score_guide")
    mel = _melody()
    har = [(b, du, p + 4) for b, du, p in mel]
    mid = str(d / "song.mid")
    write_midi(mid, [("lead", mel), ("harmony", har)], bpm=BPM)
    mid0 = str(d / "song-notempo.mid")
    write_midi(mid0, [("lead", mel), ("harmony", har)], bpm=None)
    take = str(d / "take.wav")
    sf.write(take, sing(mel, START, BPM), SR, subtype="FLOAT")
    htake = str(d / "harmony-take.wav")
    sf.write(htake, sing(har, START, BPM, seed=5), SR, subtype="FLOAT")
    return {"dir": d, "mid": mid, "mid0": mid0, "take": take, "htake": htake, "mel": mel}


def _take_inputs(path):
    from vocal_engine.analysis import onsets as ON
    from vocal_engine.analysis.f0 import estimate_f0, resolve_estimator
    from vocal_engine.audio import read_mono
    x, sr = read_mono(path)
    return estimate_f0(x=x, sr=sr, estimator=resolve_estimator()), ON.detect(x, sr)   # 重みが無ければ Gliss のモデル


def test_build_finds_start_and_track(song):
    """時間の頭（譜面の 0 拍の秒）とトラック（主旋律／ハモリ）をテイクの高さから当てる。"""
    from vocal_engine import score_guide as SG, score_import
    score = score_import.read_score(song["mid"])
    f0, on = _take_inputs(song["take"])
    notes, start, y, info = SG.build(score, f0, 0.0, on, SONG_SEC)
    assert info["track"]["name"] == "lead"
    # 高さで合わせた頭は答えの位置。発音の頭（立ち上がりの検出は音の頭より後ろに出る）に寄せた分は上限の内
    assert abs(info["fit"]["pitch_start_sec"] - START) < 0.03, info["fit"]
    assert 0.0 <= start - info["fit"]["pitch_start_sec"] <= SG.ONSET_MAX_SHIFT + 1e-9
    assert info["fit"]["fit"] > 0.6 and not info["warnings"]
    assert len(y) == int(round(SONG_SEC * SG.SR))
    f0h, onh = _take_inputs(song["htake"])
    _, start_h, _, info_h = SG.build(score, f0h, 0.0, onh, SONG_SEC)
    assert info_h["track"]["name"] == "harmony" and abs(info_h["fit"]["pitch_start_sec"] - START) < 0.03
    # トラックの位置（offset）をずらしたテイク: 渡した位置の分だけタイムライン上で見る
    _, start_o, _, _ = SG.build(score, f0, 1.5, np.asarray(on), SONG_SEC + 2)
    assert abs(start_o - (start + 1.5)) < 0.011


def test_tempo_less_midi_uses_bpm(song):
    """テンポの無い MIDI は bpm（省略時はセッションのテンポ）で読み直す。無ければ 120 とみなして警告する。"""
    from vocal_engine import score_guide as SG, score_import
    score = score_import.read_score(song["mid0"])
    assert not SG.has_tempo(score)
    f0, on = _take_inputs(song["take"])
    _, s1, _, i1 = SG.build(score, f0, 0.0, on, SONG_SEC, bpm=BPM)
    assert abs(i1["fit"]["pitch_start_sec"] - START) < 0.03 and i1["tempo_source"] == "bpm"
    assert abs(i1["bpm"] - BPM) < 1e-6
    _, s2, _, i2 = SG.build(score, f0, 0.0, on, SONG_SEC, session_bpm=BPM)
    assert abs(i2["fit"]["pitch_start_sec"] - START) < 0.03 and i2["tempo_source"] == "session"
    _, s4, _, i4 = SG.build(score, f0, 0.0, on, SONG_SEC, bpm=BPM, start_sec=START)
    assert s4 == START and "onset_shift_ms" not in i4["fit"]
    _, _, _, i3 = SG.build(score, f0, 0.0, on, SONG_SEC)
    assert i3["tempo_source"] == "default_120" and i3["warnings"]
    with pytest.raises(SG.ScoreGuideError):
        SG.build(score, f0, 0.0, on, SONG_SEC, track="no such track")


def test_sidecar_and_clip_offset(song, tmp_path):
    """印（WAV の横の JSON）を読み、ガイドのクリップの頭の分だけ秒を引く。WAV を別のもので上書きしたら使わない。"""
    from vocal_engine import score_guide as SG
    notes = [{"start_sec": 0.0, "end_sec": 0.5, "pitch": 60, "lyric": "a"},
             {"start_sec": 0.5, "end_sec": 1.0, "pitch": 60, "lyric": "b"},
             {"start_sec": 1.25, "end_sec": 2.0, "pitch": 62, "lyric": ""}]
    y, played = SG.render(notes, 2.0, 5.0)
    assert len(played) == 3
    assert played[0][1] <= played[1][0] - SG.GAP_SEC + 1e-6       # 同じ高さが続く所に隙間（発音の頭）
    w = str(tmp_path / "g.wav")
    SG.write(w, y, played, {"start_sec": 2.0})
    d = SG.read(w)
    assert d is not None and len(d["notes"]) == 3
    g = SG.guide_notes(d, offset_frames=int(1.5 * SG.SR), sr=SG.SR, duration_sec=3.5)
    assert [n.id for n in g] == ["g000", "g001", "g002"]
    assert abs(g[0].start_sec - 0.5) < 1e-6 and g[2].pitch_midi == 62.0 and g[0].text == "a"
    sf.write(w, np.zeros(SG.SR, dtype="float32"), SG.SR)          # 別の WAV で上書き
    assert SG.read(w) is None


@pytest.fixture(scope="module")
def guided(song):
    """テイク + 譜面ガイドのプロジェクト（解析済み）。"""
    from vocal_engine import score_guide as SG, score_import
    from vocal_engine.project import Project
    score = score_import.read_score(song["mid"])
    f0, on = _take_inputs(song["take"])
    _, _, y, info = SG.build(score, f0, 0.0, on, SONG_SEC)
    w = str(song["dir"] / "score-guide.wav")
    SG.write(w, y, info["played"], info)
    p = Project.open(song["take"], w, project_dir=str(song["dir"] / "proj"))
    p.analyze()
    return p, info


def test_project_uses_score_notes(guided):
    """譜面ガイドのノート・発音の頭は譜面のノートそのもの、対応付けは同じ時間軸（ずれ 0）の帯。"""
    from vocal_engine.project import timing as TM
    p, info = guided
    gn = p.guide_notes
    assert len(gn) == len(info["played"])
    assert all(abs(n.start_sec - a) < 1e-3 and n.pitch_midi == pt for n, (a, b, pt, _) in zip(gn, info["played"]))
    assert np.allclose(p.onsets("guide"), [n.start_sec for n in gn], atol=1e-3)
    tl = p.alignment.info["timeline"]
    assert tl["same"] and tl.get("given") and p.alignment.info["band_sec"] == pytest.approx(0.3)
    # 歌っている所の対応付けは帯（±0.3 秒）の中
    mids = np.array([(n.start_sec + n.end_sec) / 2 for n in TM.pitched_notes(p)])
    assert np.max(np.abs(p.alignment.to_guide(mids) - mids)) <= 0.3 + 1e-6
    gt = p.guide_timing()
    assert gt.same_timeline and len(gt.pairs) >= 40
    _, pt, _ = TM.note_correspondence(p)
    tn = TM.pitched_notes(p)
    good = sum(1 for n in tn if pt.get(n.id) is not None and n.pitch_midi is not None
               and abs(pt[n.id].pitch_midi - n.pitch_midi) < 0.6)
    assert good >= 0.9 * len(tn), (good, len(tn))


def test_estimate_timeline(song):
    """同じ時間軸の判定: 声のある窓ごとのずれが全体のずれにそろう。短い素材（窓が足りない）は判定しない。"""
    from vocal_engine.analysis import align as AL
    from vocal_engine.audio import read_mono
    x, sr = read_mono(song["take"])
    sh = int(0.06 * sr)
    late = np.concatenate([np.zeros(sh, dtype=x.dtype), x[:-sh]])        # テイクが全体に 60 ms 遅い
    other = sing(_melody(seed=11), START, BPM, seed=9)                  # 別の旋律の「ガイド」
    r = AL.estimate_timeline(late, sr, x, sr)
    assert r["same"] and abs(r["offset_sec"] - 0.06) <= 0.011, r
    assert not AL.estimate_timeline(late, sr, other, SR)["same"]
    short = AL.estimate_timeline(x[:5 * sr], sr, x[:5 * sr], sr)
    assert not short["same"] and short["windows"] == 0
    ta, ga, _, info = AL.align_seconds(late, sr, x, sr, timeline=False)
    assert "timeline" not in info
    ta, ga, _, info = AL.align_seconds(late, sr, x, sr)
    assert info["timeline"]["same"] and info["band_sec"] == AL.BAND_SEC
    m = (ta > 1.0) & (ta < SONG_SEC - 1.0)
    assert np.max(np.abs((ta[m] - ga[m]) - 0.06)) <= AL.BAND_SEC + 0.02


@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    yield m, mt
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def test_mcp_make_score_guide(mcp, song, tmp_path):
    """make_score_guide: 合成音のガイドのトラックを足してガイドに指定する。undo でトラックごと消える。"""
    m, mt = mcp
    _ok(m.open_project(song["take"], project_dir=str(tmp_path / "mcp")))
    _ok(m.analyze_take(background=False))
    r = _ok(mt.make_score_guide(song["mid"]))
    assert r["score_track"]["name"] == "lead" and abs(r["fit"]["pitch_start_sec"] - START) < 0.03
    assert os.path.exists(r["path"]) and os.path.exists(r["path"] + ".score.json")
    assert r["guide"] == r["track"] and r["notes"] > 100
    tracks = _ok(mt.list_tracks())["tracks"]
    assert any(t["id"] == r["track"] and t["guide"] for t in tracks)
    a = _ok(m.analyze_take(background=False))
    assert a["alignment"]["dtw"]["timeline"]["given"]
    plan = _ok(m.plan_edit("guide"))
    assert plan["info"]["matched_notes"] >= 0.9 * len(_ok(m.list_notes(kind="note"))["notes"])
    _ok(mt.history_undo())
    assert [t["id"] for t in _ok(mt.list_tracks())["tracks"]] == [tracks[0]["id"]]
    assert mt.make_score_guide(str(tmp_path / "missing.mid"))["ok"] is False


def test_remeasure_onset_timing(mcp, song, tmp_path):
    """remeasure の onset_timing: 「ガイドに合わせる」と同じ発音の頭の組で、ガイドの頭とのずれを補正前後で返す。"""
    m, mt = mcp
    _ok(m.open_project(song["take"], project_dir=str(tmp_path / "rm")))
    _ok(m.analyze_take(background=False))
    _ok(mt.make_score_guide(song["mid"]))
    _ok(m.analyze_take(background=False))
    _ok(m.correct_to_guide(start_sec=0.0, end_sec=13.0, pitch_strength=0.0, timing_strength=1.0))
    r = _ok(m.remeasure(start_sec=0.0, end_sec=13.0, background=False))
    ot = r["onset_timing"]
    assert ot["pairs"] >= 15 and ot["after"]["n"] >= 0.8 * ot["pairs"], ot
    assert ot["after"]["abs_ms_median"] < ot["before"]["abs_ms_median"]
    assert ot["after"]["abs_ms_median"] <= 10.0, ot


def _tones(notes, sec, seed=1):
    """[(頭, 尻, 高さ)]（秒）→ 合成音（ビブラート付き）。"""
    y = np.zeros(int(sec * SR))
    for s, e, p in notes:
        i0, i1 = int(s * SR), int(e * SR)
        t = np.arange(i1 - i0) / SR
        f = 440.0 * 2 ** ((p - 69) / 12.0) * (1 + 0.004 * np.sin(2 * np.pi * 5.5 * t))
        ph = 2 * np.pi * np.cumsum(f) / SR
        env = np.minimum(1.0, np.minimum(t / 0.015, (t[-1] - t) / 0.02 + 1e-9))
        y[i0:i1] += 0.2 * sum(np.sin(k * ph) / k ** 1.3 for k in range(1, 12)) * env
    return y.astype("float32")


@pytest.fixture(scope="module")
def many(tmp_path_factory):
    """テイクは 1.0〜2.0 秒を 1 つの音（57.5）で歌い、ガイドはそこで 59→56→58→55 と動く（1 対多の組）。"""
    from vocal_engine.project import Project
    d = tmp_path_factory.mktemp("one_to_many")
    common = [(2.4 + 0.3 * i, 2.62 + 0.3 * i, 60 + (i % 5)) for i in range(20)]
    take = [(1.0, 2.0, 57.5)] + common
    guide = [(1.0, 1.25, 59), (1.25, 1.5, 56), (1.5, 1.75, 58), (1.75, 2.0, 55)] + common
    tw, gw = str(d / "take.wav"), str(d / "guide.wav")
    sf.write(tw, _tones(take, 9.0), SR, subtype="FLOAT")
    sf.write(gw, _tones(guide, 9.0), SR, subtype="FLOAT")
    p = Project.open(tw, gw, project_dir=str(d / "proj"))
    p.analyze()
    return p


def test_one_to_many_pitch_uses_frames(many):
    """1 対多の組: ノートの中心をずらす寄せ方は、組の 1 つの音ではなくフレームごとの差の中央値へ（補正の担当の報告）。"""
    from vocal_engine.project import timing as TM
    p = many
    n = next(x for x in TM.pitched_notes(p) if x.start_sec < 1.2 and x.end_sec > 1.8)
    plan = TM.plan_guide(p, [n.id], match_pitch_shape=False)
    hit = [r for r in plan.info["one_to_many"] if r["note"] == n.id]
    assert hit and len(hit[0]["guide"]) >= 3 and hit[0]["source"] == "frames", plan.info["one_to_many"]
    assert abs(plan.pitch[n.id]) < 100.0, plan.pitch          # 以前は最も重なる音（55 なら −250）へ寄せた


def test_plan_fit_flags_worse(many):
    """当てはまりの予測: 計画をわざと外す（頭を 150 ms 遅らせる・音程を 3 半音ずらす）と悪化として出る。"""
    from vocal_engine.project import guide_fit as GF
    from vocal_engine.project import timing as TM
    p = many
    ids = [n.id for n in TM.pitched_notes(p)]
    plan = TM.plan_guide(p, ids, match_pitch_shape=False)
    f = GF.plan_fit(p, plan, 1.0, 1.0, ids)
    assert f["timing"]["corr_after"] >= f["timing"]["corr_before"] - 0.01 and not f["timing"]["worse"]
    assert not f["pitch"]["worse"]
    bad = TM.plan_guide(p, ids, match_pitch_shape=False)
    bad.d = bad.d + 0.15
    victim = ids[5]
    bad.pitch[victim] = bad.pitch.get(victim, 0.0) + 300.0
    g = GF.plan_fit(p, bad, 1.0, 1.0, ids)
    assert g["timing"]["corr_after"] < g["timing"]["corr_before"] - GF.WORSE_CORR and g["timing"]["worse"]
    assert victim in [w["note"] for w in g["pitch"]["worse"]]


def test_large_offset_needs_support():
    """タイムラインから離れた全体のずれは、十分な組が支えるときだけ使う（声の少ないテイクで −22 秒と出た）。"""
    from vocal_engine.analysis import guide_timing as GT
    far = [("t%d" % i, 100 + i * 0.5, "g%d" % i, 100 + i * 0.5 + 22.6) for i in range(4)]
    near = [("u%d" % i, 50 + i * 0.7, "h%d" % i, 50 + i * 0.7 + 0.01) for i in range(3)]
    r = GT._model(GT.GuideTiming(offset_sec=0.0), far + near)
    assert r.basis == "timeline" and abs(r.measured_offset_sec + 0.01) < 0.005
    assert {p.take_id for p in r.pairs} == {c[0] for c in near}
    assert all(c[0] in r.skipped for c in far)
    # 置き場所の違う素材は以前どおり全体のずれを足した位置（3 秒以内なら組の数を問わない。3 秒を超えるなら支えが要る）
    for n, sh in ((4, 0.25), (12, 0.25), (12, 5.0)):
        moved = [("t%d" % i, 10 + i * 0.5, "g%d" % i, 10 + i * 0.5 + sh) for i in range(n)]
        m = GT._model(GT.GuideTiming(offset_sec=0.0), moved)
        assert m.basis == "offset" and abs(m.offset_sec + sh) < 0.005 and len(m.pairs) == n
