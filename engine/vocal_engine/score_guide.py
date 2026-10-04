# -*- coding: utf-8 -*-
"""譜面（MIDI / SVP）からガイドの音を作る — ガイドの WAV が無い・悪い（息・囁きで音程が取れない）とき用。

譜面のノート列を、**時間の頭（譜面の 0 拍がタイムラインの何秒か）を推定して**タイムラインに置き、
ノートどおりの高さの合成音（倍音を持つ平らな音。ビブラート・しゃくり無し）の WAV にする。
その WAV をガイドのトラックにすれば、ガイドの解析（F0・ノート・発音の頭）・対応付け・「ガイドに合わせる」が
そのまま動く（`mcp_tracks.make_score_guide`）。

- 時間の頭: テイクの F0 と譜面の高さが合う時間が最も長い位置（オクターブ違いは半分の点。粗く 50 ms →
  細かく 10 ms。同点が続く所はその真ん中）。そのあと、譜面のノートの頭の近く（±`ONSET_TOL_SEC`）にあるテイクの発音の頭との差の
  中央値だけ寄せる（上限 `ONSET_MAX_SHIFT`）。譜面ガイドの発音の頭は譜面のノートの頭そのものだが、
  テイクの発音の頭は立ち上がりの検出（`analysis/onsets.py`）の位置で、音の頭より 15〜45 ms 後ろに出る
  （手元の曲・合成音で測った値）。寄せないと「ガイドに合わせる」がどの頭もその分だけ前へ動かす。
  `start_sec` を渡せばそれを使う（寄せない）。
- トラック: 指定が無ければ、テイクに最も合うトラック（主旋律・ハモリ・オクターブ違いを、高さの合う時間で選ぶ）。
- テンポ: 譜面のテンポ。テンポの無い MIDI（標準では 120 BPM とみなす）は bpm（省略時はセッションのテンポ）で読み直す。
- 合成音: 同じ高さが続くノートも別の音と分かるように、ノートの尻を `GAP_SEC` だけ切って立ち上がりを付ける。
"""
from __future__ import annotations

import numpy as np

SR = 48000
HOP = 0.01
GAP_SEC = 0.03             # 続くノートの間に空ける隙間（発音の頭を立てる）
ATTACK_SEC = 0.012
RELEASE_SEC = 0.015
HARMONICS = 16
LEVEL = 0.25
PITCH_TOL = 1.0            # テイクの高さが譜面からこれ（半音）以内なら「合う」
COARSE_STEP = 0.05
ONSET_TOL_SEC = 0.08       # 発音の頭で寄せるときに、譜面のノートの頭から探す範囲
ONSET_MAX_SHIFT = 0.06     # 発音の頭で寄せる量の上限
MIN_FIT = 0.3              # 高さの合う割合がこれ未満なら、時間の頭の推定は当てにならない（警告）


class ScoreGuideError(ValueError):
    pass


def score_notes(score, track=None):
    """(トラック, ノートの列) を選ぶ。track は番号か名前。省略時は None（呼ぶ側が合うものを選ぶ）。"""
    tracks = [t for t in score["tracks"] if t["notes"]]
    if not tracks:
        raise ScoreGuideError("譜面に音符が無い")
    if track is None:
        return tracks
    if isinstance(track, (int, np.integer)) or (isinstance(track, str) and track.isdigit()):
        sel = [t for t in tracks if t["index"] == int(track)]
    else:
        sel = [t for t in tracks if t["name"] == track]
    if not sel:
        raise ScoreGuideError("譜面のトラックが無い: %r（%s）" % (track, [(t["index"], t["name"]) for t in tracks]))
    return sel


def has_tempo(score):
    """譜面にテンポの指定があるか（MIDI のテンポのメタイベント・SVP のテンポ）。"""
    tempo = score.get("tempo") or []
    if score.get("format") == "svp":
        return True
    return not (len(tempo) == 1 and abs(float(tempo[0]["bpm"]) - 120.0) < 1e-6 and tempo[0]["beat"] == 0)


def retime(notes, scale):
    """テンポを読み直す（秒を scale 倍）。"""
    return [dict(n, start_sec=n["start_sec"] * scale, end_sec=n["end_sec"] * scale) for n in notes]


