# -*- coding: utf-8 -*-
"""SVP / 歌詞付き Standard MIDI File を同じ音符列へ読む（ファイルは変更しない）。"""
from __future__ import annotations

import bisect
import json
import math
import struct
from pathlib import Path
from types import SimpleNamespace

from .phoneme.auto_lyrics import utterance_ranges

BLICK = 705_600_000  # Synthesizer V の 4 分音符


class ScoreImportError(ValueError):
    pass


def _tempo_clock(changes, units_per_beat, default_us=500_000):
    """拍位置→秒。テンポイベントが同位置にあれば最後の値を使う。"""
    points = {0: default_us}
    for tick, us in changes:
        if tick < 0 or us <= 0:
            raise ScoreImportError("テンポの位置または値が不正")
        points[int(tick)] = int(us)
    ticks = sorted(points)
    secs = [0.0]
    for left, right in zip(ticks, ticks[1:]):
        secs.append(secs[-1] + (right - left) * points[left] / (units_per_beat * 1_000_000))

    def at(tick):
        i = bisect.bisect_right(ticks, tick) - 1
        return secs[i] + (tick - ticks[i]) * points[ticks[i]] / (units_per_beat * 1_000_000)

    return at, [{"beat": t / units_per_beat, "bpm": round(60_000_000 / points[t], 6),
                 "sec": round(s, 6)} for t, s in zip(ticks, secs)]


def _note(start, end, pitch, lyric):
    if not all(math.isfinite(float(v)) for v in (start, end, pitch)) or end <= start:
        raise ScoreImportError("音符の位置・長さ・音高が不正")
    return {"start_sec": round(start, 6), "end_sec": round(end, 6),
            "pitch": int(pitch), "lyric": str(lyric or "").strip()}


def _svp_meter(events):
    """SVP の小節番号を、MIDI と共通の四分音符単位へ変える。"""
    out = []
    beat = 0.0
    prev_index = 0
    prev_num, prev_den = 4, 4
    for item in sorted(events, key=lambda x: int(x["index"])):
        index = int(item["index"])
        if index < prev_index:
            raise ScoreImportError("拍子の小節番号が不正")
        beat += (index - prev_index) * prev_num * 4 / prev_den
        prev_index = index
        prev_num, prev_den = int(item["numerator"]), int(item["denominator"])
        if prev_num <= 0 or prev_den <= 0:
            raise ScoreImportError("拍子の値が不正")
        out.append({"beat": beat, "numerator": prev_num, "denominator": prev_den})
    return out


def read_svp(path):
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ScoreImportError("SVP を読めない: %s" % e) from e
    if not isinstance(obj, dict) or not isinstance(obj.get("tracks"), list):
        raise ScoreImportError("SVP の tracks が無い")
    time = obj.get("time") or {}
    tempos = [(round(float(x["position"])), round(60_000_000 / float(x["bpm"])))
              for x in time.get("tempo", [])]
    clock, tempo = _tempo_clock(tempos, BLICK)
    origin = float(time.get("startTimeSeconds") or 0)
    lib = {g.get("uuid"): g for g in obj.get("library", [])}
    tracks = []
    for ti, track in enumerate(obj["tracks"]):
        notes = []
        refs = [(track.get("mainGroup") or {}, track.get("mainRef") or {})]
        refs += [(lib.get(ref.get("groupID"), {}), ref) for ref in track.get("groups", [])]
        for group, ref in refs:
            if ref.get("mute") or ref.get("isInstrumental"):
                continue
            offset = int(ref.get("blickOffset") or 0)
            begin = int(ref.get("blickAbsoluteBegin") or 0)
            end = int(ref.get("blickAbsoluteEnd") or -1)
            transpose = int(ref.get("pitchOffset") or 0)
            for n in group.get("notes", []):
                on = int(n["onset"]) + offset
                off = on + int(n["duration"])
                if on < begin or (end >= 0 and on >= end) or n.get("attributes", {}).get("muted"):
                    continue
                off = min(off, end) if end >= 0 else off
                notes.append(_note(clock(on) + origin, clock(off) + origin,
                                   int(n["pitch"]) + transpose, n.get("lyrics")))
        notes.sort(key=lambda n: (n["start_sec"], n["end_sec"]))
        tracks.append({"index": ti, "name": track.get("name") or "Track %d" % (ti + 1),
                       "notes": notes})
    return {"format": "svp", "tempo": tempo,
            "meter": _svp_meter(time.get("meter") or []), "tracks": tracks}


