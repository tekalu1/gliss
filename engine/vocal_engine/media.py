# -*- coding: utf-8 -*-
"""エンジンの入力 = **ソース＋ソース内オフセット＋ソース ID**（DAW 連携 段階 0。`docs/daw-stage0.md` §1）。

段階 2 までは「曲頭 0:00 の WAV ファイル」だけを受け付けていた。これを ARA のモデル
（オーディオソース ⊃ リージョン）に写せる形に一般化する:

| ここ | ARA | 意味 |
|---|---|---|
| ソース（`source_id`, `path`, `source_frames`） | `ARAAudioSource`（persistentID・サンプル数） | 元の音声 1 本。ファイルでもサンプル列でもよい |
| クリップ（`offset_frames`, `frames`） | `ARAAudioModification` / playback region の source 側の範囲 | ソースのうち編集する範囲。ソースの先頭からのサンプル数 |

- **プロジェクトの時間はクリップの頭を 0 とする。** 解析・編集リスト・画面の秒はすべてクリップ内の秒。
  ソース上の位置 = `offset_sec` + クリップ内の秒。
- **「WAV ファイルを開く」はその特別な場合**（オフセット 0・長さ = ファイル全体・ID = 中身の SHA-256 の頭）。
  段階 2 までの `project.json`（これらのキーが無い）も読める（`normalize_media`）。
- **サンプル列**（`Samples`）は、プロジェクトのディレクトリの `sources/<id>.wav` に**そのまま**
  （float32 なら FLOAT、int16 なら PCM_16 … と型を保って）書き出して、以降はファイルのソースと同じに扱う。
  ARA ではホストがサンプルを持っているが、エンジンは別プロセス（VST3 / ARA の調査の案 (a)）
  なので、プラグイン側が一時 WAV か共有メモリで渡すことになる。ここではその受け口を一時 WAV にしてある。

約束: `media` の dict（`project.json` の `take` / `guide`）の `frames` / `duration_sec` / `sr` /
`channels` / `subtype` / `path` / `sha256` は段階 2 までと同じ意味（`frames` はクリップの長さ、
`path` と `sha256` はソースのファイル）。足したのは `source_id` / `source_kind` / `source_frames` /
`source_duration_sec` / `offset_frames` / `offset_sec`。
"""
from dataclasses import dataclass
import hashlib
import io
import os
import threading

import numpy as np
import soundfile as sf

from .audio import file_sig, sha256_file

SOURCE_KINDS = ("file", "samples")


class MediaError(RuntimeError):
    pass


@dataclass
class Samples:
    """メモリ上のサンプル列をソースとして渡す（ARA のオーディオソース相当）。

    samples: (n,) か (n, ch)。float32 / float64 / int16 / int32
    sr: サンプルレート
    source_id: ソース ID（ARA の persistentID など）。省略すると中身のハッシュから作る
    name: 表示名（ファイル名の代わり。書き出しの既定の名前にも使う）
    """
    samples: np.ndarray
    sr: int
    source_id: str | None = None
    name: str | None = None

    def array(self):
        a = np.asarray(self.samples)
        if a.dtype == np.float16:
            a = a.astype(np.float32)
        if a.dtype not in (np.int16, np.int32, np.float32, np.float64):
            raise MediaError("samples の型は int16 / int32 / float32 / float64 のどれか: %s" % a.dtype)
        if a.ndim == 1:
            a = a[:, None]
        if a.ndim != 2 or a.shape[0] < 1:
            raise MediaError("samples は (n,) か (n, ch) で 1 サンプル以上")
        if a.shape[1] > a.shape[0]:
            raise MediaError("samples の形が (ch, n) に見える（(n, ch) で渡すこと）: %r" % (a.shape,))
        return np.ascontiguousarray(a)

    def content_hash(self):
        a = self.array()
        h = hashlib.sha256()
        h.update(("%s|%d|%s" % (a.dtype.str, int(self.sr), a.shape)).encode())
        h.update(a.tobytes())
        return h.hexdigest()


