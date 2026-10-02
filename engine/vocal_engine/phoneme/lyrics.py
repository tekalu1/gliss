# -*- coding: utf-8 -*-
"""歌詞を**区間ごと**に持つためのデータモデル（段階2の続き / 実制作）。

段階2 までは歌詞が 1 本の文字列で、素材全体を 1 回でアラインしていた。
実制作のテイクは**曲全体（158 秒）**で、歌うのはその一部（アウトロの 4 フレーズ）
しかない。全体を 1 回でアラインすると

  - 141 秒ぶんの無音に音素を割り当てようとして境界が壊れる
  - 1 フレーズ直したいだけなのに毎回 158 秒を推論する

ので、**`{start_sec, end_sec, text}` の配列**で持ち、**区間ごとに**アラインする。
区間の外は「歌詞の無い発声・息・無音」として扱い、音素は付けない（`SP` で埋める）。

保存形式（`project.json` の `lyrics`）:

```json
{"take": [{"start_sec": 144.30, "end_sec": 148.77, "text": "さくらさくら…"},
          {"start_sec": 149.19, "end_sec": 150.56, "text": "やよいのそらは"}]}
```

段階2 までの `{"take": "さくら…"}`（ただの文字列）も読める。
その場合は**素材全体を 1 区間**とみなす（`start_sec` / `end_sec` が None）。
"""
from __future__ import annotations

MIN_ENTRY_SEC = 0.05


class LyricsError(ValueError):
    pass


def _num(v):
    if v is None or v == "":
        return None
    return round(float(v), 4)


def normalize_entry(e):
    """1 件を `{"start_sec", "end_sec", "text"}` に揃える。`start` / `end` も受ける。"""
    if isinstance(e, str):
        return {"start_sec": None, "end_sec": None, "text": e.strip()}
    if not isinstance(e, dict):
        raise LyricsError("歌詞の区間は {start_sec, end_sec, text} の辞書で渡す: %r" % (e,))
    s = _num(e.get("start_sec", e.get("start")))
    t = _num(e.get("end_sec", e.get("end")))
    text = (e.get("text") or "").strip()
    if s is not None and t is not None and t - s < MIN_ENTRY_SEC:
        raise LyricsError("区間が短すぎる（%.3f〜%.3f 秒）。%.0f ms 以上にすること"
                          % (s, t, MIN_ENTRY_SEC * 1000))
    if (s is None) != (t is None):
        raise LyricsError("start_sec と end_sec は両方そろえるか、両方省く（素材全体）")
    out = {"start_sec": s, "end_sec": t, "text": text}
    if e.get("reading"):
        out["reading"] = str(e["reading"]).strip()
    if e.get("origin") in ("estimated", "confirmed"):
        out["origin"] = e["origin"]
    if isinstance(e.get("confirmed_syllables"), list):
        out["confirmed_syllables"] = sorted({int(i) for i in e["confirmed_syllables"]
                                            if isinstance(i, int) and i >= 0})
    if isinstance(e.get("estimate"), dict):
        est = e["estimate"]
        out["estimate"] = {"reading": str(est.get("reading") or ""),
                           "confidence": round(float(est.get("confidence") or 0), 4)}
    return out


def normalize(value):
    """`project.json` / MCP から来た歌詞を**区間の配列**にする。

    受ける形: None / "" / "歌詞" / [{"start_sec","end_sec","text"}, ...] / ["歌詞", ...]
    返す形:   [{"start_sec": float|None, "end_sec": float|None, "text": str}, ...]（時間順）
    空文字のエントリは落とす。
    """
    if value is None:
        return []
    if isinstance(value, str):
        t = value.strip()
        return [{"start_sec": None, "end_sec": None, "text": t}] if t else []
    if isinstance(value, dict):
        value = [value]
    out = [normalize_entry(e) for e in value]
    out = [e for e in out if e["text"]]
    out.sort(key=lambda e: (e["start_sec"] is not None, e["start_sec"] or 0.0))
    _check_overlap(out)
    return out


def _check_overlap(entries):
    ranged = [e for e in entries if e["start_sec"] is not None]
    if ranged and len(ranged) != len(entries):
        raise LyricsError("区間つきの歌詞と「素材全体」の歌詞は混ぜられない"
                          "（全部に start_sec / end_sec を付けるか、1 件だけにする）")
    for a, b in zip(ranged[:-1], ranged[1:]):
        if b["start_sec"] < a["end_sec"] - 1e-6:
            raise LyricsError("歌詞の区間が重なっている: %.3f〜%.3f と %.3f〜%.3f"
                              % (a["start_sec"], a["end_sec"],
                                 b["start_sec"], b["end_sec"]))