def _roll(notes, n, offset_frames=0):
    r = np.full(n, np.nan)
    for x in notes:
        a = int(round(x["start_sec"] / HOP)) + offset_frames
        b = int(round(x["end_sec"] / HOP)) + offset_frames
        a, b = max(0, a), min(n, b)
        if b > a:
            r[a:b] = float(x["pitch"])
    return r


def _fit_score(take, roll):
    """take（テイクの MIDI の高さ。無声は nan）と roll（譜面）の重なりの点（高さの合う時間）。"""
    both = ~np.isnan(take) & ~np.isnan(roll)
    if not both.any():
        return 0.0, 0, 0
    d = take[both] - roll[both]
    exact = np.abs(d) <= PITCH_TOL
    octave = (np.abs((d + 6.0) % 12.0 - 6.0) <= PITCH_TOL) & ~exact
    return float(exact.sum() + 0.5 * octave.sum() - 0.25 * (~exact & ~octave).sum()), int(exact.sum()), int(both.sum())


def fit_start(notes, take_midi, lo_sec, hi_sec):
    """譜面の 0 秒をタイムラインの何秒に置くと、テイクの高さと最も合うか。

    take_midi: タイムラインの 10 ms ごとのテイクの高さ（MIDI、無声は nan）。返り値 (秒, 情報)。"""
    n = len(take_midi)
    span = int(np.ceil(max(x["end_sec"] for x in notes) / HOP)) + 2
    base = _roll(notes, span)
    pad = np.full(span, np.nan)
    ext = np.concatenate([pad, np.asarray(take_midi, dtype="float64"), pad])   # 位置 k の譜面 = ext[span + k :]

    def score_at(k):
        seg = ext[span + k: span + k + span]
        if len(seg) < span:
            seg = np.concatenate([seg, np.full(span - len(seg), np.nan)])
        return _fit_score(seg, base)

    def search(ks):
        best = None
        for k in ks:
            sc = score_at(int(k))
            if best is None or sc[0] > best[1][0]:
                best = (int(k), sc)
        return best

    lo_k = max(-span, int(np.floor(lo_sec / HOP)))
    hi_k = min(n, int(np.ceil(hi_sec / HOP)))
    if hi_k <= lo_k:
        raise ScoreGuideError("時間の頭を探す範囲が無い")
    step = max(1, int(round(COARSE_STEP / HOP)))
    k0, _ = search(range(lo_k, hi_k + 1, step))
    # 細かく: 点が最高とほぼ同じ（差 0.5% 以内）位置が続くとき（テイクの音がノートより短いと、その分だけ
    # 動かしても点が変わらない）は、その範囲の真ん中
    fine = [(k, score_at(k)) for k in range(k0 - 2 * step, k0 + 2 * step + 1)]
    top = max(v[0] for _, v in fine)
    tied = [k for k, v in fine if v[0] >= top - max(1.0, 0.005 * abs(top))]
    k1 = int(round(float(np.median(tied))))
    sc = score_at(k1)
    voiced_score = int((~np.isnan(base)).sum())
    return k1 * HOP, {"matched_frames": sc[1], "overlap_frames": sc[2],
                      "fit": round(sc[1] / max(1, sc[2]), 3),
                      "covered": round(sc[2] / max(1, voiced_score), 3), "score": round(sc[0], 1)}


def refine_by_onsets(notes, start, onsets):
    """譜面のノートの頭の近くにあるテイクの発音の頭との差の中央値だけ寄せる（上限 `ONSET_MAX_SHIFT`）。"""
    on = np.sort(np.asarray(onsets, dtype="float64"))
    if not len(on):
        return start, 0
    d = []
    for x in notes:
        t = x["start_sec"] + start
        j = int(np.searchsorted(on, t))
        cand = [on[k] - t for k in (j - 1, j) if 0 <= k < len(on)]
        if cand:
            v = min(cand, key=abs)
            if abs(v) <= ONSET_TOL_SEC:
                d.append(v)
    if len(d) < 5:
        return start, len(d)
    shift = float(np.clip(np.median(d), -ONSET_MAX_SHIFT, ONSET_MAX_SHIFT))
    return start + shift, len(d)


