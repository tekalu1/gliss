# -*- coding: utf-8 -*-
"""歌詞 → 音素 → 強制アラインメント → 確信度 を通す本体。

流れ:

    歌詞テキスト
      -> g2p（pyopenjtalk-plus / 自前テーブル）
      -> merge_long_vowels()              ← 長音「ー」は直前の母音に吸収（最重要）
      -> HubertFA ONNX で強制アラインメント（CPU, RTF 0.025）
      -> ブレス（AP）を無音の中に差し込む
      -> 確信度（4 指標の合議）
      -> PhonemeResult（音素列＋境界列＋警告）

確信度は**アライナーが確率をくれない**ので合議で作る:

  1. エネルギーの裏づけ: 無声子音は隣の母音に対する**相対落差**（−12 dB 以下で満点）。
     段階2 のスパイクで最も効いた指標（絶対しきい値は叫び素材で破綻する）。
  2. V/UV の整合: 母音区間の有声率。
  3. 長さの妥当性: 10 ms 未満の音素、1 秒を超える母音は低く。
  4. 発声塊との整合: −50 dB を超える 40 ms 以上の塊の頭が
     音素境界に乗っているか。**塊の数と音節数は一致しない**（叫びでは数えすぎる）ので、
     数の食い違いは全体の確信度を下げる係数として効かせ、警告にも出す。
"""
from __future__ import annotations

import numpy as np

from .. import log
from . import g2p as G
from .hubertfa import AlignerSetupError, get_aligner
from .model import Boundary, PhonemeResult, PhonemeSpan, build_boundaries

BLOB_DB = -50.0                 # 発声塊のしきい値
BLOB_MIN_MS = 40.0
CLOSURE_DROP_DB = -12.0         # 「隣の母音に対する相対落差」の合格ライン
LONG_VOWEL_SEC = 1.0
SHORT_PHONEME_SEC = 0.010
PAD_SEC = 0.20                  # 区間ごとに切り出すときの前後の余白
# アラインの作り方の版。**区間の失敗を含む結果**のキャッシュは、この版が違えば取り直す
# （失敗の原因を直した版で開き直したとき、失敗したままの結果を使い続けないように。issue #58）。
# 失敗の無い結果は版によらず使う（歌詞ごとのキャッシュを無駄に捨てない）。
ALIGN_REVISION = 2              # 2: 促音 cl を無音のクラスとして渡す（issue #58）
CLOSURE_QUIET_DB = 15.0         # 促音の閉鎖 = 前の音素の山より これだけ下がった区間
CLOSURE_KEEP_PREV_SEC = 0.03    # 閉鎖を広げても前の音素に残す長さ
CLOSURE_KEEP_NEXT_SEC = 0.02    # 同じく次の音素（破裂・摩擦）に残す長さ


def align_lyrics(x, sr, lyrics, f0r=None, source="take", use_pyopenjtalk=True,
                 detect_breath=True, duration_sec=None):
    """波形と歌詞 -> `PhonemeResult`（素材全体を 1 区間として扱う従来の入口）。"""
    return align_lyrics_ranges(x, sr, lyrics, f0r=f0r, source=source,
                               use_pyopenjtalk=use_pyopenjtalk,
                               detect_breath=detect_breath, duration_sec=duration_sec)


