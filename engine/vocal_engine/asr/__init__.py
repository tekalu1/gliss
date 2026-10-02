# -*- coding: utf-8 -*-
"""区間の音声認識で歌詞を確かめる（issue #54）。

- 認識は**候補**を返すだけ。確定の歌詞は書き換えない（取り込むのは set_lyrics / set_note_syllable）。
- 重みは同梱しない。初回に利用者が確かめてからダウンロードする（`download.py`）。
- faster-whisper は任意の依存。無ければ `status()` が理由を返し、画面は「聞き取る」を無効にする。
"""
from __future__ import annotations

import re

from ..config import asr_models_dir
from . import catalog, download, recognize
from .catalog import DEFAULT_MODEL, AsrModel, size_text   # noqa: F401
from .recognize import AsrUnavailable, set_recognizer_factory   # noqa: F401

MAX_SEC = 120.0              # 1 回に聞き取る区間の上限
LOW_PROB = 0.5               # これ未満の語は「確信が低い」と知らせる
CAUTION = ("候補。確定の歌詞は変えていない。取り込むときは set_lyrics（区間全体）か "
           "set_note_syllable（1 音節）を呼ぶ")


def status(model_id="auto", probe=False):
    """使えるか・理由・モデルの一覧（大きさ・ライセンス・保存先・入っているか）。

    probe=True で、GPU で動くか（device: "cuda" / "cpu"、device_note: CPU になる理由）も調べる。
    """
    root = asr_models_dir()
    m = catalog.get(model_id, root)
    ok, reason = recognize.backend_available(m)
    out = {"available": ok, "reason": reason, "models_dir": root,
           "model": m.info(root), "models": [x.info(root) for x in catalog.models()],
           "installed": m.installed(root), "loaded": recognize.loaded(m.id),
           "install_hint": None if ok else recognize.INSTALL_HINT}
    if probe and ok:
        out["device"], out["device_note"] = recognize.expected_device(m)
    return out


def resolve(model_id="auto"):
    root = asr_models_dir()
    return catalog.get(model_id, root), root


def prepare(model: AsrModel, root, progress=None, cancel=None):
    ok, reason = recognize.backend_available(model)
    if not ok:
        raise AsrUnavailable(reason)
    return download.download(model, root, progress=progress, cancel=cancel)


# ---------------------------------------------------------------- 結果
def reading_of(text):
    """文字 → ひらがなの読み（長音・句読点の扱いは歌詞のアラインメントと同じ g2p）。"""
    from ..phoneme import g2p as G
    r = G.g2p(text)
    return G.katakana_to_hiragana("".join(s.kana for s in r.syllables
                                          if s.romaji not in (G.SP, G.AP)))


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        new = [i]
        for j, y in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (x != y)))
        row = new
    return row[-1]


def lyrics_candidate(text):
    """set_lyrics に渡す形（空白と、文末の句点・感嘆符などを落とす）。"""
    t = re.sub(r"\s+", "", text or "")
    return t.strip("。．.、，,")


def build_result(segs, start_sec, end_sec, model: AsrModel, device, device_note,
                 elapsed, current_entries=None):
    text = "".join(s["text"] for s in segs).strip()
    words = [{"text": w["text"].strip(), "start_sec": round(start_sec + w["start"], 3),
              "end_sec": round(start_sec + w["end"], 3),
              "probability": round(w["probability"], 3)}
             for s in segs for w in s["words"] if w["text"].strip()]
    segments = [{"start_sec": round(start_sec + s["start"], 3),
                 "end_sec": round(start_sec + s["end"], 3), "text": s["text"].strip(),
                 "avg_logprob": round(s["avg_logprob"], 3),
                 "no_speech_prob": round(s["no_speech_prob"], 3)} for s in segs]
    warnings = []
    kana = None
    if text:
        try:
            kana = reading_of(text)
        except Exception as e:              # noqa: BLE001  漢字が読めない（pyopenjtalk-plus が無い）など
            warnings.append("読み（かな）を出せない: %s" % str(e).splitlines()[0])
    else:
        warnings.append("何も聞き取れなかった（無音・息・伴奏だけの可能性）")
    if text and segs and max(s["no_speech_prob"] for s in segs) > 0.6:
        warnings.append("無音の可能性が高い所の認識。実際には無い言葉（幻覚）のおそれ")
    if kana and re.search(r"(.{1,4})\1{3,}", kana):
        warnings.append("同じ音の繰り返し。叫びや繰り返しは誤りが多い（評価で約 67%）ので要確認")
    if re.search(r"[A-Za-z]", text):
        warnings.append("日本語以外の文字を含む")
    low = [w for w in words if w["probability"] < LOW_PROB]
    if low:
        warnings.append("確信の低い語: %s" % "、".join(
            "%s（%.2f 秒）" % (w["text"], w["start_sec"]) for w in low[:8]))
    if device == "cpu":
        warnings.append("CPU で処理した（GPU より大幅に遅い）%s"
                        % ("。理由: %s" % device_note if device_note else ""))
    probs = [w["probability"] for w in words]
    confidence = None if not probs else {
        "value": round(sum(probs) / len(probs), 3), "min": round(min(probs), 3),
        "source": "whisper_word_probability", "calibrated": False,
        "note": "モデル内部の語の確率の平均。正解率ではない"}
    out = {"start_sec": round(start_sec, 4), "end_sec": round(end_sec, 4),
           "text": text, "lyrics": lyrics_candidate(text), "kana": kana,
           "words": words, "segments": segments, "confidence": confidence,
           "warnings": warnings, "model": model.id, "device": device,
           "elapsed_sec": round(elapsed, 3), "candidate": True, "note": CAUTION}
    if current_entries is not None:
        out["current"] = compare_current(kana, current_entries)
    return out


def compare_current(kana, entries):
    """その区間に今ある歌詞（確定・推定）と候補の読みの違い。"""
    if not entries:
        return {"entries": [], "kana": None, "char_errors": None, "char_error_rate": None,
                "same": None}
    readings = []
    for e in entries:
        try:
            readings.append(reading_of(e.get("reading") or e["text"]))
        except Exception:                   # noqa: BLE001
            readings.append("")
    cur = "".join(readings)
    errs = distance(kana or "", cur) if cur else None
    return {"entries": [{"start_sec": e["start_sec"], "end_sec": e["end_sec"],
                         "text": e["text"], "reading": e.get("reading"),
                         "origin": e.get("origin", "confirmed")} for e in entries],
            "kana": cur or None, "char_errors": errs,
            "char_error_rate": (round(errs / len(cur), 3) if cur else None),
            "same": (errs == 0) if cur else None}