@dataclass
class Clip:
    """ソース（ファイルのパスか `Samples`）のうち、編集する範囲。

    オフセット・長さはサンプル数（`*_frames`）か秒（`*_sec`）で。両方あればサンプル数が優先。
    長さを省くとソースの終わりまで。
    """
    source: object                      # str（WAV のパス）| Samples
    offset_frames: int | None = None
    length_frames: int | None = None
    offset_sec: float | None = None
    length_sec: float | None = None
    source_id: str | None = None
    # ソースの外（オフセットが負・終わりを越える）を**無音で詰めて**よいか。セッションのガイドを
    # テイクの位置に合わせて切り出すときだけ使う（`project/session.py`。テイクには使わない）
    pad: bool = False

    @property
    def is_samples(self):
        return isinstance(self.source, Samples)


def as_clip(x):
    """パス（str / PathLike）・`Samples`・`Clip` のどれでも `Clip` にする。"""
    if x is None:
        return None
    if isinstance(x, Clip):
        return x
    if isinstance(x, Samples):
        return Clip(source=x)
    if isinstance(x, (str, os.PathLike)):
        return Clip(source=os.fspath(x))
    raise MediaError("テイク／ガイドはパス・Samples・Clip のどれか: %r" % type(x))


def _subtype_for(dtype):
    if dtype == np.int16:
        return "PCM_16"
    if dtype == np.int32:
        return "PCM_32"
    if dtype == np.float32:
        return "FLOAT"
    return "DOUBLE"


def default_source_id(sha256):
    return "sha256:%s" % sha256[:16]


def safe_name(s, default="take"):
    return "".join(ch for ch in str(s) if ch.isalnum() or ch in "-_")[:40] or default


def samples_key(clip):
    """プロジェクトのディレクトリ名などに使う短い鍵（中身のハッシュ）。"""
    return clip.source.content_hash()[:8]


def materialize(clip, project_dir):
    """サンプル列のソースを `<project_dir>/sources/<id>.wav` に書いてパスを返す（ファイルならそのまま）。

    同じ中身なら書き直さない（書き出しは決定的なので SHA-256 も同じになる）。
    """
    if not clip.is_samples:
        path = os.path.abspath(clip.source)
        if not os.path.exists(path):
            raise MediaError("音声が見つからない: %s" % path)
        return path
    s = clip.source
    a = s.array()
    sid = clip.source_id or s.source_id or ("samples:%s" % s.content_hash()[:16])
    d = os.path.join(os.path.abspath(project_dir), "sources")
    os.makedirs(d, exist_ok=True)
    # ファイル名は ID を英数字化して 40 字まで＋ ID のハッシュ 8 桁（加工で同じ名前になる別の ID と取り違えない）
    tag = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8]
    path = os.path.join(d, "%s-%s.wav" % (safe_name(sid.replace(":", "-"), "source"), tag))
    subtype = _subtype_for(a.dtype)
    if os.path.exists(path):
        try:
            cur, sr = sf.read(path, dtype=a.dtype.name if a.dtype.kind in "if" else "float64",
                              always_2d=True)
            if sr == int(s.sr) and cur.shape == a.shape and np.array_equal(cur, a):
                return path
        except Exception:                        # noqa: BLE001  読めなければ書き直す
            pass
    tmp = path + ".tmp.wav"
    sf.write(tmp, a, int(s.sr), subtype=subtype, format="WAV")
    os.replace(tmp, path)
    return path


def resolve_range(clip, sr, total):
    """クリップの (オフセット, 長さ) をサンプル数で。ソースに収まらなければ MediaError。

    `clip.pad` なら、ソースの外（オフセットが負・終わりを越える）も許す（外は無音。`read_clip`）。
    ただしソースと 1 サンプルも重ならない範囲はエラー。"""
    if clip.offset_frames is not None:
        off = int(clip.offset_frames)
    elif clip.offset_sec is not None:
        off = int(round(float(clip.offset_sec) * sr))
    else:
        off = 0
    if clip.length_frames is not None:
        n = int(clip.length_frames)
    elif clip.length_sec is not None:
        n = int(round(float(clip.length_sec) * sr))
    else:
        n = total - off
    if n <= 0:
        raise MediaError("クリップの長さが 0 以下: %d" % n)
    if getattr(clip, "pad", False):
        if off >= total or off + n <= 0:
            raise MediaError("クリップがソースと重ならない: オフセット %d / 長さ %d（ソースは %d サンプル）"
                             % (off, n, total))
        return off, n
    if off < 0 or off >= total:
        raise MediaError("オフセットがソースの外: %d（ソースは %d サンプル）" % (off, total))
    if off + n > total:
        raise MediaError("クリップがソースの終わりを越える: %d + %d > %d" % (off, n, total))
    return off, n