def align_lyrics_ranges(x, sr, entries, f0r=None, source="take", use_pyopenjtalk=True,
                        detect_breath=True, duration_sec=None, pad_sec=PAD_SEC):
    """**区間ごと**にアラインして 1 本の `PhonemeResult` にまとめる。

    `entries` は `phoneme/lyrics.py` の形（文字列なら素材全体の 1 区間）。

      - 区間ごとに波形を切り出して HubertFA に掛ける。切り出しは前後 `pad_sec`
        （既定 200 ms）だけ広げる。頭の子音と語尾の解放が切れないようにするため。
        隣の区間には食い込まない。
      - 区間の**外は音素を付けない**。`SP`（無音）で埋める。
        「歌詞の無い発声」もここに入る（測るのは `list_notes` の仕事）。
      - 確信度・警告・境界は**まとめてから**付ける（素材全体の F0 とエネルギーを見るため）。
      - **区間のアラインが失敗しても全体は落とさない**（issue #58）。その区間は音素なし（`SP`）で続け、
        `warnings` に `align_failed` を入れる。重み・辞書が無いなど環境の問題（`AlignerSetupError`）は上げる。
    """
    from . import lyrics as LY
    entries = LY.normalize(entries)
    dur = float(duration_sec) if duration_sec is not None else len(x) / float(sr)
    if not entries:
        return PhonemeResult(source=source, lyrics="", kana="", phonemes=[], boundaries=[],
                             duration_sec=dur)
    ranges = LY.spans(entries, dur)
    al = get_aligner()

    spans, warnings, ent_info = [], [], []
    kana_parts, lab_parts, unknown, devoiced = [], [], [], []
    syll_base = 0
    elapsed = 0.0
    audio_sec = 0.0
    total_confs = []
    failed = []
    for i, (e, (s0, s1)) in enumerate(zip(entries, ranges)):
        lo = 0.0 if i == 0 else ranges[i - 1][1]
        hi = dur if i == len(entries) - 1 else ranges[i + 1][0]
        a = max(0.0, lo, s0 - pad_sec)
        b = min(dur, hi, s1 + pad_sec)
        ia, ib = int(round(a * sr)), int(round(b * sr))
        res = None
        try:
            seg = np.asarray(x)[ia:ib]
            if len(seg) < int(0.05 * sr):
                raise ValueError("歌詞の区間 %.3f〜%.3f 秒に音が無い" % (s0, s1))
            res = G.g2p(e.get("reading") or e["text"],
                        use_pyopenjtalk=use_pyopenjtalk, merge_long=True)
            out = al.align(seg, sr, res.romaji_seq, detect_breath=detect_breath)
            part = _attach_text(out["phonemes"], res, out.get("used_words"))
        except AlignerSetupError:
            raise
        except Exception as err:                         # noqa: BLE001  区間の失敗は区間に閉じ込める
            msg = str(err) or type(err).__name__
            log.get("phoneme").warning("歌詞の区間 %d（%.2f〜%.2f 秒 %r）の音素アラインに失敗: %s",
                                       i, s0, s1, e["text"][:30], msg)
            failed.append(i)
            warnings.append({"kind": "align_failed", "entry_index": i,
                             "start_sec": _r(s0), "end_sec": _r(s1), "error": msg,
                             "message": "%.2f〜%.2f 秒の歌詞の音素を切れなかった（%s）。"
                                        "この区間は音素なしで続ける" % (s0, s1, msg)})
            ent_info.append({"index": i, "text": e["text"],
                             "kana": res.kana if res is not None else None,
                             "start_sec": _r(s0), "end_sec": _r(s1),
                             "aligned_start_sec": None, "aligned_end_sec": None,
                             "window_sec": [_r(a), _r(b)], "n_phonemes": 0,
                             "failed": True, "error": msg})
            if res is not None:
                syll_base += len(res.syllables)
            continue
        if detect_breath and out["breaths"]:
            part = _insert_breaths(part, out["breaths"])
        off = ia / float(sr)
        for p in part:
            p.start_sec = round(p.start_sec + off, 4)
            p.end_sec = round(p.end_sec + off, 4)
        part = _trim_edges(part)
        for p in part:
            if p.syllable_index is not None:
                p.syllable_index += syll_base
        syll_base += len(res.syllables)
        elapsed += out["elapsed_sec"]
        audio_sec += (b - a)
        total_confs.append(out["total_confidence"])
        unknown += out["unknown_syllables"]
        devoiced += list(res.devoiced)
        kana_parts.append(res.kana)
        lab_parts.append(res.lab_line())
        spans += part
        ent_info.append({
            "index": i, "text": e["text"], "kana": res.kana,
            "start_sec": _r(s0), "end_sec": _r(s1),
            "aligned_start_sec": _r(part[0].start_sec) if part else None,
            "aligned_end_sec": _r(part[-1].end_sec) if part else None,
            "window_sec": [_r(a), _r(b)],
            "n_phonemes": len([p for p in part if p.label != "silence"]),
            "rtf": out["rtf"], "elapsed_sec": out["elapsed_sec"],
        })
        if not part:
            warnings.append({"kind": "empty_entry", "entry_index": i,
                             "message": "%.2f〜%.2f 秒の歌詞から音素が取れなかった"
                                        % (s0, s1)})

    spans = _fill_silence(spans, dur, source)
    spans = _renumber(spans, source)
    _refine_closures(spans, f0r)
    if unknown:
        warnings.append({"kind": "unknown_syllable",
                         "message": "辞書に無い音節を飛ばした: %s" % ", ".join(unknown)})
    global_mul, blob_info, blob_warn = _blob_check(spans, f0r, ranges)
    warnings += blob_warn
    _score(spans, f0r, float(np.mean(total_confs)) if total_confs else 0.5, global_mul)
    warnings += _length_warnings(spans)

    bounds = build_boundaries(spans, source=source)
    _score_boundaries(bounds, spans)

    voiced = [p for p in spans if p.label != "silence"]
    conf = float(np.mean([p.confidence for p in voiced])) if voiced else 0.0
    syll = _syllables_of(spans)
    aligner = dict(_al_info(al), revision=ALIGN_REVISION, failed_entries=failed,
                   rtf=round(elapsed / max(audio_sec, 1e-6), 4),
                   elapsed_sec=round(elapsed, 4),
                   total_confidence=(round(float(np.mean(total_confs)), 3)
                                     if total_confs else None),
                   entries=len(entries), aligned_sec=round(audio_sec, 3))
    aligner.update(blob_info)
    log.get("phoneme").info(
        "%s を %d 音素にアライン（区間 %d / 音 %.1f s / %.2f s / RTF %.3f / 確信度 %.2f）",
        source, len(voiced), len(entries), audio_sec, elapsed,
        elapsed / max(audio_sec, 1e-6), conf)
    return PhonemeResult(
        source=source, lyrics=LY.text_of(entries), kana=" ".join(kana_parts),
        phonemes=spans, boundaries=bounds, syllables=syll, warnings=warnings,
        confidence=conf, aligner=aligner, entries=ent_info,
        g2p={"source": "pyopenjtalk-plus" if G.pyopenjtalk_available() else "table",
             "lab": " | ".join(lab_parts),
             "n_syllables": len(syll), "merge_long_vowels": True,
             "devoiced": devoiced},
        duration_sec=dur)