def _vlq(data, pos):
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise ScoreImportError("MIDI の可変長整数が途中で終わった")
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7f)
        if b < 0x80:
            return value, pos
    raise ScoreImportError("MIDI の可変長整数が長すぎる")


def _midi_events(raw):
    pos = tick = 0
    running = None
    while pos < len(raw):
        delta, pos = _vlq(raw, pos)
        tick += delta
        if pos >= len(raw):
            raise ScoreImportError("MIDI イベントが途中で終わった")
        status = raw[pos]
        if status < 0x80:
            if running is None:
                raise ScoreImportError("MIDI の running status が不正")
            status = running
        else:
            pos += 1
            running = status if status < 0xf0 else None
        if status == 0xff:
            if pos >= len(raw):
                raise ScoreImportError("MIDI メタイベントが途中で終わった")
            kind = raw[pos]
            pos += 1
            size, pos = _vlq(raw, pos)
            value = raw[pos:pos + size]
            pos += size
            if len(value) != size:
                raise ScoreImportError("MIDI メタイベントが途中で終わった")
            yield tick, "meta", kind, value
            if kind == 0x2f:
                break
        elif status in (0xf0, 0xf7):
            size, pos = _vlq(raw, pos)
            pos += size
            if pos > len(raw):
                raise ScoreImportError("MIDI SysEx が途中で終わった")
        elif status < 0xf0:
            kind = status & 0xf0
            size = 1 if kind in (0xc0, 0xd0) else 2
            value = raw[pos:pos + size]
            pos += size
            if len(value) != size:
                raise ScoreImportError("MIDI ノートが途中で終わった")
            yield tick, "channel", status, value
        else:
            raise ScoreImportError("未対応の MIDI ステータス: %02x" % status)


def _midi_text(raw):
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc).strip("\x00 \r\n")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1").strip("\x00 \r\n")


def read_midi(path):
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise ScoreImportError("MIDI を読めない: %s" % e) from e
    if len(data) < 14 or data[:4] != b"MThd":
        raise ScoreImportError("Standard MIDI File ではない")
    size = struct.unpack_from(">I", data, 4)[0]
    if size < 6 or len(data) < 8 + size:
        raise ScoreImportError("MIDI ヘッダーが不正")
    fmt, count, division = struct.unpack_from(">HHH", data, 8)
    if fmt not in (0, 1) or not division or division & 0x8000:
        raise ScoreImportError("MIDI は format 0/1・PPQ のみ対応")
    pos = 8 + size
    raw_tracks = []
    for _ in range(count):
        if data[pos:pos + 4] != b"MTrk" or pos + 8 > len(data):
            raise ScoreImportError("MIDI トラックが不正")
        length = struct.unpack_from(">I", data, pos + 4)[0]
        pos += 8
        if pos + length > len(data):
            raise ScoreImportError("MIDI トラックが途中で終わった")
        raw_tracks.append(list(_midi_events(data[pos:pos + length])))
        pos += length
    tempo_events = [(tick, int.from_bytes(v, "big")) for events in raw_tracks
                    for tick, typ, kind, v in events if typ == "meta" and kind == 0x51 and len(v) == 3]
    clock, tempo = _tempo_clock(tempo_events, division)
    meter = [{"beat": tick / division, "numerator": v[0], "denominator": 2 ** v[1]}
             for events in raw_tracks for tick, typ, kind, v in events
             if typ == "meta" and kind == 0x58 and len(v) >= 2]
    tracks = []
    for ti, events in enumerate(raw_tracks):
        name = "Track %d" % (ti + 1)
        lyric_events = []
        starts = {}
        pairs = []
        for tick, typ, kind, v in events:
            if typ == "meta":
                if kind == 0x03:
                    name = _midi_text(v) or name
                elif kind in (0x05, 0x01):
                    lyric_events.append((tick, _midi_text(v)))
            elif kind & 0xf0 == 0x90 and v[1] > 0:
                starts.setdefault((kind & 0x0f, v[0]), []).append(tick)
            elif kind & 0xf0 == 0x80 or (kind & 0xf0 == 0x90 and v[1] == 0):
                stack = starts.get((kind & 0x0f, v[0]))
                if stack:
                    on = stack.pop(0)
                    if tick > on:
                        pairs.append((on, tick, v[0]))
        pairs.sort()
        # 歌詞メタは音符の直前または同 tick が一般的。次の音符へ一度だけ付ける。
        notes = []
        li = 0
        pending = ""
        for on, off, pitch in pairs:
            while li < len(lyric_events) and lyric_events[li][0] <= on:
                pending = lyric_events[li][1]
                li += 1
            notes.append(_note(clock(on), clock(off), pitch, pending))
            pending = ""
        tracks.append({"index": ti, "name": name, "notes": notes})
    return {"format": "midi", "tempo": tempo, "meter": meter, "tracks": tracks}