def range_suffix(off, n, total):
    """ソースの一部ならプロジェクトのディレクトリ名に付ける印（全体なら空。段階 2 までと同じ名前）。"""
    if off == 0 and n == total:
        return ""
    return "-o%d-n%d" % (off, n)


@dataclass(frozen=True)
class FileInfo:
    samplerate: int
    frames: int
    channels: int
    subtype: str


_info_cache = {}
_info_lock = threading.Lock()


def file_info(path):
    """`sf.info` の中身。ファイルの署名（サイズ・更新時刻）が同じ間は開き直さない（issue #63。
    トラックを選ぶたびに G: のテイクとガイドを開いていた）。"""
    key = os.path.normcase(os.path.abspath(path))
    sig = file_sig(path)
    if sig is not None:
        with _info_lock:
            hit = _info_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    i = sf.info(path)
    info = FileInfo(int(i.samplerate), int(i.frames), int(i.channels), i.subtype)
    if sig is not None:
        with _info_lock:
            _info_cache[key] = (sig, info)
    return info


def describe(clip, path, sha256=None, role="take"):
    """クリップ → `project.json` に入れる media の dict（`path` は `materialize` の結果）。"""
    info = file_info(path)
    sr = int(info.samplerate)
    total = int(info.frames)
    sha = sha256 or sha256_file(path)
    off, n = resolve_range(clip, sr, total)
    if clip.is_samples:
        kind = "samples"
        sid = clip.source_id or clip.source.source_id or \
            ("samples:%s" % clip.source.content_hash()[:16])
    else:
        kind = "file"
        sid = clip.source_id or default_source_id(sha)
    return {
        "path": os.path.abspath(path),
        "sr": sr,
        "channels": int(info.channels),
        "frames": n,
        "duration_sec": round(n / sr, 6),
        "subtype": info.subtype,
        "sha256": sha,
        "role": role,
        "source_id": sid,
        "source_kind": kind,
        "source_name": (clip.source.name if clip.is_samples and clip.source.name else
                        os.path.basename(path)),
        "source_frames": total,
        "source_duration_sec": round(total / sr, 6),
        "offset_frames": off,
        "offset_sec": round(off / sr, 6),
        # サンプル列は中身のハッシュで同一か見る（書いた WAV の SHA-256 は、float だと libsndfile が
        # PEAK チャンクに書いた時刻を入れるので、同じ中身でも書くたびに変わる）
        "content_sha256": clip.source.content_hash() if clip.is_samples else None,
        # ソースの外を無音で詰めた（オフセットが負・終わりを越える）。セッションのガイドだけ
        "pad": bool(off < 0 or off + n > total),
    }


def normalize_media(m):
    """段階 2 までの media（ソースのキーが無い）を今の形にそろえる。None はそのまま。"""
    if not m:
        return m
    m = dict(m)
    if "frames" not in m and "duration_sec" in m and "sr" in m:
        m["frames"] = int(round(float(m["duration_sec"]) * int(m["sr"])))
    m.setdefault("offset_frames", 0)
    m.setdefault("offset_sec", round(int(m["offset_frames"]) / int(m["sr"]), 6))
    m.setdefault("source_kind", "file")
    if not m.get("source_id") and m.get("sha256"):
        m["source_id"] = default_source_id(m["sha256"])
    m.setdefault("source_frames", int(m["offset_frames"]) + int(m["frames"]))
    m.setdefault("source_duration_sec", round(int(m["source_frames"]) / int(m["sr"]), 6))
    if m.get("path"):
        m.setdefault("source_name", os.path.basename(m["path"]))
    return m


def same_clip(a, b):
    """2 つの media が**同じ中身の同じ範囲**か（プロジェクトを使い回すかの判定）。

    ソース ID は比べない（同じファイルを別の ID で開き直しても、編集リストを捨てない）。"""
    if not a or not b:
        return False
    a, b = normalize_media(a), normalize_media(b)
    if a.get("content_sha256") and b.get("content_sha256"):
        same = a["content_sha256"] == b["content_sha256"]     # サンプル列どうし: 中身で比べる
    else:
        same = a.get("sha256") == b.get("sha256")
    return (same
            and int(a["offset_frames"]) == int(b["offset_frames"])
            and int(a["frames"]) == int(b["frames"]))