def _r(v, nd=4):
    return None if v is None else round(float(v), nd)


def _al_info(al):
    return {"name": "hubertfa", "version": "v0.0.7",
            "device": (al.providers or ["?"])[0] if al.providers else "?",
            "model_dir": al.folder}


def _trim_edges(spans):
    """区間の外まで伸びた頭・尻の無音を落とす（隙間はあとで `SP` で埋める）。"""
    i, j = 0, len(spans)
    while i < j and spans[i].label == "silence":
        i += 1
    while j > i and spans[j - 1].label == "silence":
        j -= 1
    return spans[i:j]


def _fill_silence(spans, duration_sec, source):
    """区間と区間の間・素材の端を `SP`（無音）で埋める。**音素は付けない。**"""
    spans = sorted(spans, key=lambda p: p.start_sec)
    out = []
    pos = 0.0
    for p in spans:
        if p.start_sec - pos > 1e-4:
            out.append(_sp(pos, p.start_sec, source))
        out.append(p)
        pos = max(pos, p.end_sec)
    if duration_sec - pos > 1e-4:
        out.append(_sp(pos, duration_sec, source))
    return out


def _sp(a, b, source):
    return PhonemeSpan(id="", index=0, start_sec=round(a, 4), end_sec=round(b, 4),
                       text="SP", kana=None, romaji=None, label="silence",
                       detail="silence", confidence=0.6, source=source,
                       flags=["no_lyrics"])