def read_score(path):
    ext = Path(path).suffix.lower()
    if ext == ".svp":
        return read_svp(path)
    if ext in (".mid", ".midi"):
        return read_midi(path)
    raise ScoreImportError(".svp または .mid/.midi を指定する")


def phrases(notes):
    """譜面のフレーズを list_utterances と同じ発声区間の区切りで作る。"""
    if not notes:
        return []
    adapted = [SimpleNamespace(id=i, kind="note", start_sec=n["start_sec"],
                               end_sec=n["end_sec"]) for i, n in enumerate(notes)]
    return [[notes[i] for i in ids]
            for _, _, ids in utterance_ranges(adapted, notes[-1]["end_sec"] + .12)]


def lyric_text(notes):
    return "".join(n["lyric"] for n in notes if n["lyric"] not in ("-", "+", "br", "sil", "pau"))


def estimate_song_start(notes, take_notes, initial_sec, search_sec=8.0):
    """bext の候補を音高で検算する。明瞭な優位があるときだけ開始位置を直す。"""
    scored = [n for n in take_notes if n.kind == "note" and n.pitch_midi is not None
              and n.end_sec - n.start_sec >= .08]
    if len(scored) < 8 or len(notes) < 8:
        return initial_sec, {"source": "bext", "reason": "比較できる音符が少ない"}
    starts = [n["start_sec"] for n in notes]

    def quality(offset):
        hits = covered = 0
        boundary_error = 0.0
        for n in scored:
            t = (n.start_sec + n.end_sec) / 2 + offset
            i = bisect.bisect_right(starts, t) - 1
            if i < 0 or t >= notes[i]["end_sec"]:
                continue
            covered += 1
            diff = abs((notes[i]["pitch"] - n.pitch_midi + 6) % 12 - 6)
            if diff < 1.5:
                hits += 1
                boundary_error += abs(notes[i]["start_sec"] - (n.start_sec + offset))
        return hits / len(scored), covered / len(scored), boundary_error / max(1, hits)

    base_quality, base_coverage, _ = quality(initial_sec)
    choices = []
    for step in range(-int(search_sec * 20), int(search_sec * 20) + 1):
        offset = initial_sec + step * .05
        q, coverage, boundary = quality(offset)
        choices.append((q, coverage, -boundary, -abs(offset - initial_sec), offset))
    best = max(choices)
    for step in range(-10, 11):
        offset = best[4] + step * .005
        q, coverage, boundary = quality(offset)
        best = max(best, (q, coverage, -boundary, -abs(offset - initial_sec), offset))
    result = {"source": "bext", "bext_pitch_match": round(base_quality, 3),
              "best_pitch_match": round(best[0], 3), "best_coverage": round(best[1], 3)}
    if best[0] >= .65 and best[1] >= .65 and best[0] - base_quality >= .18:
        result["source"] = "score_pitch"
        return round(best[4], 4), result
    if base_quality < .45:
        result["reason"] = "譜面と録音の音高が十分に一致しない"
    return initial_sec, result