READ_WHOLE_LIMIT = 512 * 1024 * 1024


def read_sound(path, start=0, stop=None, dtype="float64"):
    """`sf.read(path, start, stop, always_2d=True)` と同じ値を返す。ファイルは**1 回で読んでから**解釈する
    （issue #63）。libsndfile は小さな単位で何度も読むので、クラウドの仮想ドライブでは
    24〜48 MB の WAV 1 本に 0.7〜1 秒かかっていた（1 回で読めば 0.05 秒）。大きすぎるファイルはそのまま読む。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    if size is None or size > READ_WHOLE_LIMIT:
        return sf.read(path, start=start, stop=stop, dtype=dtype, always_2d=True)
    with open(path, "rb") as f:
        data = f.read()
    return sf.read(io.BytesIO(data), start=start, stop=stop, dtype=dtype, always_2d=True)


def read_clip(m, mono=False, dtype="float64"):
    """media のクリップの範囲だけを読む。(n, ch)（mono なら (n,)）と sr。

    範囲がソースの外にかかる（`pad`。オフセットが負・終わりを越える）ときは、外を 0 で詰める。"""
    path = m["path"]
    if not os.path.exists(path):
        raise MediaError("音声ファイルが見つからない: %s" % path)
    off = int(m.get("offset_frames", 0) or 0)
    n = m.get("frames")
    total = int(m.get("source_frames") or 0)
    if n is not None and (off < 0 or (total and off + int(n) > total)):
        total = file_info(path).frames
        a, b = max(0, off), min(total, off + int(n))
        y, sr = read_sound(path, start=a, stop=max(a, b), dtype=dtype)
        x = np.zeros((int(n), y.shape[1]), dtype=y.dtype)
        if b > a:
            x[a - off:b - off] = y
    else:
        stop = None if n is None else off + int(n)
        x, sr = read_sound(path, start=off, stop=stop, dtype=dtype)
    if mono:
        x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
        return np.ascontiguousarray(x), sr
    return x, sr


def display_name(m):
    """書き出しのファイル名・画面の表示に使う名前（拡張子なし）。"""
    name = m.get("source_name") or os.path.basename(m.get("path") or "take")
    stem = os.path.splitext(name)[0]
    if int(m.get("offset_frames", 0) or 0) or int(m.get("frames", 0)) != int(
            m.get("source_frames", m.get("frames", 0))):
        stem += "_%.3f-%.3f" % (float(m["offset_sec"]),
                                float(m["offset_sec"]) + float(m["duration_sec"]))
    return stem


def _hash_float(x, sr):
    h = hashlib.sha256()
    h.update(("%d|%s" % (int(sr), x.shape)).encode())
    h.update(np.ascontiguousarray(x, dtype="float64").tobytes())
    return h.hexdigest()


def as_float64(a):
    """サンプル列 → soundfile が同じ型の WAV を float64 で読んだときと同じ値。"""
    if a.dtype == np.int16:
        return a.astype("float64") / 32768.0
    if a.dtype == np.int32:
        return a.astype("float64") / 2147483648.0
    return a.astype("float64")


def clip_hash_of(clip):
    """まだ開いていない `Clip` の**音の中身**のハッシュ（`clip_audio_hash` と同じ値）。"""
    if clip.is_samples:
        a = clip.source.array()
        off, n = resolve_range(clip, int(clip.source.sr), a.shape[0])
        return _hash_float(as_float64(a[off:off + n]), clip.source.sr)
    path = os.path.abspath(clip.source)
    info = file_info(path)
    off, n = resolve_range(clip, int(info.samplerate), int(info.frames))
    x, sr = read_clip({"path": path, "offset_frames": off, "frames": n,
                       "source_frames": int(info.frames)})
    return _hash_float(x, sr)


def clip_audio_hash(m):
    """クリップの**音の中身**のハッシュ（float64 に読んだサンプル列）。

    ファイルの SHA-256 はファイルの書式で変わる（同じ音を PCM_24 の WAV で渡すか float32 の
    サンプル列で渡すかで違う）。アーカイブを別の渡し方の素材に戻すときの照合はこちらで行う。"""
    x, sr = read_clip(m)
    return _hash_float(x, sr)
