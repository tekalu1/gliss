# -*- coding: utf-8 -*-
"""音素と境界のデータモデル。

最小単位は Note と同じ「**時間範囲＋音源＋テキスト＋ラベル＋確信度**」。
音素レーンに出す文字は `text`（音素）と `kana`（対応するかな）の 2 段。

境界（`Boundary`）を音素とは**別の配列**で持つ。
設計の「子音｜母音の境界も横ドラッグで動かせる」は、
「音素の配列」ではなく「境界の配列」を編集対象にした方が素直に書ける
（1 本動かすと前後 2 つの音素の長さが同時に変わる、という関係を UI に書かずに済む）。
"""
from dataclasses import dataclass, field, asdict

MIN_PHONEME_MS = 20.0            # 音素の最短の長さ（move_boundary の下限）

BOUNDARY_KINDS = ("onset", "consonant_vowel", "vowel_consonant", "phoneme",
                  "offset", "user_added")


@dataclass
class PhonemeSpan:
    id: str
    index: int
    start_sec: float
    end_sec: float
    text: str                      # 音素（ローマ字表記。p / a / N / cl / SP / AP）
    kana: str | None               # 対応するかな（画面の歌詞レーン）
    romaji: str | None             # 音節（po / ma / …）
    label: str                     # consonant | vowel | breath | silence
    detail: str                    # consonant_voiced / _unvoiced / vowel / moraic_nasal
                                   # / closure / breath / silence / unknown
    confidence: float
    syllable_index: int | None = None
    source: str = "take"
    flags: list = field(default_factory=list)

    @property
    def duration_sec(self):
        return self.end_sec - self.start_sec

    @property
    def stretchable(self):
        """タイミング補正で**長さを変えてよい**か（子音は保つ）。"""
        return self.label in ("vowel", "breath", "silence")

    def to_json(self):
        d = asdict(self)
        d["duration_sec"] = round(self.duration_sec, 4)
        d["stretchable"] = self.stretchable
        return d

    @classmethod
    def from_json(cls, d):
        keep = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**keep)


@dataclass
class Boundary:
    id: str
    index: int
    time_sec: float
    kind: str
    before_index: int | None       # この境界の左にある音素の index
    after_index: int | None        # 右にある音素の index
    confidence: float = 0.5
    movable: bool = True
    source: str = "take"

    def to_json(self):
        return asdict(self)

    @classmethod
    def from_json(cls, d):
        keep = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**keep)


@dataclass
class PhonemeResult:
    source: str
    lyrics: str
    kana: str
    phonemes: list
    boundaries: list
    syllables: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    confidence: float = 0.0
    aligner: dict = field(default_factory=dict)
    g2p: dict = field(default_factory=dict)
    duration_sec: float = 0.0
    entries: list = field(default_factory=list)   # 区間ごとの歌詞（phoneme/lyrics.py）

    def to_json(self):
        return {
            "source": self.source, "lyrics": self.lyrics, "kana": self.kana,
            "duration_sec": round(self.duration_sec, 4),
            "confidence": round(float(self.confidence), 3),
            "aligner": self.aligner, "g2p": self.g2p,
            "phonemes": [p.to_json() for p in self.phonemes],
            "boundaries": [b.to_json() for b in self.boundaries],
            "syllables": list(self.syllables),
            "warnings": list(self.warnings),
            "entries": list(self.entries),
        }

    @classmethod
    def from_json(cls, d):
        return cls(source=d.get("source", "take"), lyrics=d.get("lyrics", ""),
                   kana=d.get("kana", ""),
                   phonemes=[PhonemeSpan.from_json(p) for p in d.get("phonemes", [])],
                   boundaries=[Boundary.from_json(b) for b in d.get("boundaries", [])],
                   syllables=list(d.get("syllables", [])),
                   warnings=list(d.get("warnings", [])),
                   confidence=float(d.get("confidence", 0.0)),
                   aligner=dict(d.get("aligner", {})), g2p=dict(d.get("g2p", {})),
                   duration_sec=float(d.get("duration_sec", 0.0)),
                   entries=list(d.get("entries", [])))

    # ------------------------------------------------------------ 参照
    def boundary(self, boundary_id):
        for b in self.boundaries:
            if b.id == boundary_id:
                return b
        raise KeyError("境界が無い: %s（get_phonemes で確認）" % boundary_id)

    def phoneme(self, phoneme_id):
        for p in self.phonemes:
            if p.id == phoneme_id:
                return p
        raise KeyError("音素が無い: %s（get_phonemes で確認）" % phoneme_id)

    def in_range(self, start_sec=None, end_sec=None):
        t0 = -1e18 if start_sec is None else float(start_sec)
        t1 = 1e18 if end_sec is None else float(end_sec)
        ph = [p for p in self.phonemes if p.end_sec > t0 and p.start_sec < t1]
        bd = [b for b in self.boundaries if t0 <= b.time_sec <= t1]
        return ph, bd

    def overlapping(self, start_sec, end_sec):
        return [p for p in self.phonemes
                if p.end_sec > start_sec + 1e-9 and p.start_sec < end_sec - 1e-9]


def build_boundaries(phonemes, source="take"):
    """音素列 → 境界列。blob の両端と、子音｜母音などの内側の境目。

    - 音素が時間的に連続していれば境界は 1 本（両側の音素を持つ）。
    - 途切れている（SP を挟まずに隙間がある）ときは、両側に 1 本ずつ立てる。
    """
    out = []
    n = 0

    def add(t, kind, bi, ai):
        nonlocal n
        out.append(Boundary(id="b%03d" % n, index=n, time_sec=round(float(t), 4),
                            kind=kind, before_index=bi, after_index=ai,
                            confidence=0.5, movable=True, source=source))
        n += 1

    if not phonemes:
        return out
    add(phonemes[0].start_sec, "onset", None, phonemes[0].index)
    for a, b in zip(phonemes[:-1], phonemes[1:]):
        if abs(a.end_sec - b.start_sec) < 1e-6:
            add(a.end_sec, _kind(a, b), a.index, b.index)
        else:
            add(a.end_sec, "offset", a.index, None)
            add(b.start_sec, "onset", None, b.index)
    add(phonemes[-1].end_sec, "offset", phonemes[-1].index, None)
    return out


def _kind(a, b):
    if a.label == "consonant" and b.label == "vowel":
        return "consonant_vowel"
    if a.label == "vowel" and b.label == "consonant":
        return "vowel_consonant"
    if a.label in ("silence", "breath") or b.label in ("silence", "breath"):
        return "onset" if a.label in ("silence", "breath") else "offset"
    return "phoneme"