def render(notes, start, duration_sec, sr=SR):
    """ノート列を合成音にする。(mono float32, 鳴らしたノート [(頭, 尻, 高さ, 歌詞)]（タイムラインの秒）)。
    ノートの位置 = 譜面の秒 + start。"""
    n = int(round(duration_sec * sr))
    y = np.zeros(n, dtype="float64")
    played = []
    ns = sorted(notes, key=lambda x: x["start_sec"])
    for i, x in enumerate(ns):
        s = x["start_sec"] + start
        e = x["end_sec"] + start
        if i + 1 < len(ns) and ns[i + 1]["start_sec"] + start - e < GAP_SEC:
            e = min(e, ns[i + 1]["start_sec"] + start - GAP_SEC)       # 続くノートの手前を切る
        a, b = max(0, int(round(s * sr))), min(n, int(round(e * sr)))
        if b - a < int(0.02 * sr):
            continue
        t = np.arange(b - a) / float(sr)
        f0 = 440.0 * 2.0 ** ((float(x["pitch"]) - 69.0) / 12.0)
        ph = 2.0 * np.pi * f0 * t
        tone = np.zeros(b - a)
        for h in range(1, HARMONICS + 1):
            if f0 * h > sr * 0.45:
                break
            tone += np.sin(h * ph) / h
        env = np.ones(b - a)
        na, nr = min(len(env), int(ATTACK_SEC * sr)), min(len(env), int(RELEASE_SEC * sr))
        if na:
            env[:na] = np.linspace(0.0, 1.0, na)
        if nr:
            env[-nr:] = np.minimum(env[-nr:], np.linspace(1.0, 0.0, nr))
        y[a:b] += LEVEL * tone * env
        played.append((a / float(sr), b / float(sr), float(x["pitch"]), x.get("lyric") or ""))
    return y.astype("float32"), played


def take_pitch_on_timeline(f0, offset_sec, duration_sec):
    """テイクの F0（テイクの 0 秒から）→ タイムラインの 10 ms ごとの MIDI の高さ（無声は nan）。"""
    n = int(np.ceil(duration_sec / HOP)) + 1
    out = np.full(n, np.nan)
    f = np.asarray(f0.f0, dtype="float64")
    v = np.asarray(f0.voiced).astype(bool) & (f > 0)
    t = np.arange(len(f)) * f0.hop_s + offset_sec
    k = np.round(t / HOP).astype(int)
    ok = v & (k >= 0) & (k < n)
    out[k[ok]] = 69.0 + 12.0 * np.log2(f[ok] / 440.0)
    return out


def build(score, take_f0, take_offset_sec, take_onsets, duration_sec, track=None, bpm=None,
          session_bpm=None, start_sec=None):
    """譜面 → (ノートの列, 時間の頭, 合成音, 情報)。

    take_f0: 編集対象のテイクの F0（`F0Result`）。take_offset_sec: そのトラックのタイムライン上の位置。
    take_onsets: テイクの発音の頭（テイクの秒）。duration_sec: 作るガイドの長さ（タイムラインの 0 秒から）。"""
    warnings = []
    scale = 1.0
    tempo_src = "score"
    if bpm is not None or not has_tempo(score):
        use = bpm if bpm is not None else session_bpm
        if use is None:
            warnings.append("譜面にテンポが無い（120 BPM とみなした）。bpm を渡すかセッションのテンポを決める")
            tempo_src = "default_120"
        else:
            if has_tempo(score) and len(score.get("tempo") or []) > 1:
                raise ScoreGuideError("テンポの変わる譜面は bpm で読み直せない")
            scale = float(score["tempo"][0]["bpm"]) / float(use)
            tempo_src = "bpm" if bpm is not None else "session"
    take = take_pitch_on_timeline(take_f0, take_offset_sec, duration_sec)
    fits = []
    for tr in score_notes(score, track):
        notes = retime(tr["notes"], scale)
        if start_sec is None:
            s, info = fit_start(notes, take, -max(x["end_sec"] for x in notes), duration_sec)
        else:
            s, info = float(start_sec), {}
        fits.append((info.get("score", 0.0), tr, notes, s, info))
    fits.sort(key=lambda f: f[0], reverse=True)
    _, tr, notes, start, info = fits[0]
    moved = 0
    if start_sec is None:
        on = np.asarray(take_onsets, dtype="float64") + take_offset_sec
        start2, moved = refine_by_onsets(notes, start, on)
        info["pitch_start_sec"] = round(start, 4)
        info["onset_shift_ms"] = round((start2 - start) * 1000.0, 1)
        start = start2
        if info.get("fit", 0) < MIN_FIT or info.get("covered", 0) < 0.05:
            warnings.append("譜面とテイクの高さがあまり合わない（fit %.2f）。トラック・テンポ・start_sec を確かめる"
                            % info.get("fit", 0))
    y, played = render(notes, start, duration_sec)
    out = {"played": played,"track": {"index": tr["index"], "name": tr["name"], "notes": len(notes)},
           "start_sec": round(start, 4), "tempo_source": tempo_src,
           "bpm": round(float(score["tempo"][0]["bpm"]) / scale, 4) if score.get("tempo") else None,
           "fit": info, "onset_pairs": moved, "warnings": warnings,
           "candidates": [{"index": f[1]["index"], "name": f[1]["name"], "score": f[0],
                           "start_sec": round(f[3], 4), "fit": f[4].get("fit")} for f in fits]}
    return notes, start, y, out


