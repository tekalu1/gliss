# -*- coding: utf-8 -*-
"""HubertFA のフレーム出力から日本語の読みを推定する。語彙や歌詞は入力しない。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..audio import resample
from . import g2p as G
from .hubertfa import get_aligner, _log_softmax, _sigmoid


@dataclass
class GuessedSyllable:
    kana: str
    start_sec: float
    end_sec: float
    confidence: float
    phonemes: tuple[str, ...]


def utterance_ranges(notes, duration_sec, gap_sec=0.30, pad_sec=0.12):
    """画面の歌詞入力と同じ、音程ノートを 300 ms 以内でつないだ発声区間。"""
    groups = []
    for n in notes:
        if n.kind != "note":
            continue
        if groups and n.start_sec - groups[-1][1] <= gap_sec:
            groups[-1][1] = max(groups[-1][1], n.end_sec)
            groups[-1][2].append(n.id)
        else:
            groups.append([n.start_sec, n.end_sec, [n.id]])
    return [(round(max(0.0, a - pad_sec), 4), round(min(duration_sec, b + pad_sec), 4), ids)
            for a, b, ids in groups]


def _kana_table():
    table = {}
    for kana in list(G._BASE) + list(G._DIGRAPH):
        if kana in "ぁぃぅぇぉゃゅょゎ" or kana == "を":
            continue
        syll = G.kana_to_syllables(kana, keep_punct_as=None)
        if len(syll) == 1 and all(p != "SP" for p in syll[0].phonemes):
            table.setdefault(tuple(syll[0].phonemes), kana)
    table[("N",)] = "ん"
    table[("w", "i")] = "うぃ"
    table[("w", "e")] = "うぇ"
    table[("w", "o")] = "うぉ"
    return table


KANA = _kana_table()


def decode_logits(frame_logits, edge_logits, vocab, frame_sec=0.01):
    """SP / 子音 / 母音・撥音の文法を持つフレーム Viterbi。"""
    ids = [0] + sorted({i for ph, i in vocab.items() if ph.startswith("ja/")})
    symbols = ["SP" if i == 0 else next(ph[3:] for ph, j in vocab.items()
                                   if j == i and ph.startswith("ja/")) for i in ids]
    vowels = set(G.VOWELS) | {"N"}
    consonants = set(symbols) - vowels - {"SP"}
    # フレーム側の確率は列制約付きアラインメントのもの。日本語以外は競合させない。
    emit = _log_softmax(np.asarray(frame_logits)[ids], axis=0)
    edge = np.clip(_sigmoid(np.asarray(edge_logits)), 1e-4, 1 - 1e-4)
    n, tmax = emit.shape
    score = np.full((n, tmax), -np.inf, dtype=np.float32)
    back = np.zeros((n, tmax), dtype=np.int16)
    score[0, 0] = emit[0, 0]
    # 最初が子音・母音から始まってもよい。
    score[1:, 0] = emit[1:, 0] - 1.5
    allowed = np.zeros((n, n), dtype=bool)
    for i, old in enumerate(symbols):
        for j, new in enumerate(symbols):
            if old == new:
                continue
            if new == "SP":
                allowed[i, j] = old in vowels
            elif old == "SP":
                allowed[i, j] = True
            elif old in consonants:
                allowed[i, j] = new in G.VOWELS
            else:
                allowed[i, j] = True
    trans = np.where(allowed, -1.0, -np.inf).astype(np.float32)
    for t in range(1, tmax):
        options = score[:, t - 1, None] + trans + np.log(edge[t])
        prev = np.argmax(options, axis=0)
        changed = options[prev, np.arange(n)]
        stayed = score[:, t - 1] + np.log1p(-edge[t])
        use_change = changed > stayed
        score[:, t] = emit[:, t] + np.where(use_change, changed, stayed)
        back[:, t] = np.where(use_change, prev, np.arange(n))
    path = np.zeros(tmax, dtype=np.int16)
    path[-1] = int(np.argmax(score[:, -1]))
    for t in range(tmax - 1, 0, -1):
        path[t - 1] = back[path[t], t]
    spans = []
    for t, state in enumerate(path):
        if not spans or spans[-1][2] != state:
            spans.append([t, t + 1, int(state)])
        else:
            spans[-1][1] = t + 1
    probs = np.exp(emit)
    syllables = []
    pending = None
    for span_i, (a, b, i) in enumerate(spans):
        ph = symbols[i]
        if ph == "SP":
            # HubertFA は cl（促音の閉鎖）も SP と同じ id=0 にする。
            # 短い内部無音が次の破裂子音へ続く場合だけ「っ」と読む。
            next_ph = symbols[spans[span_i + 1][2]] if span_i + 1 < len(spans) else None
            if syllables and 0.04 <= (b - a) * frame_sec <= 0.18 \
                    and next_ph in {"k", "t", "p", "ch", "ts"}:
                syllables.append(GuessedSyllable("っ", round(a * frame_sec, 4),
                                                   round(b * frame_sec, 4),
                                                   float(np.mean(probs[i, a:b])), ("cl",)))
            pending = None
            continue
        conf = float(np.mean(probs[i, a:b]))
        if ph in consonants:
            pending = (ph, a, conf)
            continue
        if ph == "N":
            pending = None
            key, start, confidence = ("N",), a, conf
        elif pending:
            key = (pending[0], ph)
            start, confidence = pending[1], min(pending[2], conf)
            pending = None
        else:
            key, start, confidence = (ph,), a, conf
        kana = KANA.get(key)
        if kana:
            if ph in G.VOWELS and (b - a) * frame_sec >= 0.55:
                kana += "ー"
            syllables.append(GuessedSyllable(kana, round(start * frame_sec, 4),
                                               round(b * frame_sec, 4), confidence, key))
    return syllables


def alignable(syllables, al):
    """アライナー（HubertFA の辞書・語彙）が扱える音節だけを残す。

    推定の読みはそのまま歌詞になり、強制アラインメントに渡る。**語彙に無い音素を出さない**
    （issue #58: `っ` の `cl` が `ja/cl` になって落ちた）。読みの規則（g2p）で音節に直して確かめる。"""
    out = []
    for s in syllables:
        romaji = [y.romaji for y in G.kana_to_syllables(s.kana.rstrip("ー"), keep_punct_as=None)]
        if romaji and not al.unknown_words(romaji):
            out.append(s)
    return out


def infer(x, sr):
    """1 発声区間の読みと音節時刻を返す。"""
    al = get_aligner()
    wav = resample(np.asarray(x, dtype="float64"), sr, al.sr) if sr != al.sr else x
    wav = np.ascontiguousarray(wav, dtype="float32")
    names = [o.name for o in al.session().get_outputs()]
    outs = dict(zip(names, al.session().run(names, {"waveform": wav[None, :]})))
    n = min(int(round(len(wav) / al.hop)), outs["ph_frame_logits"].shape[-1])
    return alignable(decode_logits(outs["ph_frame_logits"][0, :, :n],
                                   outs["ph_edge_logits"][0, :n], al.vocab["vocab"], al.frame_sec),
                     al)
