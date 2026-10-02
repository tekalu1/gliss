# -*- coding: utf-8 -*-
"""音素（段階2で実装）。

  歌詞（かな・カナ・漢字混じり）
    → `phoneme/g2p.py`（pyopenjtalk-plus / 自前テーブル。**長音「ー」は直前の母音に吸収**）
    → `phoneme/hubertfa.py`（HubertFA v0.0.7 ONNX、Apache-2.0、CPU で RTF 0.025）
    → `phoneme/analyze.py`（確信度と警告）

返り値のデータモデルは Note と同じ「時間範囲＋音源＋テキスト＋ラベル＋確信度」。
境界は `boundaries` として**別配列**で返す。

重みは同梱しない（HubertFA v0.0.7 の重みにライセンス表記が無いため）。
無いときは入手元を含むエラーになる（`phoneme/hubertfa.py: missing_model_message`）。
"""
from ..phoneme.g2p import LABELS, pyopenjtalk_available
from ..phoneme.hubertfa import (DOWNLOAD_URL, HUBERTFA_DIR, missing_model_message,
                                model_found)

SUPPORTED = True
BACKEND = "HubertFA v0.0.7 (ONNX, Apache-2.0)"


def backend_info():
    return {
        "supported": True,
        "aligner": BACKEND,
        "model_dir": HUBERTFA_DIR,
        "model_found": model_found(),
        "download": DOWNLOAD_URL,
        "g2p": "pyopenjtalk-plus" if pyopenjtalk_available() else "内蔵テーブル（かなのみ）",
        "labels": list(LABELS),
    }


def get_phonemes(project=None, start_sec=None, end_sec=None, source="take", limit=2000):
    """範囲の音素と境界を返す（MCP `get_phonemes` の中身）。"""
    if project is None:
        return {"supported": True, "phonemes": [], "boundaries": [],
                "reason": "プロジェクトが開かれていない（open_project を先に）",
                **backend_info()}
    if not project.has_lyrics(source):
        return {
            "supported": True, "phonemes": [], "boundaries": [], "lyrics": None,
            "reason": "%s に歌詞が入っていない。`set_lyrics(text)` で与えると"
                      "音素アラインメントが走る（漢字混じりでもよい）" % source,
            "alternative": "list_notes で音符のかたまりを取る（V/UV ベース）",
            **backend_info()}
    if not model_found():
        raise RuntimeError(missing_model_message())
    res = project.phonemes(source)
    if res is None:
        return {"supported": True, "phonemes": [], "boundaries": [],
                "reason": "音素がまだ解析されていない（analyze_take を実行する）",
                **backend_info()}

    ph, bd = res.in_range(start_sec, end_sec)
    hidden = set()
    if source == "take":
        from ..phoneme.lyrics import hidden_estimated_syllable_indices
        hidden = hidden_estimated_syllable_indices(
            project.lyrics_entries("take"), res.syllables, project.take_notes,
            project.estimated_excluded_note_ids())
        if hidden:
            ph = [p for p in ph if p.syllable_index not in hidden]
            visible = {p.index for p in res.phonemes if p.syllable_index not in hidden}
            bd = [b for b in bd if
                  (b.before_index is None or b.before_index in visible) and
                  (b.after_index is None or b.after_index in visible)]
    total = len(ph)
    ph = ph[:limit]
    src, out = project.time_map()
    import numpy as np
    return {
        "supported": True,
        "source": source,
        "lyrics": res.lyrics,
        "kana": res.kana,
        "range_sec": [start_sec, end_sec],
        "aligner": res.aligner,
        "g2p": res.g2p,
        "confidence": round(res.confidence, 3),
        "total": total,
        "returned": len(ph),
        "truncated": total > len(ph),
        "phonemes": [p.to_json() for p in ph],
        "syllables": [s for s in res.syllables
                      if (start_sec is None or s["end_sec"] > start_sec)
                      and (end_sec is None or s["start_sec"] < end_sec)
                      and s["index"] not in hidden],
        "boundaries": [dict(b.to_json(),
                            edited_sec=round(float(np.interp(b.time_sec, src, out)), 4))
                       for b in bd],
        "warnings": res.warnings,
        "labels": {"values": list(LABELS),
                   "note": "consonant は長さを保つ対象、vowel / breath / silence は伸縮してよい"},
    }