def _syllables_of(spans):
    """画面の歌詞レーン用（かな 1 文字 = 1 ブロック）。音素列だけから作る。"""
    by = {}
    for p in spans:
        if p.syllable_index is None:
            continue
        d = by.setdefault(p.syllable_index, {"phoneme_indices": [], "start": p.start_sec,
                                             "end": p.end_sec, "kana": p.kana,
                                             "romaji": p.romaji})
        d["phoneme_indices"].append(p.index)
        d["start"] = min(d["start"], p.start_sec)
        d["end"] = max(d["end"], p.end_sec)
    out = []
    for n, si in enumerate(sorted(by)):
        d = by[si]
        out.append({"index": n, "kana": d["kana"], "romaji": d["romaji"],
                    "start_sec": round(d["start"], 4), "end_sec": round(d["end"], 4),
                    "phoneme_indices": d["phoneme_indices"]})
    return out


# ---------------------------------------------------------------- 文字の貼り直し
def _attach_text(raw, res, used=None):
    """アライナーの出力（SP 込み）に、g2p のかな・音節・ラベルを貼る。

    アライナーへ渡した音節列と返ってくる音素列は同じ順なので、
    **SP を除いた並び**で 1 対 1 に対応する。
    used: アライナーが使った音節の番号（辞書に無くて飛ばした音節を除く。`align()` の `used_words`）。
    続けて来た閉鎖（っっ）はアライナーで 1 つにまとまるので、出てこない閉鎖は読み飛ばす。
    """
    keep = None if used is None else set(used)
    lex = []                   # [(syllable_index, kana, romaji, phoneme)]
    for si, s in enumerate(res.syllables):
        if s.romaji in (G.SP, G.AP):
            continue
        if keep is not None and si not in keep:
            continue
        for ph in s.phonemes:
            lex.append((si, s.kana, s.romaji, ph))
    spans = []
    k = 0
    for r in raw:
        t = r["text"]
        if t in ("SP", "pau", ""):
            spans.append(_span(r, "SP", None, None, None))
            continue
        if t == "AP":
            spans.append(_span(r, "AP", None, None, None))
            continue
        while k < len(lex) and lex[k][3] != t and G.detail_of(lex[k][3]) == "closure":
            k += 1
        if k < len(lex):
            si, kana, romaji, expect = lex[k]
            if expect != t:
                log.get("phoneme").warning("音素が食い違う: 期待 %r / アライナー %r", expect, t)
            spans.append(_span(r, t, kana, romaji, si))
            k += 1
        else:
            spans.append(_span(r, t, None, None, None))
    return spans


def _span(r, text, kana, romaji, si):
    return PhonemeSpan(
        id="", index=0, start_sec=round(float(r["start"]), 4),
        end_sec=round(float(r["end"]), 4), text=text, kana=kana, romaji=romaji,
        label=G.label_of(text), detail=G.detail_of(text),
        confidence=float(r.get("confidence", 0.5)), syllable_index=si)


def _insert_breaths(spans, breaths):
    """ブレス（AP）を無音（SP）の中に差し込む。語の上には置かない。"""
    out = []
    for s in spans:
        if s.label != "silence" or s.text != "SP":
            out.append(s)
            continue
        pieces = [(s.start_sec, s.end_sec, None)]        # (start, end, breath | None)
        for b in breaths:
            nxt = []
            for a, z, tag in pieces:
                lo, hi = max(a, b["start"]), min(z, b["end"])
                if tag is not None or hi - lo < 0.06:
                    nxt.append((a, z, tag))
                    continue
                if a < lo:
                    nxt.append((a, lo, None))
                nxt.append((lo, hi, b))
                if hi < z:
                    nxt.append((hi, z, None))
            pieces = nxt
        for a, z, tag in pieces:
            if z - a < 1e-6:
                continue
            if tag is not None:
                out.append(PhonemeSpan(id="", index=0, start_sec=round(a, 4),
                                       end_sec=round(z, 4), text="AP", kana=None,
                                       romaji=None, label="breath", detail="breath",
                                       confidence=float(tag.get("confidence", 0.6)),
                                       flags=["auto_detected"]))
            else:
                out.append(PhonemeSpan(id="", index=0, start_sec=round(a, 4),
                                       end_sec=round(z, 4), text="SP", kana=None,
                                       romaji=None, label="silence", detail="silence",
                                       confidence=s.confidence))
    return out


