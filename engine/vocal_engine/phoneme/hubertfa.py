# -*- coding: utf-8 -*-
"""HubertFA v0.0.7（ONNX）の薄い推論。**依存は onnxruntime + numpy だけ**。

HubertFA のリポジトリは取り込まない（段階2 の方針）。
ONNX の口は 1 本 / 出口は 3 本しかないので、必要なのは

    waveform(1, N) @ 44100 Hz
      -> ph_frame_logits (1, vocab, T)   各フレームの音素の対数尤度
         ph_edge_logits  (1, T)          境界らしさ
         cvnt_logits     (1, C, T)       非言語（None / AP / EP）

と、そこから音素列を Viterbi で切る後段だけ。後段は
https://github.com/wolfgitpr/HubertFA （**Apache-2.0**）の `tools/decoder.py` の
アルゴリズムを numpy で書き直したもの（型・変数名は追えるように寄せてある）。

**重みは同梱しない**（v0.0.7 の zip にも README にもライセンス表記が無いため）。
無い場合は入手元を含むエラーを出す。
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from .. import log
from ..audio import resample

# 置き場は F0 と同じ config.models_dir()（VOCAL_ENGINE_MODELS_DIR。旧名 VOCAL_ENGINE_MODELS も読む）
from ..config import models_dir as _models_dir  # noqa: E402

DEFAULT_MODELS_DIR = _models_dir()
HUBERTFA_DIR = os.environ.get(
    "VOCAL_ENGINE_HUBERTFA",
    os.path.join(DEFAULT_MODELS_DIR, "hubertfa", "1218_hfa_model_new_dict"))

DOWNLOAD_URL = ("https://github.com/wolfgitpr/HubertFA/releases/download/v0.0.7/"
                "1218_hfa_model_new_dict.zip")
DOWNLOAD_SHA256 = "48bd6dbcc293e47cc6cbcc556baf575e3d91d61d0ac88ddeccd6098f8f346fa2"

ONNX_VERSION = 5
_session_cache = {}
_load_lock = threading.RLock()     # 裏の準備（issue #63）と表が同時に初めて読むとき、415 MB を 2 回読まない


class AlignerError(RuntimeError):
    pass


class AlignerSetupError(AlignerError):
    """重み・辞書・版が合わないなど、**環境**の問題（どの歌詞でも失敗する）。

    区間ごとのアラインの失敗（歌詞の側の問題）とは分けて、黙って飛ばさずに上げる。"""


def model_dir():
    return HUBERTFA_DIR


def model_path():
    return os.path.join(HUBERTFA_DIR, "model.onnx")


def model_found():
    return all(os.path.exists(os.path.join(HUBERTFA_DIR, n))
               for n in ("model.onnx", "vocab.json", "config.json"))


def missing_model_message():
    return (
        "音素アラインメントの重みが見つからない。\n"
        "  探した場所: %s\n"
        "  入手元    : %s\n"
        "  SHA-256   : %s（256,589,553 バイト）\n"
        "zip を展開して `%s` の直下に model.onnx / vocab.json / config.json / VERSION / "
        "japanese_dict_full.txt が並ぶようにすること。\n"
        "（HubertFA のコードは Apache-2.0 だが**重みにはライセンス表記が無い**ので同梱しない。"
        "画面の初回の「モデルの準備」でも取得できる）"
        % (HUBERTFA_DIR, DOWNLOAD_URL, DOWNLOAD_SHA256, HUBERTFA_DIR))


# ---------------------------------------------------------------- 数値
def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _log_softmax(x, axis=-1):
    m = np.max(x, axis=axis, keepdims=True)
    return x - m - np.log(np.sum(np.exp(x - m), axis=axis, keepdims=True))


def _softmax(x, axis=-1):
    e = np.exp(x - np.max(x, axis=axis, keepdims=True))
    return e / e.sum(axis=axis, keepdims=True)


# ---------------------------------------------------------------- モデル
class HubertFA:
    """ONNX セッション＋辞書。プロセス内で使い回す（ロードに数秒かかる）。"""

    def __init__(self, folder=None):
        self.folder = os.path.abspath(folder or HUBERTFA_DIR)
        if not model_found() and folder is None:
            raise AlignerSetupError(missing_model_message())
        need = ("model.onnx", "vocab.json", "config.json")
        miss = [n for n in need if not os.path.exists(os.path.join(self.folder, n))]
        if miss:
            raise AlignerSetupError(missing_model_message())
        with open(os.path.join(self.folder, "vocab.json"), encoding="utf-8") as f:
            self.vocab = json.load(f)
        with open(os.path.join(self.folder, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.mel = cfg["mel_spec_config"]
        self.sr = int(self.mel["sample_rate"])           # 44100
        self.hop = int(self.mel["hop_size"])             # 441 = 10 ms
        self.frame_sec = self.hop / self.sr
        vpath = os.path.join(self.folder, "VERSION")
        if os.path.exists(vpath):
            with open(vpath, encoding="utf-8") as f:
                v = int((f.readline() or "0").strip() or 0)
            if v != ONNX_VERSION:
                raise AlignerSetupError("HubertFA の ONNX バージョンが %d（対応は %d）: %s"
                                   % (v, ONNX_VERSION, self.folder))
        self._dicts = {}
        self._session = None
        self.load_sec = None
        self.providers = None

    # -------------------------------------------------- 辞書
    def dictionary(self, language="ja"):
        if language in self._dicts:
            return self._dicts[language]
        name = self.vocab["dictionaries"].get(language)
        if not name:
            raise AlignerSetupError("HubertFA の辞書に %r が無い（あるのは %s）"
                               % (language, ", ".join(self.vocab["dictionaries"])))
        path = os.path.join(self.folder, name)
        if not os.path.exists(path):
            raise AlignerSetupError("辞書が無い: %s" % path)
        d = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "\t" not in line:
                    continue
                k, v = line.split("\t", 1)
                d[k.strip()] = v.strip().split()
        self._dicts[language] = d
        return d

    # -------------------------------------------------- セッション
    def session(self):
        with _load_lock:
            return self._session_locked()

    def _session_locked(self):
        if self._session is None:
            import onnxruntime as ort
            t0 = time.perf_counter()
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                model_path() if self.folder == HUBERTFA_DIR
                else os.path.join(self.folder, "model.onnx"),
                opts, providers=["CPUExecutionProvider"])
            self.load_sec = time.perf_counter() - t0
            self.providers = self._session.get_providers()
            log.get("phoneme").info("HubertFA をロード: %.2f s / %s",
                                    self.load_sec, self.providers)
        return self._session

    # -------------------------------------------------- 入力の組み立て
    @property
    def silent_phonemes(self):
        """語彙で無音のクラス（id 0）にまとめられた音素（`cl`・`AP` など。`SP` は除く）。"""
        return set(self.vocab.get("silent_phonemes") or ()) - {"SP"}

    def token(self, ph, language="ja"):
        """音素 → 語彙のキー。**無音の音素（`cl`・`AP` など）には言語の接頭辞を付けない。**

        HubertFA の `vocab.json` は `silent_phonemes`（`cl` を含む）を接頭辞なしで持ち、どれも無音と
        同じ id 0 にまとめている（`merged_phoneme_groups`）。上流の `DictionaryG2P` は `SP` 以外の
        すべてに `ja/` を付けるので、辞書の `cl` → `cl` が `ja/cl` になって語彙に無い（issue #58）。
        """
        if ph == "SP" or ph in self.silent_phonemes:
            return ph
        prefix = language if self.vocab.get("language_prefix") else None
        return "%s/%s" % (prefix, ph) if prefix else ph

    def word_tokens(self, word, language="ja"):
        """ローマ字音節 1 つ → 語彙のキーの列。辞書・語彙に無ければ None（その音節はアラインできない）。"""
        d = self.dictionary(language)
        if word not in d:
            return None
        vocab = self.vocab["vocab"]
        toks = [self.token(ph, language) for ph in d[word] if ph != "SP"]
        if not toks or any(t not in vocab for t in toks):
            return None
        return toks

    def unknown_words(self, romaji_words, language="ja"):
        """アラインできない（辞書・語彙に無い）音節。推定の読みの検査に使う。"""
        return [w for w in romaji_words
                if w not in ("SP", "AP", "") and self.word_tokens(w, language) is None]

    def build_sequence(self, romaji_words, language="ja"):
        """ローマ字音節の列 → (ph_seq, word_seq, ph2word, unknown, used)。

        HubertFA の `DictionaryG2P` と同じ規則: 先頭と末尾は `SP`、
        単語の間にも `SP` を 1 つ挟む（連続 SP は作らない）。`used` は使った音節の番号
        （`romaji_words` の中の位置。辞書に無くて飛ばした音節は入らない）。

        **促音 `cl`（閉鎖）は単語の間の `SP` の代わりに置く**（`a cl k` であって `a SP cl SP k` ではない）。
        語彙では `SP` と同じ無音のクラスなので、閉鎖の区間 = 前の母音と次の子音の間の無音になる
        （段階2 の方針どおり「閉鎖 = 無音区間。境界を持たせる」）。`SP` と違って**飛ばせない**
        （1 フレーム以上取る）ので、音節の数が歌詞と合ったままになる（続けて来たら 1 つにまとめる）。
        語彙に無い音素があっても `KeyError` にはせず、その音節を `unknown` に入れて飛ばす。
        """
        silent = self.silent_phonemes
        word_seq, ph_seq, ph2word = [], ["SP"], [-1]
        unknown, used = [], []
        wi = 0
        for pos, w in enumerate(romaji_words):
            if w in ("SP", "AP", ""):
                continue
            toks = self.word_tokens(w, language)
            if toks is None:
                unknown.append(w)
                continue
            word_seq.append(w)
            used.append(pos)
            if all(t in silent for t in toks):
                # 閉鎖だけの音節（っ）: 単語の間の SP と入れ替える（先頭の SP は残す）
                if ph_seq[-1] not in silent:
                    if ph_seq[-1] == "SP" and len(ph_seq) > 1:
                        ph_seq.pop()
                        ph2word.pop()
                    ph_seq.append(toks[0])
                    ph2word.append(wi)
                wi += 1
                continue
            for t in toks:
                ph_seq.append(t)
                ph2word.append(wi)
            ph_seq.append("SP")
            ph2word.append(-1)
            wi += 1
        if ph_seq[-1] != "SP":
            ph_seq.append("SP")
            ph2word.append(-1)
        if not any(p != "SP" and p not in silent for p in ph_seq):
            raise AlignerError("歌詞から辞書に載っている音節が 1 つも取れなかった")
        return ph_seq, word_seq, ph2word, unknown, used

    # -------------------------------------------------- 推論
    def align(self, x, sr, romaji_words, language="ja", detect_breath=True):
        """波形と音節列 → 音素区間。

        返り値: {"phonemes": [{"start","end","text","confidence"}...],
                 "total_confidence", "rtf", ...}
        """
        x = np.asarray(x, dtype="float64")
        if x.ndim > 1:
            x = x.mean(axis=1)
        wav = resample(x, sr, self.sr) if sr != self.sr else x
        wav = np.ascontiguousarray(wav, dtype="float32")
        wav_length = len(wav) / self.sr

        ph_seq, word_seq, ph2word, unknown, used = self.build_sequence(romaji_words, language)
        sess = self.session()
        names = [o.name for o in sess.get_outputs()]
        t0 = time.perf_counter()
        outs = dict(zip(names, sess.run(names, {"waveform": wav[None, :]})))
        elapsed = time.perf_counter() - t0

        spans, total_conf, frame_conf = self._decode(
            outs["ph_frame_logits"], outs["ph_edge_logits"], wav_length, ph_seq)
        breaths = []
        if detect_breath and "cvnt_logits" in outs:
            breaths = self._decode_breath(outs["cvnt_logits"], wav_length)
        return {
            "phonemes": spans,
            "breaths": breaths,
            "total_confidence": float(total_conf),
            "frame_sec": self.frame_sec,
            "duration_sec": wav_length,
            "elapsed_sec": round(elapsed, 4),
            "rtf": round(elapsed / max(wav_length, 1e-6), 4),
            "unknown_syllables": unknown,
            "word_seq": word_seq,
            "used_words": used,
            "aligner": {"name": "hubertfa", "version": "v0.0.7",
                        "device": (self.providers or ["?"])[0],
                        "model_dir": self.folder},
        }

    # -------------------------------------------------- デコード
    def _decode(self, ph_frame_logits, ph_edge_logits, wav_length, ph_seq):
        """強制アラインメントの Viterbi（HubertFA `AlignmentDecoder.decode` の移植）。"""
        vocab = self.vocab["vocab"]
        ph_frame_logits = np.asarray(ph_frame_logits)[0]       # [vocab, T]
        ph_edge_logits = np.asarray(ph_edge_logits)[0]         # [T]
        missing = sorted({p for p in ph_seq if p not in vocab})
        if missing:                                            # build_sequence が弾くので来ないはず
            raise AlignerError("音素が HubertFA の語彙に無い: %s" % ", ".join(missing))
        ph_seq_id = np.array([vocab[p] for p in ph_seq], dtype="int64")
        # 飛ばしてよいのは SP だけ（閉鎖 cl は同じ無音のクラスだが、歌詞にある音節なので飛ばさない）
        skippable = np.array([p == "SP" for p in ph_seq], dtype=bool)

        ph_mask = np.full(self.vocab["vocab_size"], 1e9)
        ph_mask[ph_seq_id] = 0.0
        ph_mask[0] = 0.0

        T_avail = int((wav_length * self.sr + 0.5) / self.hop)
        ph_frame_logits = ph_frame_logits[:, :T_avail]
        ph_edge_logits = ph_edge_logits[:T_avail]

        adj = ph_frame_logits - ph_mask[:, None]
        prob_log_all = _log_softmax(adj, axis=0).astype("float32")
        edge = np.clip(_sigmoid(ph_edge_logits), 0.0, 1.0).astype("float32")
        edge_diff = np.concatenate([np.diff(edge), [0.0]])
        edge_prob = np.clip(edge + np.concatenate([[0.0], edge[:-1]]), 0.0, 1.0)

        idx_seq, time_int, frame_conf = self._viterbi(ph_seq_id, prob_log_all, edge_prob,
                                                      skippable=skippable)
        T = prob_log_all.shape[1]
        total_conf = float(np.exp(np.mean(np.log(frame_conf + 1e-6)) / 3.0))

        time_int = np.asarray(time_int, dtype="int32")
        frac = np.clip(edge_diff[time_int] / 2.0, -0.5, 0.5)
        times = self.frame_sec * np.concatenate(
            [time_int.astype("float64") + frac, [float(T)]])
        times = np.clip(times, 0.0, None)

        # フレームごとの確信度を音素区間に配る。
        # **`SP` は落とす**（HubertFA の `decode(ignore_sp=True)` と同じ）。
        # 辞書 G2P は音節と音節の間に必ず SP を挟むので、そのまま残すと
        # 「ポ｜（無音 5 ms）｜マ」のような偽の無音が音素レーンに並んでしまう。
        conf_at = np.asarray(frame_conf, dtype="float64")
        spans = []
        for i, ph_idx in enumerate(idx_seq):
            a, b = float(times[i]), float(times[i + 1])
            if b <= a:
                continue
            text = ph_seq[ph_idx].split("/")[-1]
            if text == "SP":
                continue
            fa = int(round(a / self.frame_sec))
            fb = max(fa + 1, int(round(b / self.frame_sec)))
            c = conf_at[fa:fb]
            spans.append({"start": a, "end": b, "text": text,
                          "confidence": float(np.mean(c)) if len(c) else float(total_conf)})
        spans = self._tidy(spans, wav_length)
        return spans, total_conf, conf_at

    @staticmethod
    def _viterbi(ph_seq_id, ph_prob_log, edge_prob, prob3_pad_len=2, skippable=None):
        """[S, T] の DP。type1 = 留まる / type2 = 次へ / type3 = 1 つ飛ばす（SP を跨ぐ）。

        skippable: 飛ばしてよい状態（省略時は上流と同じく id 0 = 無音のクラス）。"""
        S = len(ph_seq_id)
        if skippable is None:
            skippable = (np.asarray(ph_seq_id) == 0)
        prob_log = ph_prob_log[ph_seq_id, :]
        T = prob_log.shape[1]
        if S < 2:
            prob3_pad_len = 1

        curr_max = np.full(S, -np.inf, dtype="float64")
        dp = np.full((S, T), -np.inf, dtype="float32")
        dp[0, 0] = prob_log[0, 0]
        curr_max[0] = prob_log[0, 0]
        if skippable[0] and S > 1:
            dp[1, 0] = prob_log[1, 0]
            curr_max[1] = prob_log[1, 0]

        back = np.full((S, T), -1, dtype="int32")
        e_log = np.log(edge_prob + 1e-6)
        ne_log = np.log(1.0 - edge_prob + 1e-6)
        mask_reset = (ph_seq_id == 0)
        i3 = np.arange(prob3_pad_len, S)
        idx_arr = np.clip(i3 - prob3_pad_len + 1, 0, S - 1)
        cond3 = (idx_arr >= S - 1) | skippable[idx_arr]

        prob2 = np.full(S, -np.inf, dtype="float32")
        prob3 = np.full(S, -np.inf, dtype="float32")
        for t in range(1, T):
            pl = prob_log[:, t]
            prob1 = dp[:, t - 1] + pl + ne_log[t]
            prob2[1:] = dp[:S - 1, t - 1] + pl[:S - 1] + e_log[t] + curr_max[:S - 1] * (T / S)
            if len(i3):
                cand = (dp[:S - prob3_pad_len, t - 1] + pl[:S - prob3_pad_len]
                        + e_log[t] + curr_max[:S - prob3_pad_len] * (T / S))
                prob3[i3] = np.where(cond3, cand, -np.inf)
            stacked = np.vstack((prob1, prob2, prob3))
            mi = np.argmax(stacked, axis=0)
            dp[:, t] = stacked[mi, np.arange(S)]
            back[:, t] = mi
            stay = (mi == 0)
            np.maximum(curr_max, pl, out=curr_max, where=stay)
            np.copyto(curr_max, pl, where=~stay)
            curr_max[mask_reset] = 0.0
            prob2[1:] = -np.inf
            if len(i3):
                prob3[i3] = -np.inf

        if S == 1:
            s = 0
        else:
            s = S - 2 if (dp[-2, -1] > dp[-1, -1] and skippable[-1]) else S - 1
        idx_seq, time_int, conf = [], [], []
        for t in range(T - 1, -1, -1):
            conf.append(dp[s, t])
            if back[s, t] != 0:
                idx_seq.append(s)
                time_int.append(t)
                if back[s, t] == 1:
                    s -= 1
                elif back[s, t] == 2:
                    s -= prob3_pad_len
            if s < 0:
                break
        idx_seq.reverse()
        time_int.reverse()
        conf.reverse()
        conf = np.exp(np.diff(np.pad(conf, (1, 0), "constant", constant_values=0.0), 1))
        return idx_seq, time_int, conf

    def _decode_breath(self, cvnt_logits, wav_length, threshold=0.5, max_gap=5,
                       min_frames=10):
        """非言語ラベル（AP = ブレス）。HubertFA `NonLexicalDecoder` の移植。"""
        classes = ["None"] + list(self.vocab.get("non_lexical_phonemes", []))
        if "AP" not in classes:
            return []
        n = int((wav_length * self.sr + 0.5) / self.hop)
        probs = _softmax(np.asarray(cvnt_logits)[:, :, :n], axis=1)[0]
        p = probs[classes.index("AP")]
        out, start, gap = [], None, 0
        for i in range(len(p)):
            if p[i] >= threshold:
                if start is None:
                    start = i
                gap = 0
            elif start is not None:
                if gap < max_gap:
                    gap += 1
                else:
                    end = i - gap - 1
                    if end > start and (end - start) >= min_frames:
                        out.append({"start": start * self.frame_sec,
                                    "end": end * self.frame_sec, "text": "AP",
                                    "confidence": float(np.mean(p[start:end]))})
                    start, gap = None, 0
        if start is not None and (len(p) - start) >= min_frames:
            out.append({"start": start * self.frame_sec,
                        "end": (len(p) - 1) * self.frame_sec, "text": "AP",
                        "confidence": float(np.mean(p[start:]))})
        return out

    @staticmethod
    def _tidy(spans, wav_length, gap=0.1):
        """小さな隙間を埋め、残った隙間に `SP` を置く。

        HubertFA の `WordList.fill_small_gaps` → `add_SP` と同じ規則:
          - 100 ms 以下の隙間は直前の音素を伸ばして埋める（音節間の切れ目は無音ではない）
          - 残った隙間・先頭・末尾に `SP` を 1 つ置く
        """
        spans = [s for s in spans if s["end"] > s["start"]]
        if not spans:
            return [{"start": 0.0, "end": float(wav_length), "text": "SP",
                     "confidence": 0.0}] if wav_length > 0 else []
        if 0 < spans[0]["start"] < gap:
            spans[0]["start"] = 0.0
        if spans[-1]["end"] >= wav_length - gap:
            spans[-1]["end"] = float(wav_length)
        for a, b in zip(spans[:-1], spans[1:]):
            if 0 < b["start"] - a["end"] <= gap:
                a["end"] = b["start"]

        out = []
        if spans[0]["start"] > 0:
            out.append({"start": 0.0, "end": spans[0]["start"], "text": "SP",
                        "confidence": 0.0})
        out.append(spans[0])
        for b in spans[1:]:
            if b["start"] > out[-1]["end"] + 1e-9:
                out.append({"start": out[-1]["end"], "end": b["start"], "text": "SP",
                            "confidence": 0.0})
            out.append(b)
        if out[-1]["end"] < wav_length - 1e-9:
            out.append({"start": out[-1]["end"], "end": float(wav_length), "text": "SP",
                        "confidence": 0.0})
        return out


def get_aligner(folder=None):
    """プロセス内で 1 つだけ持つ（ONNX 415 MB のロードを繰り返さない）。"""
    key = os.path.abspath(folder or HUBERTFA_DIR)
    with _load_lock:
        if key not in _session_cache:
            _session_cache[key] = HubertFA(folder)
        return _session_cache[key]