def match_to_take(notes, take_notes, onsets, duration_sec, song_start_sec):
    """譜面を録音の有声部へ写し、根拠のない区間は保留する。

    bext で曲の原点を置き、声がある音符だけ残す。検出した発音の頭の差の
    中央値で各フレーズを微修正する。大きい位置の違いは自動では推測しない。
    """
    voiced = [n for n in take_notes if n.kind == "note" and n.end_sec > n.start_sec]
    onset_times = sorted(float(x) for x in onsets)
    grouped = phrases(notes)
    take_utterances = utterance_ranges(take_notes, duration_sec)
    voiced_by_id = {n.id: n for n in voiced}
    entries, matched, unmatched = [], [], []
    for pi, group in enumerate(grouped):
        score_start = group[0]["start_sec"] - song_start_sec
        score_end = group[-1]["end_sec"] - song_start_sec
        choices = [(max(0.0, min(score_end, b) - max(score_start, a)),
                    -abs((a + b) / 2 - (score_start + score_end) / 2), ui, a, b, ids)
                   for ui, (a, b, ids) in enumerate(take_utterances)]
        best = max(choices, default=None)
        if best is None or best[0] <= 0:
            unmatched.append({"phrase": pi, "start_sec": round(group[0]["start_sec"], 4),
                              "end_sec": round(group[-1]["end_sec"], 4), "reason": "対応する発声がない"})
            continue
        _, _, utterance_index, utterance_start, utterance_end, utterance_ids = best
        local_voiced = [voiced_by_id[i] for i in utterance_ids if i in voiced_by_id]
        local_onsets = [t for t in onset_times if utterance_start <= t <= utterance_end]
        pieces = []
        run = []
        for n in group:
            s, e = n["start_sec"] - song_start_sec, n["end_sec"] - song_start_sec
            active = 0 <= e and s < duration_sec and any(
                x.start_sec < e + 0.10 and x.end_sec > s - 0.10 for x in local_voiced)
            if active:
                run.append(n)
            elif run:
                pieces.append(run)
                run = []
        if run:
            pieces.append(run)
        if not pieces:
            unmatched.append({"phrase": pi, "start_sec": round(group[0]["start_sec"], 4),
                              "end_sec": round(group[-1]["end_sec"], 4), "reason": "対応する発声がない"})
            continue
        for piece in pieces:
            text = lyric_text(piece)
            if len(piece) < 2 or not text:
                unmatched.append({"phrase": pi, "start_sec": round(piece[0]["start_sec"], 4),
                                  "end_sec": round(piece[-1]["end_sec"], 4), "reason": "歌詞または音符が不足"})
                continue
            # 音高は歌唱法やオクターブで違う場合があるので、信頼度の情報として返す。
            pitch_hits = 0
            for n in piece:
                t = (n["start_sec"] + n["end_sec"]) / 2 - song_start_sec
                near = [x for x in local_voiced if x.start_sec - .08 <= t < x.end_sec + .08
                        and x.pitch_midi is not None]
                if near and min(abs((n["pitch"] - x.pitch_midi + 6) % 12 - 6) for x in near) < 2:
                    pitch_hits += 1
            # 譜面の歌詞付き音符の頭 ↔ 実音のオンセットを最短で組む。
            diffs = []
            for n in piece:
                if not n["lyric"] or n["lyric"] in ("-", "+", "br", "sil", "pau"):
                    continue
                t = n["start_sec"] - song_start_sec
                i = bisect.bisect_left(local_onsets, t)
                candidates = local_onsets[max(0, i - 1):i + 1]
                if candidates:
                    d = min((x - t for x in candidates), key=abs)
                    if abs(d) <= .18:
                        diffs.append(d)
            diffs.sort()
            correction = diffs[len(diffs) // 2] if len(diffs) >= 2 else 0.0
            residual = sorted(abs(d - correction) for d in diffs)
            median_residual = residual[len(residual) // 2] if residual else None
            # 頭だけは密な別フレーズとも偶然合う。音高の 8 割一致を要求する。
            confidence = (len(diffs) >= 2 and pitch_hits >= 2 and
                          pitch_hits / len(piece) >= .80 and
                          (median_residual is None or median_residual <= .09))
            if not confidence:
                unmatched.append({"phrase": pi, "start_sec": round(piece[0]["start_sec"], 4),
                                  "end_sec": round(piece[-1]["end_sec"], 4),
                                  "reason": "発音の頭と音高の対応が不足",
                                  "onset_pairs": len(diffs), "pitch_hits": pitch_hits})
                continue
            start = max(0.0, piece[0]["start_sec"] - song_start_sec + correction)
            end = min(duration_sec, piece[-1]["end_sec"] - song_start_sec + correction)
            if end - start < .05:
                continue
            # 装飾的な音符やフレーズが接していても、set_lyrics の区間は重ねない。
            if entries and start < entries[-1]["end_sec"]:
                start = entries[-1]["end_sec"]
            if end - start < .05:
                continue
            entries.append({"start_sec": round(start, 4), "end_sec": round(end, 4),
                            "text": text})
            matched.append({"phrase": pi, "start_sec": round(start, 4),
                            "end_sec": round(end, 4), "score_start_sec": round(piece[0]["start_sec"], 4),
                            "utterance_index": utterance_index,
                            "utterance_start_sec": utterance_start,
                            "utterance_end_sec": utterance_end,
                            "note_ids": utterance_ids,
                            "notes": len(piece), "onset_pairs": len(diffs),
                            "onset_shift_ms": round(correction * 1000, 1),
                            "onset_median_residual_ms": round(median_residual * 1000, 1),
                            "pitch_hits": pitch_hits})
    return entries, matched, unmatched