def _refine_closures(spans, f0r):
    """促音の閉鎖（`cl`）を、実際に音が落ちている区間まで広げる。

    HubertFA の語彙では `cl` は無音と同じクラスで、歌では閉鎖の無音を前の母音の尻や次の子音
    （`k` など）の頭に配って、`cl` を 1 フレーム（10 ms）に潰すことが多い（issue #58 の実測:
    「い｜っ｜か」で閉鎖の無音が 150 ms あるのに `cl` は 9 ms、`k` が 168 ms）。段階2 の長音と同じ現象。
    閉鎖は無音なので伸縮してよい区間（`STRETCHABLE`）になり、ここを正しく取るほど編集が楽になる。

    前の音素の後半〜次の音素の終わりの手前で、前の音素の山から `CLOSURE_QUIET_DB` 以上落ちた
    フレームが続く区間のうち、アライナーの `cl` に最も近いものを閉鎖にする。見つからなければそのまま。
    """
    if f0r is None:
        return
    rms = np.asarray(f0r.rms_db, dtype="float64")
    hop = float(f0r.hop_s)
    n = len(rms)
    if n == 0:
        return
    for i in range(1, len(spans) - 1):
        c, a, b = spans[i], spans[i - 1], spans[i + 1]
        if c.detail != "closure" or a.label == "silence" or b.label == "silence":
            continue
        lo = a.start_sec + max(CLOSURE_KEEP_PREV_SEC, 0.5 * a.duration_sec)
        hi = b.end_sec - CLOSURE_KEEP_NEXT_SEC
        fa, fb = int(np.ceil(lo / hop)), int(np.floor(hi / hop))
        fa, fb = max(0, fa), min(n, fb)
        if fb - fa < 2:
            continue
        pa, pb = int(a.start_sec / hop), max(int(a.start_sec / hop) + 1, int(np.ceil(a.end_sec / hop)))
        peak = rms[max(0, pa):min(n, pb)]
        if not len(peak):
            continue
        quiet = rms[fa:fb] < float(np.max(peak)) - CLOSURE_QUIET_DB
        runs, k = [], 0
        while k < len(quiet):
            if not quiet[k]:
                k += 1
                continue
            j = k
            while j < len(quiet) and quiet[j]:
                j += 1
            runs.append(((fa + k) * hop, (fa + j) * hop))
            k = j
        if not runs:
            continue
        mid = 0.5 * (c.start_sec + c.end_sec)
        s0, s1 = min(runs, key=lambda r: 0.0 if r[0] <= mid <= r[1]
                     else min(abs(r[0] - mid), abs(r[1] - mid)))
        s0, s1 = min(s0, c.start_sec), max(s1, c.end_sec)
        if s1 - s0 <= c.duration_sec + 1e-6:
            continue
        c.start_sec, c.end_sec = round(s0, 4), round(s1, 4)
        a.end_sec, b.start_sec = c.start_sec, c.end_sec
        c.flags.append("closure_from_energy")


def _renumber(spans, source):
    spans = sorted([s for s in spans if s.end_sec > s.start_sec],
                   key=lambda s: s.start_sec)
    for i, s in enumerate(spans):
        s.index = i
        s.id = "ph%03d" % i
        s.source = source
    return spans


