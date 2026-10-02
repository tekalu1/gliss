# -*- coding: utf-8 -*-
"""音符のかたまり（F0 ベースの簡易分割）。

段階1では openvpi/GAME は使わない（重みが CC BY-NC-SA 4.0 = 非商用のため）。代わりに F0 だけで切る:

  1. 有声区間を取り出す（V/UV は f0.py の判定）
  2. 区間内を「直近 15 フレームの中央値からの逸脱が `split_cents` を
     `persist` フレーム続いたら切る」で分割（ビブラート ±50 c では切れない）
  3. `min_note_ms` 未満の断片は隣の近い方へ併合
  4. 無声側の区間もラベル付きで返す（silence / breath / unvoiced）

ラベルの基準（素材の調査で決めたもの）:
  歌唱 = 連続有声 >= 200 ms かつ F0 の IQR <= 9 半音
  有声（セリフ・叫び） = IQR がそれより開く
  囁き・息 = 無声で RMS ピーク < -35 dBFS
"""
from dataclasses import dataclass, asdict

import numpy as np


from .f0 import hz_to_midi, midi_to_name

SPLIT_CENTS = 100.0        # 1 半音
PERSIST_FRAMES = 3         # 30 ms 続いたら切る
MIN_NOTE_MS = 80.0
MIN_VOICED_MS = 50.0
SUNG_MIN_MS = 200.0
SUNG_MAX_IQR_ST = 9.0
WHISPER_MAX_DB = -35.0
SILENCE_MAX_DB = -55.0
LONG_UNVOICED_SEC = 2.0    # これより長い無声区間は、静かなところで割る
QUIET_MIN_SEC = 0.5        # 「静か」と認める最短の長さ


@dataclass
class Note:
    id: str
    start_sec: float
    end_sec: float
    kind: str                 # note | unvoiced | breath | silence
    label: str                # sung | voiced_unstable | short | breath | silence | unvoiced
    source: str               # take | guide
    text: str | None          # 歌詞・音素（段階1では常に None）
    pitch_hz: float | None
    pitch_midi: float | None
    note_name: str | None
    iqr_semitones: float | None
    rms_peak_db: float
    confidence: float
    start_frame: int
    end_frame: int

    @property
    def duration_sec(self):
        return self.end_sec - self.start_sec

    def to_json(self):
        d = asdict(self)
        d["duration_sec"] = round(self.duration_sec, 4)
        return d


def _runs(mask):
    """bool 列 → [(start, end_exclusive, value), ...]"""
    mask = np.asarray(mask, dtype=bool)
    out = []
    if len(mask) == 0:
        return out
    s = 0
    cur = bool(mask[0])
    for i in range(1, len(mask)):
        if bool(mask[i]) != cur:
            out.append((s, i, cur))
            s, cur = i, bool(mask[i])
    out.append((s, len(mask), cur))
    return out


def _smooth_voiced(voiced, min_frames):
    """短すぎる有声／無声の断片を潰す（細切れの音符を防ぐ）。"""
    v = np.asarray(voiced, dtype=bool).copy()
    changed = True
    while changed:
        changed = False
        for s, e, val in _runs(v):
            if e - s < min_frames and not (s == 0 and e == len(v)):
                v[s:e] = not val
                changed = True
                break
    return v


def _split_run(cents, s, e, split_cents, persist, min_frames):
    """1 つの有声区間を音符に切る。"""
    segs = []
    seg_start = s
    dev = 0
    for i in range(s + 1, e):
        ref_from = max(seg_start, i - 15)
        ref = float(np.median(cents[ref_from:i])) if i > ref_from else cents[i]
        if abs(cents[i] - ref) > split_cents:
            dev += 1
            if dev >= persist:
                cut = i - dev + 1
                if cut - seg_start >= min_frames:
                    segs.append((seg_start, cut))
                    seg_start = cut
                dev = 0
        else:
            dev = 0
    segs.append((seg_start, e))
    return segs


def _merge_short(segs, cents, min_frames):
    """min_frames 未満の断片を、中央値の近い隣へ併合する。"""
    segs = list(segs)
    while len(segs) > 1:
        lens = [e - s for s, e in segs]
        i = int(np.argmin(lens))
        if lens[i] >= min_frames:
            break
        med = float(np.median(cents[segs[i][0]:segs[i][1]]))
        cands = []
        if i > 0:
            cands.append((abs(med - float(np.median(cents[segs[i - 1][0]:segs[i - 1][1]]))), i - 1))
        if i < len(segs) - 1:
            cands.append((abs(med - float(np.median(cents[segs[i + 1][0]:segs[i + 1][1]]))), i + 1))
        _, j = min(cands)
        a, b = sorted((i, j))
        segs[a] = (segs[a][0], segs[b][1])
        segs.pop(b)
    return segs