def text_of(entries):
    """全区間の歌詞をつないだ 1 本の文字列（画面の入力欄・要約用）。"""
    return " ".join(e["text"] for e in entries if e["text"])


def key_of(entries):
    """キャッシュの照合に使う一意のキー。"""
    return [[e["start_sec"], e["end_sec"], e["text"], e.get("reading")]
            for e in entries]


def spans(entries, duration_sec):
    """各エントリの実際の時間範囲（None は素材全体）。"""
    out = []
    for e in entries:
        s = 0.0 if e["start_sec"] is None else max(0.0, e["start_sec"])
        t = duration_sec if e["end_sec"] is None else min(duration_sec, e["end_sec"])
        out.append((s, t))
    return out


def confirmed_syllable_indices(entries, syllables):
    """Return alignment indices backed by an explicitly supplied or corrected reading."""
    confirmed = set()
    for entry in entries:
        aligned = [s for s in syllables if entry["start_sec"] is None or
                   (s["end_sec"] > entry["start_sec"] and s["start_sec"] < entry["end_sec"])]
        if entry.get("origin") != "estimated":
            confirmed.update(s["index"] for s in aligned)
        else:
            marked = set(entry.get("confirmed_syllables", []))
            confirmed.update(s["index"] for i, s in enumerate(aligned) if i in marked)
    return confirmed


def hidden_estimated_syllable_indices(entries, syllables, notes, excluded_ids):
    """Estimated syllables whose note has a manually chosen boundary."""
    confirmed = confirmed_syllable_indices(entries, syllables)
    excluded = [n for n in notes if n.id in excluded_ids]
    return {s["index"] for s in syllables if s["index"] not in confirmed
            and any(n.end_sec > s["start_sec"] and n.start_sec < s["end_sec"]
                    for n in excluded)}


def upsert(entries, text, start_sec=None, end_sec=None, mode="replace"):
    """1 件を足す／置き換える／消す。

    | 条件 | 動き |
    |---|---|
    | 範囲なし・`text` あり | **全部を 1 件に置き換える**（段階2 までと同じ動き） |
    | 範囲なし・`text` 空 | 全部消す |
    | 範囲あり・`text` あり | その範囲に**重なる**エントリを消して 1 件入れる |
    | 範囲あり・`text` 空 | その範囲に重なるエントリを消す（削除） |
    | `mode="add"` | 重なりの削除をしない（重なっていたらエラー） |
    """
    text = (text or "").strip()
    if start_sec is None and end_sec is None:
        return normalize(text)
    e = normalize_entry({"start_sec": start_sec, "end_sec": end_sec, "text": text})
    keep = list(entries)
    replaced = [k for k in keep if k["start_sec"] is not None
                and k["end_sec"] > e["start_sec"] and k["start_sec"] < e["end_sec"]]
    if mode != "add":
        keep = [k for k in keep
                if k["start_sec"] is None
                or k["end_sec"] <= e["start_sec"] + 1e-6
                or k["start_sec"] >= e["end_sec"] - 1e-6]
    keep = [k for k in keep if k["start_sec"] is not None]   # 全体エントリは範囲指定で消す
    if text:
        if len(replaced) == 1 and replaced[0].get("estimate"):
            e["origin"] = "confirmed"
            e["estimate"] = dict(replaced[0]["estimate"])
        keep.append(e)
    return normalize(keep)


def parse_text_file(content):
    """歌詞テキストファイル → 区間の配列（画面の「歌詞を読み込む」）。

    1 行 1 区間。次のどちらでもよい:

        144.30 148.77 さくらさくら やよいのそらは…
        2:24.30 2:28.77  さくらさくら…

    時刻が無い行は**素材全体の歌詞**として 1 件にまとめる（段階2 までと同じ）。
    `#` で始まる行と空行は飛ばす。
    """
    entries = []
    plain = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) >= 3:
            s, t = _time(parts[0]), _time(parts[1])
            if s is not None and t is not None:
                entries.append({"start_sec": s, "end_sec": t, "text": parts[2].strip()})
                continue
        plain.append(line)
    if entries and plain:
        raise LyricsError("時刻つきの行と時刻の無い行が混ざっている: %r" % plain[0][:30])
    if entries:
        return normalize(entries)
    return normalize(" ".join(plain))


def _time(tok):
    """`144.30` / `2:24.30` / `0:02:24.30` を秒にする。数でなければ None。"""
    tok = tok.strip()
    if not tok:
        return None
    try:
        if ":" in tok:
            parts = [float(p) for p in tok.split(":")]
            v = 0.0
            for p in parts:
                v = v * 60.0 + p
            return round(v, 4)
        return round(float(tok), 4)
    except ValueError:
        return None