# ---------------------------------------------------------------- 確信度
def _frames(f0r, t0, t1, shrink=0.0):
    """区間を [t0, t1) のフレーム番号にする。shrink=0.2 で中央 60%。"""
    if f0r is None:
        return None, None
    d = t1 - t0
    a = int(round((t0 + d * shrink) / f0r.hop_s))
    b = int(round((t1 - d * shrink) / f0r.hop_s))
    n = len(f0r.rms_db)
    a = max(0, min(n - 1, a))
    b = max(a + 1, min(n, b))
    return a, b


def _rms(f0r, t0, t1, shrink=0.0):
    a, b = _frames(f0r, t0, t1, shrink)
    if a is None:
        return None
    seg = np.asarray(f0r.rms_db)[a:b]
    return float(np.max(seg)) if len(seg) else None


def _voiced_ratio(f0r, t0, t1, shrink=0.2):
    a, b = _frames(f0r, t0, t1, shrink)
    if a is None:
        return None
    seg = np.asarray(f0r.voiced, dtype=bool)[a:b]
    return float(np.mean(seg)) if len(seg) else None


def _score(spans, f0r, total_conf, global_mul):
    """4 指標の合議で `confidence` を上書きする。"""
    neighbours = _neighbour_vowel_rms(spans, f0r)
    for p in spans:
        c_align = float(np.clip(p.confidence ** (1.0 / 3.0), 0.0, 1.0)) if p.confidence > 0 \
            else float(np.clip(total_conf, 0.0, 1.0))
        c_energy = 0.6
        if f0r is not None:
            if p.detail in ("consonant_unvoiced", "closure"):
                here = _rms(f0r, p.start_sec, p.end_sec)
                near = neighbours.get(p.index)
                if here is not None and near is not None:
                    rel = here - near
                    c_energy = float(np.clip((0.0 - rel) / (0.0 - CLOSURE_DROP_DB), 0.15, 1.0))
                    if rel > 0:
                        p.flags.append("no_energy_drop")
            elif p.detail in ("vowel", "moraic_nasal", "consonant_voiced"):
                vr = _voiced_ratio(f0r, p.start_sec, p.end_sec)
                c_energy = 0.6 if vr is None else float(np.clip(vr, 0.05, 1.0))
            elif p.detail in ("silence", "breath"):
                vr = _voiced_ratio(f0r, p.start_sec, p.end_sec)
                c_energy = 0.7 if vr is None else float(np.clip(1.0 - vr, 0.1, 1.0))
        c_len = 1.0
        d = p.duration_sec
        if d < SHORT_PHONEME_SEC:
            c_len = 0.1
            p.flags.append("too_short")
        elif p.label == "vowel" and d > LONG_VOWEL_SEC:
            c_len = 0.5
            p.flags.append("long_vowel")
        p.confidence = round(float(np.clip(
            (0.25 * c_align + 0.45 * c_energy + 0.30 * c_len) * global_mul, 0.0, 1.0)), 3)


def _neighbour_vowel_rms(spans, f0r):
    """各音素の「隣の母音」の RMS（相対落差を測るため）。"""
    out = {}
    if f0r is None:
        return out
    for i, p in enumerate(spans):
        vals = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(spans) and spans[j].label == "vowel":
                r = _rms(f0r, spans[j].start_sec, spans[j].end_sec, shrink=0.2)
                if r is not None:
                    vals.append(r)
        if vals:
            out[p.index] = max(vals)
    return out


def _score_boundaries(bounds, spans):
    by = {p.index: p for p in spans}
    for b in bounds:
        cs = [by[i].confidence for i in (b.before_index, b.after_index)
              if i is not None and i in by]
        b.confidence = round(float(min(cs)) if cs else 0.5, 3)
        # フレーズの頭の子音・語尾の解放は人の判断
        if b.kind in ("onset", "offset"):
            b.confidence = round(b.confidence * 0.85, 3)