def segment_notes(f0res, source="take", include_unvoiced=True,
                  split_cents=SPLIT_CENTS, persist=PERSIST_FRAMES,
                  min_note_ms=MIN_NOTE_MS, min_voiced_ms=MIN_VOICED_MS,
                  id_prefix="n"):
    """F0Result → [Note]。時間順、範囲は重ならない。"""
    f0 = np.asarray(f0res.f0, dtype="float64")
    hop = f0res.hop_s
    n = len(f0)
    cents = np.full(n, np.nan)
    v = np.asarray(f0res.voiced, dtype=bool) & (f0 > 0)
    cents[v] = 1200.0 * np.log2(f0[v] / 10.0)
    # 無声を跨がない 3 点メディアンで、1 フレームの外れを落とす
    cs = cents.copy()
    for i in range(1, n - 1):
        if v[i] and v[i - 1] and v[i + 1]:
            cs[i] = float(np.median(cents[i - 1:i + 2]))
    cents = cs

    min_frames = max(1, int(round(min_note_ms / 1000.0 / hop)))
    min_voiced = max(1, int(round(min_voiced_ms / 1000.0 / hop)))
    vs = _smooth_voiced(v, min_voiced)
    # 平滑化で有声になったフレームの cents を埋める（近傍から）
    idx = np.arange(n)
    if v.any():
        cents = np.where(np.isnan(cents),
                         np.interp(idx, idx[v], cents[v], left=cents[v][0], right=cents[v][-1]),
                         cents)
    else:
        cents = np.zeros(n)

    rms = np.asarray(f0res.rms_db, dtype="float64")
    spans = []
    for s, e, is_v in _runs(vs):
        if is_v:
            segs = _merge_short(_split_run(cents, s, e, split_cents, persist, min_frames),
                                cents, min_frames)
            spans.extend([(a, b, True) for a, b in segs])
        elif (e - s) * hop >= LONG_UNVOICED_SEC:
            # 曲全体のテイクでは無声区間が 140 秒続く。**最大値**でラベルを決めると、
            # その中の物音 1 つで 140 秒ぜんぶが「息」になり、画面が斜線で埋まる。
            # 静かなところで割って、静かなぶんは silence にする。
            spans.extend([(a, b, False) for a, b in _split_quiet(rms, s, e, hop)])
        else:
            spans.append((s, e, False))

    conf = np.asarray(f0res.confidence, dtype="float64")
    notes = []
    k = 0
    for s, e, is_v in spans:
        if not is_v and not include_unvoiced:
            continue
        k += 1
        nid = "%s%03d" % (id_prefix, k)
        peak_db = float(np.max(rms[s:e])) if e > s else -120.0
        dur_ms = (e - s) * hop * 1000.0
        if is_v:
            fv = f0[s:e][f0[s:e] > 0]
            if len(fv) == 0:
                fv = np.array([np.nan])
            midi = hz_to_midi(fv)
            q1, q3 = (np.percentile(midi, [25, 75]) if np.isfinite(midi).all()
                      else (np.nan, np.nan))
            iqr = float(q3 - q1)
            med_midi = float(np.median(midi))
            label = ("sung" if (dur_ms >= SUNG_MIN_MS and iqr <= SUNG_MAX_IQR_ST)
                     else ("short" if dur_ms < SUNG_MIN_MS else "voiced_unstable"))
            # 確信度: F0 の確信度 × 安定度 × 長さ
            c_f0 = float(np.mean(conf[s:e])) if e > s else 0.0
            stab = float(np.exp(-max(iqr, 0.0) / 3.0)) if np.isfinite(iqr) else 0.3
            dur_f = min(1.0, dur_ms / SUNG_MIN_MS)
            confidence = round(float(np.clip(c_f0 * (0.5 + 0.5 * stab) * (0.6 + 0.4 * dur_f), 0, 1)), 3)
            notes.append(Note(
                id=nid, start_sec=round(s * hop, 4), end_sec=round(e * hop, 4),
                kind="note", label=label, source=source, text=None,
                pitch_hz=round(float(np.median(fv)), 3), pitch_midi=round(med_midi, 3),
                note_name=midi_to_name(med_midi), iqr_semitones=round(iqr, 3),
                rms_peak_db=round(peak_db, 2), confidence=confidence,
                start_frame=int(s), end_frame=int(e)))
        else:
            if peak_db < SILENCE_MAX_DB:
                kind, label = "silence", "silence"
            elif peak_db < WHISPER_MAX_DB:
                kind, label = "breath", "breath"
            else:
                kind, label = "unvoiced", "unvoiced"
            notes.append(Note(
                id=nid, start_sec=round(s * hop, 4), end_sec=round(e * hop, 4),
                kind=kind, label=label, source=source, text=None,
                pitch_hz=None, pitch_midi=None, note_name=None, iqr_semitones=None,
                rms_peak_db=round(peak_db, 2), confidence=0.6,
                start_frame=int(s), end_frame=int(e)))
    return notes


def _split_quiet(rms, s, e, hop, min_db=SILENCE_MAX_DB, min_sec=QUIET_MIN_SEC):
    """長い無声区間を「しっかり静かなところ」で割る。[(a, b), ...] を返す。"""
    need = max(1, int(round(min_sec / hop)))
    quiet = np.asarray(rms[s:e], dtype="float64") < min_db
    out = []
    cur = s
    for a, b, is_q in _runs(quiet):
        if not is_q or (b - a) < need:
            continue
        if a + s > cur:
            out.append((cur, a + s))
        out.append((a + s, b + s))
        cur = b + s
    if cur < e:
        out.append((cur, e))
    return out or [(s, e)]


def notes_in_range(notes, start_sec=None, end_sec=None, kinds=None):
    s = -1e18 if start_sec is None else float(start_sec)
    e = 1e18 if end_sec is None else float(end_sec)
    out = [n for n in notes if n.end_sec > s and n.start_sec < e]
    if kinds:
        out = [n for n in out if n.kind in kinds]
    return out