# ---------------------------------------------------------------- 譜面ガイドの印（WAV の横の JSON）
SIDECAR_SUFFIX = ".score.json"
FORMAT = "gliss-score-guide"


def sidecar_path(wav_path):
    return str(wav_path) + SIDECAR_SUFFIX


def write(wav_path, y, played, info, sr=SR):
    """合成音の WAV と、鳴らしたノートの印（`sidecar_path`）を書く。"""
    import json
    import os
    import soundfile as sf
    from .project.store import replace_file
    os.makedirs(os.path.dirname(os.path.abspath(wav_path)) or ".", exist_ok=True)
    tmp = str(wav_path) + ".tmp.wav"
    sf.write(tmp, y, sr, subtype="FLOAT")
    replace_file(tmp, wav_path)
    d = {"format": FORMAT, "version": 1, "sr": int(sr), "frames": int(len(y)),
         "notes": [[round(a, 6), round(b, 6), p, ly] for a, b, p, ly in played],
         "info": {k: v for k, v in info.items() if k != "played"}}
    tmp = sidecar_path(wav_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    replace_file(tmp, sidecar_path(wav_path))
    return d


_CACHE = {}


def read(wav_path):
    """譜面ガイドの印。無い・WAV と合わない（長さが違う = 別のファイルで上書きされた）なら None。"""
    import json
    import os
    sp = sidecar_path(wav_path)
    try:
        st = os.stat(sp)
        wst = os.stat(wav_path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
    hit = _CACHE.get(sp)
    if hit is not None and hit[0] == key:
        return hit[1]
    d = None
    try:
        with open(sp, encoding="utf-8") as f:
            d = json.load(f)
        import soundfile as sf
        inf = sf.info(wav_path)
        if d.get("format") != FORMAT or int(inf.frames) != int(d["frames"]) or int(inf.samplerate) != int(d["sr"]):
            d = None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        d = None
    _CACHE[sp] = (key, d)
    return d


def guide_notes(d, offset_frames=0, sr=None, duration_sec=None):
    """印のノート（WAV の秒）→ ガイドのノート（`Note`。ガイドのクリップの秒 = WAV の秒 − 切り出しの頭）。"""
    from .analysis.f0 import midi_to_name
    from .analysis.notes import Note
    sr = int(sr or d["sr"])
    off = float(offset_frames) / sr
    out = []
    for a, b, p, ly in d["notes"]:
        s, e = a - off, b - off
        if duration_sec is not None and (e <= 0.0 or s >= duration_sec):
            continue
        s, e = max(0.0, s), e if duration_sec is None else min(float(duration_sec), e)
        if e - s < 0.02:
            continue
        hz = 440.0 * 2.0 ** ((float(p) - 69.0) / 12.0)
        out.append(Note(id="g%03d" % len(out), start_sec=round(s, 4), end_sec=round(e, 4), kind="note",
                        label="sung", source="guide", text=ly or None, pitch_hz=round(hz, 3),
                        pitch_midi=float(p), note_name=midi_to_name(float(p)), iqr_semitones=0.0,
                        rms_peak_db=-12.0, confidence=1.0, start_frame=int(round(s / HOP)),
                        end_frame=int(round(e / HOP))))
    return out