def _length_warnings(spans):
    out = []
    for p in spans:
        if p.label == "vowel" and p.duration_sec > LONG_VOWEL_SEC:
            out.append({
                "kind": "long_vowel_no_control_point", "phoneme_index": p.index,
                "message": "母音 %r が %.0f ms あり内部に制御点が無い。画面で足すことを勧める"
                           % (p.text, p.duration_sec * 1000)})
        if p.duration_sec < SHORT_PHONEME_SEC and p.label != "silence":
            out.append({"kind": "too_short", "phoneme_index": p.index,
                        "message": "音素 %r が %.1f ms しかない（アラインの失敗を疑う）"
                                   % (p.text, p.duration_sec * 1000)})
    return out


# ---------------------------------------------------------------- 発声塊
def _blobs(f0r):
    """−50 dB を超える 40 ms 以上の塊。"""
    if f0r is None:
        return []
    m = np.asarray(f0r.rms_db) > BLOB_DB
    hop = f0r.hop_s
    need = int(round(BLOB_MIN_MS / 1000.0 / hop))
    out = []
    i = 0
    while i < len(m):
        if not m[i]:
            i += 1
            continue
        j = i
        while j < len(m) and m[j]:
            j += 1
        if j - i >= need:
            out.append((i * hop, j * hop))
        i = j
    return out


def _blob_check(spans, f0r, ranges=None):
    """塊の数・塊の頭と音素境界の距離 → 全体の確信度の係数と警告。

    `ranges` は歌詞を付けた区間（`[(start, end), ...]`）。曲全体のテイクでは
    **歌詞の無いところの塊まで数えると数が合わなくなる**ので、
    区間に重なる塊と、区間に入っている音素だけを見る。
    """
    blobs = _blobs(f0r)
    if ranges:
        blobs = [b for b in blobs
                 if any(b[1] > s - 0.25 and b[0] < t + 0.25 for s, t in ranges)]
        spans = [p for p in spans
                 if any(p.end_sec > s - 0.25 and p.start_sec < t + 0.25 for s, t in ranges)]
    if not blobs or not spans:
        return 1.0, {}, []
    edges = sorted({round(p.start_sec, 4) for p in spans if p.label != "silence"}
                   | {round(p.end_sec, 4) for p in spans if p.label != "silence"})
    if not edges:                   # 音素が 1 つも無い（すべての区間のアラインが失敗した。issue #58）
        return 1.0, {}, []
    e = np.asarray(edges)
    d = [float(np.min(np.abs(e - b[0]))) * 1000.0 for b in blobs]
    med = float(np.median(d))
    # 発声のかたまり = 無音で区切られた音素のグループ
    groups = 0
    prev_silent = True
    for p in spans:
        silent = p.label == "silence"
        if prev_silent and not silent:
            groups += 1
        prev_silent = silent
    mul = float(np.clip(1.0 - max(0.0, med - 20.0) / 120.0, 0.6, 1.0))
    warn = []
    if groups and abs(len(blobs) - groups) > 0:
        # 段階2 の評価: 塊の数 = 音節数 にはならない。数え違いは確信度を下げる材料にとどめる。
        # **差の「割合」で効かせる**（実素材で 8 対 4 になり、個数で効かせると
        # 素材が長いほど無条件に 0.7 倍になってしまった）。
        rel = abs(len(blobs) - groups) / float(max(len(blobs), groups))
        mul *= float(np.clip(1.0 - 0.25 * rel, 0.8, 1.0))
        warn.append({
            "kind": "blob_count_mismatch",
            "message": "−50 dB の塊が %d 個、音素から見た発声のかたまりが %d 個で合わない"
                       "（叫びでは塊を数えすぎる）" % (len(blobs), groups)})
    if med > 60.0:
        warn.append({"kind": "blob_offset", "message":
                     "塊の頭と最寄りの音素境界の距離が中央値 %.0f ms。境界を見直すこと" % med})
    info = {"blobs": len(blobs), "voiced_groups": groups,
            "blob_head_to_boundary_median_ms": round(med, 1),
            "blob_head_to_boundary_max_ms": round(float(np.max(d)), 1),
            "confidence_multiplier": round(mul, 3)}
    return mul, info, warn
