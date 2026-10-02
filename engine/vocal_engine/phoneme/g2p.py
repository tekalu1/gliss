# -*- coding: utf-8 -*-
"""歌詞 → かな → ローマ字音節 → 音素。

段階2スパイク（かな → 音素の変換）の移植。変更点は 3 つだけ:

  - 漢字の読みは **pyopenjtalk（`pyopenjtalk-plus`、MIT + 修正 BSD）** を既定で使う。
    無ければ `READING_OVERRIDES` で手当てし、それでも漢字が残ったら**分かるエラー**にする。
  - `merge_long_vowels()` を **既定で通す**（`g2p(..., merge_long=True)` が既定）。
    段階2 の実測: `トマー` を `t o m a a` と書くと HubertFA も SOFA も 2 つ目の母音を
    8〜10 ms に潰す。
  - 音素のラベルを 2 段で持つ（`label` = 画面・MCP 向けの 4 値、`detail` = 8 値）。

音素体系は HubertFA / SOFA / DiffSinger 系が共通で使う日本語ローマ字辞書
（`japanese_dict_full.txt`: `ka` → `k a`、`n` → `N`、`cl` → `cl`）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unicodedata
from dataclasses import dataclass, field

SP = "SP"                      # 無音
AP = "AP"                      # ブレス（吸気）

VOWELS = {"a", "i", "u", "e", "o"}
UNVOICED_CONSONANTS = {"k", "ky", "kw", "s", "sh", "t", "ty", "ts", "ch",
                       "h", "hy", "f", "p", "py"}
VOICED_CONSONANTS = {"g", "gy", "gw", "z", "j", "d", "dy", "b", "by", "m", "my",
                     "n", "ny", "r", "ry", "w", "y", "v"}

# 画面・MCP に出す 4 値（指示どおり）。detail はその内訳。
LABELS = ("consonant", "vowel", "breath", "silence")
DETAILS = ("consonant_voiced", "consonant_unvoiced", "vowel", "moraic_nasal",
           "closure", "breath", "silence", "unknown")

# 伸縮してよい（= タイミング補正で長さを変えてよい）ラベル。子音は保つ。
STRETCHABLE = ("vowel", "breath", "silence")

_BASE = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "wi", "ゑ": "we", "を": "o",
    "ん": "n",
    "ゔ": "vu",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゎ": "wa",
}

_DIGRAPH = {
    "きゃ": "kya", "きぃ": "kyi", "きゅ": "kyu", "きぇ": "kye", "きょ": "kyo",
    "ぎゃ": "gya", "ぎぃ": "gyi", "ぎゅ": "gyu", "ぎぇ": "gye", "ぎょ": "gyo",
    "しゃ": "sha", "しゅ": "shu", "しぇ": "she", "しょ": "sho",
    "じゃ": "ja", "じゅ": "ju", "じぇ": "je", "じょ": "jo", "じぃ": "ji",
    "ちゃ": "cha", "ちゅ": "chu", "ちぇ": "che", "ちょ": "cho",
    "ぢゃ": "ja", "ぢゅ": "ju", "ぢぇ": "je", "ぢょ": "jo",
    "にゃ": "nya", "にぃ": "nyi", "にゅ": "nyu", "にぇ": "nye", "にょ": "nyo",
    "ひゃ": "hya", "ひぃ": "hyi", "ひゅ": "hyu", "ひぇ": "hye", "ひょ": "hyo",
    "びゃ": "bya", "びぃ": "byi", "びゅ": "byu", "びぇ": "bye", "びょ": "byo",
    "ぴゃ": "pya", "ぴぃ": "pyi", "ぴゅ": "pyu", "ぴぇ": "pye", "ぴょ": "pyo",
    "みゃ": "mya", "みぃ": "myi", "みゅ": "myu", "みぇ": "mye", "みょ": "myo",
    "りゃ": "rya", "りぃ": "ryi", "りゅ": "ryu", "りぇ": "rye", "りょ": "ryo",
    "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo", "ふゅ": "hyu",
    "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo",
    "うぃ": "wi", "うぇ": "we", "うぉ": "wo",
    "てぃ": "ti", "てゅ": "tyu", "でぃ": "di", "でゅ": "dyu",
    "とぅ": "tu", "どぅ": "du",
    "つぁ": "tsa", "つぃ": "tsi", "つぇ": "tse", "つぉ": "tso",
    "いぇ": "ye",
    "くぁ": "kwa", "くぃ": "kwi", "くぇ": "kwe", "くぉ": "kwo",
    "ぐぁ": "gwa", "ぐぃ": "gwi", "ぐぇ": "gwe", "ぐぉ": "gwo",
}

_ROMAJI_TO_PHONEMES: dict[str, list[str]] = {}


def _build_romaji_table():
    if _ROMAJI_TO_PHONEMES:
        return
    onsets = ["ky", "kw", "gy", "gw", "sh", "ch", "ts", "ty", "dy", "ny",
              "hy", "by", "py", "my", "ry", "jy",
              "k", "g", "s", "z", "j", "t", "d", "n", "h", "b", "p", "f",
              "m", "y", "r", "w", "v", "l"]
    syllables = set()
    for v in "aiueo":
        syllables.add(v)
        for o in onsets:
            syllables.add(o + v)
    for s in syllables:
        for o in sorted(onsets, key=len, reverse=True):
            if s.startswith(o) and len(s) > len(o):
                head = "j" if o == "jy" else ("r" if o == "l" else o)
                _ROMAJI_TO_PHONEMES[s] = [head, s[len(o):]]
                break
        else:
            _ROMAJI_TO_PHONEMES[s] = [s]
    _ROMAJI_TO_PHONEMES["shi"] = ["sh", "i"]
    _ROMAJI_TO_PHONEMES["chi"] = ["ch", "i"]
    _ROMAJI_TO_PHONEMES["tsu"] = ["ts", "u"]
    _ROMAJI_TO_PHONEMES["n"] = ["N"]
    _ROMAJI_TO_PHONEMES["cl"] = ["cl"]
    _ROMAJI_TO_PHONEMES[SP] = [SP]
    _ROMAJI_TO_PHONEMES[AP] = [AP]


_build_romaji_table()

# pyopenjtalk が入っていない環境での最小限の手当て（よく使う語だけ）
READING_OVERRIDES = {
    "今日": "キョウ",
    "歌声": "ウタゴエ",
}

_PUNCT = "！!？?、。，,．.・…「」『』()（）〜～ー-‐–—　 \t\n\r"


# ---------------------------------------------------------------- ラベル
def detail_of(ph: str) -> str:
    if ph in VOWELS:
        return "vowel"
    if ph == "N":
        return "moraic_nasal"
    if ph == "cl":
        return "closure"
    if ph == AP:
        return "breath"
    if ph in (SP, "pau", ""):
        return "silence"
    if ph in UNVOICED_CONSONANTS:
        return "consonant_unvoiced"
    if ph in VOICED_CONSONANTS:
        return "consonant_voiced"
    return "unknown"


def label_of(ph: str) -> str:
    """画面・MCP に出す 4 値（consonant / vowel / breath / silence）。

    撥音 `N` は母音と同じように伸ばせるので vowel 側、
    促音 `cl` は無音なので silence 側に寄せる（伸縮してよい区間の判定に使う）。
    """
    d = detail_of(ph)
    if d in ("vowel", "moraic_nasal"):
        return "vowel"
    if d == "breath":
        return "breath"
    if d in ("silence", "closure"):
        return "silence"
    return "consonant"


# ---------------------------------------------------------------- データ
@dataclass
class Syllable:
    kana: str
    romaji: str
    phonemes: list = field(default_factory=list)

    def to_json(self):
        return {"kana": self.kana, "romaji": self.romaji, "phonemes": list(self.phonemes)}


@dataclass
class G2PResult:
    text: str
    kana: str
    syllables: list
    source: str = "table"          # "pyopenjtalk" | "table"
    devoiced: list = field(default_factory=list)   # 無声化が期待される音素の通し番号

    @property
    def romaji_seq(self):
        return [s.romaji for s in self.syllables]

    @property
    def phoneme_seq(self):
        return [p for s in self.syllables for p in s.phonemes]

    @property
    def label_seq(self):
        return [label_of(p) for p in self.phoneme_seq]

    def phoneme_to_syllable(self):
        """音素の通し番号 → 音節の番号。"""
        out = []
        for i, s in enumerate(self.syllables):
            out.extend([i] * len(s.phonemes))
        return out

    def lab_line(self):
        """HubertFA の `.lab`（ローマ字音節をスペース区切り）。"""
        return " ".join(s.romaji for s in self.syllables if s.romaji not in (SP, AP))

    def to_json(self):
        return {
            "text": self.text, "kana": self.kana, "source": self.source,
            "romaji": self.romaji_seq, "phonemes": self.phoneme_seq,
            "labels": self.label_seq,
            "n_syllables": len([s for s in self.syllables if s.romaji not in (SP, AP)]),
            "n_phonemes": len([p for p in self.phoneme_seq if p not in (SP, AP)]),
            "n_vowels": sum(1 for p in self.phoneme_seq if p in VOWELS),
            "syllables": [s.to_json() for s in self.syllables],
            "devoiced": list(self.devoiced),
        }


# ---------------------------------------------------------------- 変換
def katakana_to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s)


def hiragana_to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in s)


def normalize_lyrics(text: str) -> str:
    """表記ゆれの正規化。長音らしき記号は `ー` に統一する。"""
    text = unicodedata.normalize("NFKC", text)
    for ch in "〜～~∼―‐–—-":
        text = text.replace(ch, "ー")
    return text


def _has_kanji(text: str) -> bool:
    return bool(re.search(r"[㐀-鿿豈-﫿]", text))


def _pyopenjtalk_kana(text: str):
    """漢字の部分だけ pyopenjtalk で読みにする（記号は落とさない）。"""
    try:
        import pyopenjtalk                       # noqa: F401
    except Exception:                            # noqa: BLE001
        return None
    import pyopenjtalk
    parts = re.split(r"([㐀-鿿豈-﫿]+[぀-ゟ]*)", text)
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 1 and p:
            out.append(pyopenjtalk.g2p(p, kana=True))
        else:
            out.append(hiragana_to_katakana(p))
    return "".join(out)


def pyopenjtalk_available() -> bool:
    """漢字を読めるか（表示用）。まだ読み込んでいなければ見つかるかだけを見る（engine_info のたびに読み込むと、
    アドオン（vocal_engine/addons.py）の拡張モジュールがエンジンに掴まれて、画面から削除できなくなる）。"""
    if "pyopenjtalk" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("pyopenjtalk") is not None
    except (ImportError, ValueError):
        return False


def text_to_kana(text: str, use_pyopenjtalk: bool = True):
    """歌詞 → カタカナ。(kana, source) を返す。"""
    text = normalize_lyrics(text)
    for k, v in READING_OVERRIDES.items():
        text = text.replace(k, v)
    if not _has_kanji(text):
        return hiragana_to_katakana(text), "table"
    if use_pyopenjtalk:
        kana = _pyopenjtalk_kana(text)
        if kana is not None and not _has_kanji(kana):
            return kana, "pyopenjtalk"
    raise ValueError("歌詞に漢字が残っている: %r\n%s" % (text, kanji_hint()))


def kanji_hint():
    """漢字を読めないときの案内。配布版はアドオン（vocal_engine/addons.py）、開発版は venv に入れる。"""
    from .. import config
    if config.frozen():
        return ("漢字の読みは、Gliss の「ヘルプ > モデルと追加の機能…」から「漢字の歌詞の読み」をダウンロードすると使える。"
                "それまではかな書きの歌詞を与えること。")
    return ("pyopenjtalk-plus（MIT + 修正 BSD）を入れるか、かな書きの歌詞を与えること。\n"
            "  インストール: uv pip install --python .venv/Scripts/python.exe pyopenjtalk-plus")


def _mk(kana: str, romaji: str) -> Syllable:
    return Syllable(kana=kana, romaji=romaji,
                    phonemes=list(_ROMAJI_TO_PHONEMES.get(romaji, [romaji])))


def _last_vowel(sylls):
    for s in reversed(sylls):
        for ph in reversed(s.phonemes):
            if ph in VOWELS:
                return ph
        if s.romaji == "n":
            return "i"
    return None


def kana_to_syllables(kana: str, keep_punct_as: str | None = SP):
    """かな列 → 音節列（拗音は 2 文字で 1 音節、っ→cl、ん→N、ー→直前の母音）。"""
    s = katakana_to_hiragana(normalize_lyrics(kana))
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in _PUNCT and ch != "ー":
            if keep_punct_as and (not out or out[-1].romaji != keep_punct_as):
                out.append(_mk(ch, keep_punct_as))
            i += 1
            continue
        if ch == "ー":
            pv = _last_vowel(out)
            if pv is None:
                i += 1
                continue
            out.append(_mk("ー", pv))
            i += 1
            continue
        if ch == "っ":
            out.append(_mk("っ", "cl"))
            i += 1
            continue
        if i + 1 < len(s) and s[i:i + 2] in _DIGRAPH:
            out.append(_mk(s[i:i + 2], _DIGRAPH[s[i:i + 2]]))
            i += 2
            continue
        if ch in _BASE:
            out.append(_mk(ch, _BASE[ch]))
            i += 1
            continue
        out.append(_mk(ch, SP))
        i += 1
    while out and out[0].romaji == SP:
        out.pop(0)
    while out and out[-1].romaji == SP:
        out.pop()
    return out


def merge_long_vowels(sylls):
    """長音で生まれた「同じ母音だけの音節」を直前の音節に吸収する。

    段階2 の実測: `ma` + `a` を与えると
    HubertFA も SOFA も 2 つ目の `a` に 8〜10 ms しか割かず、
    無音の向こう側（ファイル末尾）へ飛ばすことすらある。吸収すれば他の境界は 1 ms も動かない。
    """
    out = []
    for s in sylls:
        if (out and s.kana == "ー" and len(s.phonemes) == 1 and s.phonemes[0] in VOWELS
                and out[-1].phonemes and out[-1].phonemes[-1] == s.phonemes[0]):
            out[-1] = Syllable(kana=out[-1].kana + s.kana, romaji=out[-1].romaji,
                               phonemes=list(out[-1].phonemes))
            continue
        out.append(s)
    return out


def devoiced_hints(kana: str):
    """pyopenjtalk が返す母音の無声化（`I` / `U`）の位置。確信度の検算に使う。"""
    try:
        import pyopenjtalk
    except Exception:                            # noqa: BLE001
        return []
    try:
        raw = pyopenjtalk.g2p(kana).split()
    except Exception:                            # noqa: BLE001
        return []
    idx = []
    n = 0
    for p in raw:
        if p == "pau":
            continue
        if p in ("I", "U"):
            idx.append(n)
        n += 1
    return idx


def g2p(text: str, use_pyopenjtalk: bool = True, merge_long: bool = True) -> G2PResult:
    """歌詞 → G2PResult。**長音の吸収は既定で入る**（段階2 の最重要の実装指針）。"""
    kana, source = text_to_kana(text, use_pyopenjtalk=use_pyopenjtalk)
    sylls = kana_to_syllables(kana)
    if merge_long:
        sylls = merge_long_vowels(sylls)
    dv = devoiced_hints(kana) if use_pyopenjtalk else []
    return G2PResult(text=text, kana=kana, syllables=sylls, source=source, devoiced=dv)


SELF_TESTS = [
    ("さくらいろ", "s a k u r a i r o"),
    ("トマー！", "t o m a"),                     # 長音は吸収される（既定）
    ("トーマトマトマトマ！", "t o m a t o m a t o m a t o m a"),
    ("カンカン", "k a N k a N"),
    ("がっこう", "g a cl k o u"),
    ("しゃしん", "sh a sh i N"),
    ("ほしぞらにとんでゆく", "h o sh i z o r a n i t o N d e y u k u"),
    ("ふぁいと", "f a i t o"),
    ("きょうりゅう", "ky o u ry u u"),
]


def run_self_tests():
    fails = []
    for text, expect in SELF_TESTS:
        got = " ".join(p for p in g2p(text, use_pyopenjtalk=False).phoneme_seq
                       if p not in (SP, AP))
        if got != expect:
            fails.append("%r: expect %r, got %r" % (text, expect, got))
    return fails
