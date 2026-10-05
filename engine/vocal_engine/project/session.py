# -*- coding: utf-8 -*-
"""セッション = 1 曲ぶんの**複数トラック**（issue #7。`docs/track-view.md` §1）。

| トラックの種類 | 中身 | 編集 |
|---|---|---|
| `vocal` | ボーカルのテイク（複数）。**1 本をガイドに指定できる** | できる（トラックごとのプロジェクト） |
| `inst` | 伴奏。聴くだけ | できない |

- **1 トラック = 1 つの音声ファイル（か、その一部 = クリップ）**。タイムライン上の位置 `offset_sec`
  （**音源全体の位置**。クリップの横ドラッグでずらす。非破壊）を持つ。既定は 0（曲頭 0:00 起点のファイル）。
- **編集はトラックごとのプロジェクト**（`Project`。今までの 1 テイク＋ガイドの形そのまま）に入る。
  編集の秒はトラックの頭が 0（位置をずらしても編集は音と一緒に動く）。
- **ガイドとの対応はタイムライン上の位置で取る**: 編集対象のテイクのプロジェクトには、ガイドのトラックを
  「テイクの頭のタイムライン上の位置」から切り出したクリップを渡す（位置の差が負ならガイドの前を無音で詰める。
  `media.Clip(pad=True)`）。位置の差が 0 なら**ファイル全体をそのまま渡す**（段階 2 までと同じ。既存の
  プロジェクトのガイドの解析・歌詞をそのまま使える）。
- 置き場: **最初に開いたテイクのプロジェクトのディレクトリ**に `session.json`。他のボーカルのトラックの
  プロジェクトはその下の `tracks/<名前>-<中身のハッシュ 8 桁>/`（セッションごとに独立。テストの分離にもなる）。
- 後方互換: `session.json` が無いプロジェクト（1 テイク＋ガイド）を開くと、テイクとガイドの 2 トラックの
  セッションを作る（ガイドのトラックをガイドに指定。プロジェクトの中身は変えない）。
- 画面と外部のエージェント（Claude Code）が同じセッションを触るので、読む前に `reload_if_changed`
  （project.json と同じ last-writer-wins）。**どのトラックを編集しているか**はプロセスごと
  （`current` はファイルに「最後に選んだトラック」として残すだけ）。

## 取り消しの履歴（issue #16。1 曲で 1 本）

`session.json` の `history` に、曲に保存されるものを変えた操作を**トラックをまたいで 1 本**に並べる。

| kind | 中身 | 取り消し |
|---|---|---|
| `edit` | トラックのプロジェクトの changeset（1 つ以上。複数ノートのピッチのドラッグは 1 つにまとめる） | そのトラックを編集対象にして、changeset を後ろから undo |
| `session` | トラックの操作と前後のスナップショット | 前のスナップショットへ。単体版のミキサー操作ではミュート／ソロ・音量・パンも戻す |
| `archive` | 明示的な ARA 補正インポートの前後 | 補正全体と解析方式を一操作で戻す。ホストの初期復元は履歴外 |
| `estimator` | 単体版の F0 方式と各トラックの解析方式 | 方式を戻し、必要な解析を復元する |

- 取り消せないもの: 表示・選択・編集対象の切り替え、ARA で DAW が所有するミキサー操作
- 新しい操作を入れると、やり直しの列（末尾の取り消し済み）は捨てる
- テンポ（`tempo`。issue #18）も `session` の項目に入る（スナップショットに含める）。ドラッグ・続けたホイールの変更は
  group で 1 つの項目にまとめる（`record_session(group=…)`）
- 後方互換: `history` の無い session.json（前の版）は、各トラックの project.json の changeset を
  作った時刻の順に並べて作る（前の版で入れた編集も Ctrl+Z で戻せる）。`history_marks`（トラックごとに
  履歴に入れた changeset の番号の最大）より新しい changeset が履歴に無ければ末尾に足す
  （履歴を知らない版のエンジン・プロジェクトを直接開いた編集も取りこぼさない）

## DAW（ARA）のセッション（`mcp_ara.py`）

`ara` = True のセッションは DAW のドキュメント 1 つ。トラックに `ara_id`（DAW の AudioModification の persistentID）を持ち、
トラックの有無・位置・名前・素材は DAW が決める（`ara_*` のツール。取り消しの履歴に入れない）。

- 同じファイルの別のトラックを許す（`add_track(ara_id=…)` は同じファイル・同じ範囲の重複の検査を飛ばす）
- 取り消しの `session` の項目を戻しても、ARA のトラックは今のまま（DAW の操作を Gliss の Ctrl+Z で戻さない）。
  戻すのはガイドの指定・テンポと、ARA でないトラック
- 外したトラックの id は `ara_gone` に控え、同じ `ara_id` を足し直したら同じ id にする（`history_marks` を引き継ぐ）

## テンポ（issue #18。`proposal/v3.html` §4・§8）

`tempo` = {bpm, num, den, start_sec, source, …}（無ければ None = 画面は秒のグリッド）。1 曲に 1 つ。

- `bpm`: 4 分音符の数／分（Studio One と同じ）。`num` / `den`: 拍子。`start_sec`: 1 小節目の頭（タイムラインの秒。負も可）
- `source`: "ixml"（トラックの WAV の iXML にある PreSonus のテンポマップから自動で読んだ）/ "manual"（手で入れた）/
  "daw"（DAW（ARA）の MusicalContext。`ara_sync`）。
  iXML からは、まだテンポが無いときにトラックを足した・開いたときに読む（`detect_tempo`。読んだトラックは
  `tempo_checked` に控えて読み直さない）。1 小節目はソングの 0:00 とみなし、bext の TimeReference から求める
- テンポが途中で変わる曲も、グリッドはファイルの頭のテンポ 1 つ（`varies` / `bpm_range` に範囲を残す）
"""
import copy
import json
import os
import re
import time

import soundfile as sf

from .. import bwf, log
from .. import media as M
from ..audio import file_sig, sha256_file
from ..phoneme import lyrics as LY
from .store import (Project, ProjectError, _default_project_dir, _shift_guide_lyrics, cached_project,
                    _tmp_name, dir_lock, remember_open, replace_file, warm_media)

SESSION_FILE = "session.json"
FORMAT = "vocal-editor-session"
VERSION = 1
KINDS = ("vocal", "inst")
PRIMARY_DIR = "."
# 名前から伴奏らしいものを伴奏として足す（間違えたらトラックの右クリックで変えられる）
_INST_RE = re.compile(r"(inst|karaoke|off[ _-]?vocal|backing|minus[ _-]?one|オケ|伴奏|カラオケ)",
                      re.IGNORECASE)


class SessionError(ProjectError):
    pass


def guess_kind(path):
    """ファイル名から `vocal` / `inst` を推す（`Inst_mix.wav`・`カラオケ.wav` などは伴奏）。"""
    name = os.path.splitext(os.path.basename(str(path)))[0]
    return "inst" if _INST_RE.search(name) else "vocal"


def _same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _clip_dict(clip):
    """`media.Clip` → トラックに持つクリップの範囲（秒。ファイル全体なら None）。"""
    if clip is None or (clip.offset_frames is None and clip.offset_sec is None
                        and clip.length_frames is None and clip.length_sec is None):
        return None
    info = sf.info(os.path.abspath(clip.source))
    sr = int(info.samplerate)
    off, n = M.resolve_range(clip, sr, int(info.frames))
    if off == 0 and n == int(info.frames):
        return None
    return {"offset_sec": round(off / sr, 6), "length_sec": round(n / sr, 6)}


def _same_clip_dict(a, b):
    if not a and not b:
        return True
    if not a or not b:
        return False
    return (abs(float(a["offset_sec"]) - float(b["offset_sec"])) < 1e-6
            and abs(float(a["length_sec"]) - float(b["length_sec"])) < 1e-6)


GAIN_MIN_DB = -60.0     # これ以下は無音（−∞）
GAIN_MAX_DB = 6.0


def norm_gain_db(v):
    """トラックの音量（dB）。0 を既定に、−60〜+6 に丸める（−60 は無音 = −∞）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    return round(max(GAIN_MIN_DB, min(GAIN_MAX_DB, v)), 3)


def norm_pan(v):
    """トラックのパン（−1 = 左いっぱい 〜 +1 = 右いっぱい。0 = 中央）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    return round(max(-1.0, min(1.0, v)), 4)


CUT_EPS = 1e-3                  # 切れ目・消した区間の端がこれより近ければ同じ点とみなす（秒）
CUT_MIN_EDGE = 0.02             # トラックの両端からこれより内側にだけ切れ目を入れられる（秒）
MUTE_MIN = 1e-3                 # 消した区間の最小の長さ（秒）


def norm_cuts(cuts, duration=None):
    """切れ目（トラックの頭が 0 の秒）を整える: 数だけ・範囲の中だけ・昇順・近いものは 1 つに。"""
    out = []
    for v in sorted(float(x) for x in (cuts or []) if _finite(x)):
        if v <= 0 or (duration is not None and v >= duration):
            continue
        if out and v - out[-1] < CUT_EPS:
            continue
        out.append(round(v, 6))
    return out


def norm_mutes(mutes, duration=None):
    """部分のミュートの区間 [[始め, 終わり]…] を整える: 範囲に収め・昇順・重なる／接する区間は 1 つに。"""
    rows = []
    for m in mutes or []:
        try:
            a, b = float(m[0]), float(m[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (_finite(a) and _finite(b)):
            continue
        a = max(0.0, a)
        if duration is not None:
            b = min(float(duration), b)
        if b - a >= MUTE_MIN:
            rows.append([a, b])
    rows.sort()
    out = []
    for a, b in rows:
        if out and a <= out[-1][1] + CUT_EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 6), round(b, 6)] for a, b in out]


def _finite(v):
    try:
        return v is not None and abs(float(v)) < float("inf")
    except (TypeError, ValueError):
        return False


def subtract_range(mutes, a, b):
    """区間のリストから [a, b] を引く（消した部分を戻す）。"""
    out = []
    for x, y in mutes:
        if y <= a + CUT_EPS / 2 or x >= b - CUT_EPS / 2:
            out.append([x, y])
            continue
        if x < a - CUT_EPS / 2:
            out.append([x, a])
        if y > b + CUT_EPS / 2:
            out.append([b, y])
    return out


def pieces_of(cuts, duration):
    """切れ目で分けた部分 [[始め, 終わり]…]（トラックの頭が 0 の秒）。"""
    xs = [0.0] + list(cuts) + [float(duration)]
    return [[xs[i], xs[i + 1]] for i in range(len(xs) - 1)]


def covered(mutes, a, b):
    """[a, b] が消した区間で全部覆われているか。"""
    for x, y in mutes:
        if x <= a + CUT_EPS and y >= b - CUT_EPS:
            return True
    return False


def _norm_track(t):
    t = dict(t)
    t.setdefault("kind", "vocal")
    if t["kind"] not in KINDS:
        t["kind"] = "vocal"
    t["offset_sec"] = float(t.get("offset_sec") or 0.0)
    t["mute"] = bool(t.get("mute"))
    t["solo"] = bool(t.get("solo"))
    t["gain_db"] = norm_gain_db(t.get("gain_db"))
    t["pan"] = norm_pan(t.get("pan"))
    t.setdefault("clip", None)
    t.setdefault("source_id", None)
    t.setdefault("ara_id", None)
    # 切れ目と、消した部分（クリップの分割。トラックの頭＝クリップの頭が 0 の秒。offset_sec で動かしても一緒に動く）
    dur = t.get("duration_sec")
    t["cuts"] = norm_cuts(t.get("cuts"), dur)
    t["mutes"] = norm_mutes(t.get("mutes"), dur)
    t.setdefault("name", os.path.splitext(os.path.basename(t.get("path") or "track"))[0])
    return t


class Session:
    def __init__(self, dir_path):
        self.dir = os.path.abspath(dir_path)
        self.tracks = []
        self.guide = None            # ガイドのトラックの id（1 本だけ）
        self.current = None          # 最後に選んだトラック（画面を開き直したときに戻す）
        # 段階 2 までのプロジェクトを移行したとき、ガイドの音声が見つからなかった（外付け・同期待ちなど）。
        # 見つかったらガイドのトラックにする。それまでプロジェクトのガイドは外さない
        self.missing_guide = None
        self.seq = 0
        self._sig = None
        self.history = None          # 取り消しの履歴（None = まだ作っていない。ensure_history）
        self.history_seq = 0
        self.history_marks = {}      # トラック id → 履歴に入れた changeset の番号の最大
        self.tempo = None            # テンポ（issue #18。無ければ秒のグリッド）
        self.tempo_checked = []      # iXML のテンポを読みに行ったトラック（読み直さない）
        self.ara = False             # DAW（ARA）のドキュメントのセッション（mcp_ara.py）
        self.ara_gone = {}           # 外した ARA のトラック: ara_id → {id, …}（足し直したら同じ id）

    # ------------------------------------------------------------ 読み書き
    @property
    def path(self):
        return os.path.join(self.dir, SESSION_FILE)

    @staticmethod
    def exists(dir_path):
        return os.path.exists(os.path.join(dir_path, SESSION_FILE))

    @classmethod
    def load(cls, dir_path):
        s = cls(dir_path)
        s._read()
        return s

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("format") != FORMAT:
            raise SessionError("session.json の形式が違う: %r" % d.get("format"))
        if int(d.get("version") or 1) > VERSION:
            raise SessionError("session.json の版 %s はこのエンジン（%d まで）では読めない"
                               % (d.get("version"), VERSION))
        self.tracks = [_norm_track(t) for t in d.get("tracks") or []]
        ids = {t["id"] for t in self.tracks}
        self.guide = d.get("guide") if d.get("guide") in ids else None
        self.current = d.get("current") if d.get("current") in ids else None
        self.missing_guide = d.get("missing_guide") or None
        self.seq = max([int(d.get("seq") or 0)] + [_num_id(t["id"]) for t in self.tracks])
        h = d.get("history")
        self.history = [dict(e) for e in h] if isinstance(h, list) else None
        self.history_seq = int(d.get("history_seq") or 0)
        self.history_marks = dict(d.get("history_marks") or {})
        self.tempo = norm_tempo(d.get("tempo"))
        self.tempo_checked = [str(x) for x in d.get("tempo_checked") or []]
        self.ara = bool(d.get("ara"))
        self.ara_gone = {str(k): dict(v) if isinstance(v, dict) else {"id": str(v)}
                         for k, v in (d.get("ara_gone") or {}).items()}
        self._sig = self._stat()
        return self

    def to_json(self):
        d = {"format": FORMAT, "version": VERSION, "guide": self.guide,
             "current": self.current, "seq": self.seq, "missing_guide": self.missing_guide,
             "tracks": self.tracks, "tempo": self.tempo, "tempo_checked": self.tempo_checked}
        if self.ara:
            d.update(ara=True, ara_gone=self.ara_gone)
        if self.history is not None:
            d.update(history=self.history, history_seq=self.history_seq,
                     history_marks=self.history_marks)
        return d

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = _tmp_name(self.path)               # 別のプロセスのエンジンと書きかけのファイルを共有しない
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.to_json(), ensure_ascii=False, indent=2))
        replace_file(tmp, self.path)
        self._sig = self._stat()
        return self.path

    def _stat(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload_if_changed(self):
        """session.json がディスク上で変わっていたら読み直す（外部のエージェントがトラックを足した等）。"""
        sig = self._stat()
        if sig is None or sig == self._sig:
            return False
        log.get().info("session.json が外部で変わっていたので読み直す: %s", self.path)
        self._read()
        return True

    # ------------------------------------------------------------ トラック
    def track(self, track_id):
        for t in self.tracks:
            if t["id"] == track_id:
                return t
        raise SessionError("トラックが無い: %s（list_tracks で確認）" % track_id)

    def find(self, path, clip=None):
        for t in self.tracks:
            if t.get("ara_id"):
                continue                         # DAW のトラックは同じファイルでも別もの（find_ara で引く）
            if _same_path(t["path"], path) and _same_clip_dict(t.get("clip"), clip):
                return t
        return None

    def find_ara(self, ara_id):
        """DAW の AudioModification（persistentID）のトラック。無ければ None。"""
        for t in self.tracks:
            if ara_id and t.get("ara_id") == ara_id:
                return t
        return None

    def primary(self):
        for t in self.tracks:
            if t.get("project_dir") == PRIMARY_DIR:
                return t
        return None

    def project_dir_of(self, t):
        d = t.get("project_dir")
        if not d:
            raise SessionError("%s（%s）にはプロジェクトが無い" % (t["id"], t["name"]))
        return os.path.normpath(d if os.path.isabs(d) else os.path.join(self.dir, d))

    def add_track(self, path, kind=None, name=None, offset_sec=0.0, clip=None, at=None,
                  project_dir=None, source_id=None, ara_id=None, track_id=None):
        """音声ファイルをトラックにする（同じファイル・同じ範囲がもうあれば SessionError）。

        ara_id: DAW（ARA）の AudioModification のトラック。同じファイルの別のトラックを許す（同じ ara_id は不可）。
        track_id: 使う id（外したトラックを足し直すとき。今あるトラックの id なら新しく振る）。"""
        path = os.path.abspath(str(path))
        if not os.path.exists(path):
            raise SessionError("音声が見つからない: %s" % path)
        if ara_id:
            if self.find_ara(ara_id) is not None:
                raise SessionError("もうトラックにある（ara_id %s）" % ara_id)
        elif self.find(path, clip) is not None:
            raise SessionError("もうトラックにある: %s" % os.path.basename(path))
        try:
            info = sf.info(path)
        except Exception as e:                   # noqa: BLE001
            raise SessionError("音声として読めない: %s（%s）" % (os.path.basename(path), e))
        kind = kind or guess_kind(path)
        if kind not in KINDS:
            raise SessionError("kind は vocal か inst: %r" % kind)
        sr, frames = int(info.samplerate), int(info.frames)
        dur = float(clip["length_sec"]) if clip else frames / sr
        sha = sha256_file(path)
        if project_dir is None and self._is_primary_source(sha, clip, sr, frames):
            # 外した最初のテイクを足し直した: 最初のテイクのプロジェクト（"."。前の編集）を使う（issue #32。
            # 新しい tracks/… にすると、編集が残っているのに出てこない）
            project_dir = PRIMARY_DIR
        if project_dir is None:
            stem = M.safe_name(os.path.splitext(os.path.basename(path))[0])
            suffix = ""
            if clip:
                suffix = "-o%d-n%d" % (int(round(clip["offset_sec"] * sr)),
                                       int(round(clip["length_sec"] * sr)))
            project_dir = "tracks/%s-%s%s" % (stem, sha[:8], suffix)
            used = {t.get("project_dir") for t in self.tracks}
            base, k = project_dir, 2
            while project_dir in used:
                project_dir = "%s-%d" % (base, k)
                k += 1
        # 同じプロジェクト（編集）のトラックを外して足し直した: 前の id を使う（issue #32）。新しい id にすると、
        # 取り消しの履歴の「そのトラックに入れた changeset の番号」（history_marks）が引き継がれず、前の編集が
        # 履歴の末尾に足し直されて、次の Ctrl+Z が古い編集を取り消してしまう
        tid = track_id if track_id and track_id not in {t["id"] for t in self.tracks} else None
        if tid is None:
            tid = self._old_id_for(project_dir)
        if tid is None:
            self.seq += 1
            tid = "t%d" % self.seq
        self.seq = max(self.seq, _num_id(tid))
        t = _norm_track({
            "id": tid, "name": name or os.path.splitext(os.path.basename(path))[0],
            "kind": kind, "path": path, "sha256": sha, "sr": sr, "channels": int(info.channels),
            "source_frames": frames, "duration_sec": round(dur, 6), "clip": clip,
            "offset_sec": round(float(offset_sec or 0.0), 6), "mute": False, "solo": False,
            "project_dir": project_dir, "source_id": source_id, "ara_id": ara_id or None,
        })
        if at is None:
            self.tracks.append(t)
        else:
            self.tracks.insert(int(at), t)
        log.get().info("トラックを足した: %s %s（%s・%.3f 秒）", tid, t["name"], kind, dur)
        return t

    def _old_id_for(self, project_dir):
        """取り消しの履歴のスナップショットで project_dir を使っていた（今は無い）トラックの id。無ければ None。"""
        now = {t["id"] for t in self.tracks}
        want = os.path.normcase(os.path.normpath(project_dir))
        for e in reversed(self.history or []):
            if e.get("kind") != "session":
                continue
            for side in ("after", "before"):
                for t in (e.get(side) or {}).get("tracks") or []:
                    d = t.get("project_dir")
                    if d and t.get("id") not in now and os.path.normcase(os.path.normpath(d)) == want:
                        return t["id"]
        return None

    def _is_primary_source(self, sha, clip, sr, frames):
        """最初のテイクのプロジェクト（"."）が空いていて、その素材が (sha, clip) と同じか。"""
        if any(t.get("project_dir") == PRIMARY_DIR for t in self.tracks):
            return False
        pj = os.path.join(self.dir, "project.json")
        if not os.path.exists(pj):
            return False
        try:
            with open(pj, encoding="utf-8") as f:
                tk = M.normalize_media(json.load(f).get("take"))
        except Exception:                        # noqa: BLE001
            return False
        if not tk or tk.get("source_kind") == "samples" or tk.get("sha256") != sha:
            return False
        off = int(tk.get("offset_frames") or 0)
        n = int(tk.get("frames") or 0)
        if clip:
            return (off == int(round(float(clip["offset_sec"]) * sr))
                    and n == int(round(float(clip["length_sec"]) * sr)))
        return off == 0 and n == int(frames)

    def remove_track(self, track_id):
        t = self.track(track_id)
        self.tracks = [x for x in self.tracks if x["id"] != track_id]
        if self.guide == track_id:
            self.guide = None
        if self.current == track_id:
            self.current = None
        return t

    def vocal_tracks(self):
        return [t for t in self.tracks if t["kind"] == "vocal"]

    def audible(self, t):
        solo = any(x["solo"] for x in self.tracks)
        return not t["mute"] and (not solo or t["solo"])

    def timeline(self):
        """タイムラインの範囲（秒）: 0 か一番前のトラックの頭 〜 一番後ろのトラックの終わり。"""
        if not self.tracks:
            return 0.0, 0.0
        a = min([0.0] + [t["offset_sec"] for t in self.tracks])
        b = max(t["offset_sec"] + t["duration_sec"] for t in self.tracks)
        return round(a, 6), round(b, 6)

    # ------------------------------------------------------------ プロジェクト（編集対象）
    def take_clip_for(self, t):
        """トラック → `Project.open` に渡すテイク（ファイル全体ならパスのまま）。"""
        c = t.get("clip")
        if not c and not t.get("source_id"):
            return t["path"]
        return M.Clip(t["path"],
                      offset_sec=(c or {}).get("offset_sec"), length_sec=(c or {}).get("length_sec"),
                      source_id=t.get("source_id"))

    def guide_clip_for(self, t):
        """編集対象 t に重ねるガイド（`Project.open` の guide）と、無いときの理由。

        ガイドのトラックを「t の頭のタイムライン上の位置」から切り出す。位置の差が 0 でガイドが
        ファイル全体なら、パスそのもの（段階 2 までと同じ＝既存の解析・歌詞をそのまま使う）。
        """
        if not self.guide:
            return None, None
        g = self.track(self.guide)
        if g["id"] == t["id"]:
            return None, "編集対象がガイドのトラック自身"
        if g["kind"] != "vocal":
            return None, "ガイドが伴奏になっている"
        if not os.path.exists(g["path"]):
            return None, "ガイドの音声が見つからない: %s" % g["path"]
        gc = g.get("clip") or {}
        g_off = float(gc.get("offset_sec") or 0.0)
        gs = g_off + (float(t["offset_sec"]) - float(g["offset_sec"]))   # ガイドのファイル上の秒
        sr = int(g["sr"])
        off = int(round(gs * sr))
        end = int(round((g_off + float(g["duration_sec"])) * sr)) if gc else int(g["source_frames"])
        if off >= end or off + int(round(float(t["duration_sec"]) * sr)) <= 0:
            return None, "ガイドがこのトラックの範囲に重ならない（位置を確かめる）"
        if off == 0 and not gc:
            return g["path"], None
        return M.Clip(g["path"], offset_frames=off, length_frames=end - off, pad=off < 0), None

    def check_ara_source(self, t):
        """DAW（ARA）のトラックのソースの WAV が、`ara_set_modification` が見た後で書き換わっていたら SessionError。

        プラグインはソースを書き直してから `ara_set_modification` を呼ぶ。その間にプロジェクトを開くと、
        `Project.open` がファイルの SHA-256 の違いを「素材が変わった」と見て編集を捨ててしまうため。"""
        sig = t.get("ara_file_sig")
        if t.get("ara_id") and sig and list(file_sig(t["path"]) or []) != list(sig):
            raise SessionError("DAW の音を読み込み中（%s）。少し待ってからやり直す" % t["name"])

    @staticmethod
    def estimator_of(t):
        """トラックで明示した F0 の方式（analyze_take(estimator=…)。無ければ None = 選んでいる方式）。"""
        return t.get("estimator") or None

    def open_project_for(self, t, reuse=True, lyrics=None, guide_lyrics=None):
        """トラックのプロジェクトを開く（ガイドはセッションの指定から）。(Project, ガイドが無い理由)。"""
        if t["kind"] != "vocal":
            raise SessionError("伴奏のトラック（%s）は編集できない" % t["name"])
        if not os.path.exists(t["path"]):
            raise SessionError("音声が見つからない: %s" % t["path"])
        self.check_ara_source(t)
        gclip, why = self.guide_clip_for(t)
        pdir = self.project_dir_of(t)
        tclip = self.take_clip_for(t)
        # SHA-256 と音声ファイルの情報はロックの外で取る（ロックの中の Project.open は覚えた値を使う。issue #63）
        warm_media(tclip, gclip)
        # 裏の準備（prep.py）・別のプロセスのエンジンが同じトラックの project.json を書いている間は待つ（issue #63）
        with dir_lock(pdir):
            if cached_project(pdir) is None and os.path.exists(os.path.join(pdir, "project.json")):
                # 先に読んでメモリに置く（ガイドの歌詞の控えと Project.open で 2 回読まない。issue #63）
                try:
                    remember_open(Project(pdir).load())
                except Exception:                # noqa: BLE001  読めなければ Project.open に任せる
                    pass
            self._stash_guide_lyrics(pdir)
            p = Project.open(tclip, gclip, project_dir=pdir,
                             reuse=reuse, lyrics=lyrics, guide_lyrics=guide_lyrics)
            p.estimator_pref = self.estimator_of(t)
            if gclip is not None:
                p.guide_take_cache_path = self._guide_take_cache_path()
            missing = (why or "").startswith("ガイドの音声が見つからない") or (
                not self.guide and self.missing_guide)
            if gclip is None and p.guide is not None and not missing:
                p.clear_guide()
            if gclip is not None and guide_lyrics is None:
                self._restore_guide_lyrics(p)
        return p, why

    def _guide_take_cache_path(self):
        gt = self._guide_track()
        if gt is None or not gt.get("project_dir"):
            return None
        return os.path.join(self.project_dir_of(gt), "cache", "take-analysis.json")

    def take_matches(self, t, take):
        """プロジェクトのテイク（media の dict）が、トラックの素材（中身・範囲）と同じか。"""
        if not take or take.get("sha256") != t.get("sha256"):
            return False
        sr = int(t["sr"])
        c = t.get("clip")
        off = int(round(float(c["offset_sec"]) * sr)) if c else 0
        n = int(round(float(c["length_sec"]) * sr)) if c else int(t["source_frames"])
        return int(take.get("offset_frames") or 0) == off and int(take.get("frames") or 0) == n

    def prep_project_for(self, t):
        """**裏の準備用**（`prep.py`。issue #63）: トラックのプロジェクトを、表で開いているものとは別の
        インスタンスで開く。(Project, ガイドが無い理由)。

        ガイドはセッションの指定をメモリの上でだけ重ねる（project.json のガイドの写し・ガイドの歌詞は
        書き換えない。表でトラックを選んだときに `open_project_for` が合わせる）。project.json がまだ
        無い（足したばかり）・素材が変わったときだけ、`Project.open` で作る。
        返すプロジェクトは `background=True`（解析しても今の写しと project.json を書かない）。"""
        if t["kind"] != "vocal":
            raise SessionError("伴奏のトラック（%s）は編集できない" % t["name"])
        if not os.path.exists(t["path"]):
            raise SessionError("音声が見つからない: %s" % t["path"])
        self.check_ara_source(t)
        gclip, why = self.guide_clip_for(t)
        pdir = self.project_dir_of(t)
        # テイクの SHA-256 と音声ファイルの情報もここで取っておく（ロックの外。プロセスの中で覚えるので、
        # 表で選んだときにも取り直さない。issue #63）
        tclip = self.take_clip_for(t)
        warm_media(tclip)
        ginfo = None
        if gclip is not None:
            c = M.as_clip(gclip)
            try:
                ginfo = M.describe(c, M.materialize(c, pdir), role="guide")   # SHA-256 はロックの外で
            except M.MediaError as e:
                raise ProjectError(str(e))
        with dir_lock(pdir):
            q = None
            if os.path.exists(os.path.join(pdir, "project.json")):
                try:
                    q = Project(pdir).load()
                except ProjectError:
                    raise
                except Exception:                    # noqa: BLE001  壊れている: Project.open に任せる
                    q = None
                if q is not None and not self.take_matches(t, q.take):
                    q = None
            if q is None:
                Project.open(tclip, gclip, project_dir=pdir, set_log=False, memo=False)
                q = Project(pdir).load()
        q.background = True
        q.estimator_pref = self.estimator_of(t)      # 表で明示した方式を、準備が既定の方式で上書きしない
        if ginfo is None:
            q.guide = None
            q.lyrics.pop("guide", None)
        else:
            if not M.same_clip(q.guide, ginfo):
                g_lyr = _shift_guide_lyrics(q.guide, ginfo, q.lyrics.get("guide"))
                q.guide = ginfo
                q.lyrics.pop("guide", None)
                if g_lyr:
                    q.lyrics["guide"] = g_lyr
                for k in ("guide", "alignment", "phonemes_guide"):
                    q.analysis.pop(k, None)
            q.guide_take_cache_path = self._guide_take_cache_path()
            self._stash_guide_lyrics(pdir)
            want = self._guide_lyrics_for(q)
            if want and want != q.lyrics.get("guide", []):
                q.lyrics["guide"] = want
        return q, why

    # ---- ガイドの歌詞: ガイドの素材（ファイル）上の秒でガイドのトラックに控える。
    #      ガイドを外す・ずらす・別のテイクに重ねるたびにプロジェクトの歌詞が捨てられても、ここから戻す
    def _guide_track(self):
        try:
            return self.track(self.guide) if self.guide else None
        except SessionError:
            return None

    def _stash_guide_lyrics(self, pdir):
        """プロジェクトのガイドの歌詞（ガイドの切り出し内の秒）→ ガイドのトラックの `guide_lyrics`（ファイル上の秒）。

        ガイドの切り出しの外にある控え（前にずらして外に出た区間）は残す。"""
        cp = cached_project(pdir)                # メモリに置いた Project がディスクと同じなら読まない（issue #63）
        if cp is not None:
            g, ent = cp.guide, LY.normalize(cp.lyrics.get("guide"))
        else:
            pj = os.path.join(pdir, "project.json")
            if not os.path.exists(pj):
                return
            try:
                with open(pj, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:                    # noqa: BLE001
                return
            g = M.normalize_media(d.get("guide"))
            ent = LY.normalize((d.get("lyrics") or {}).get("guide"))
        if not g or not ent:
            return
        gt = None
        for t in self.tracks:
            if t["kind"] == "vocal" and t.get("sha256") == g.get("sha256"):
                gt = t
                break
        if gt is None:
            return
        sr = float(g["sr"])
        off = int(g.get("offset_frames") or 0) / sr
        a, b = off, off + int(g["frames"]) / sr
        if any(e.get("start_sec") is None for e in ent):
            gt["guide_lyrics"] = [dict(e) for e in ent]
            return
        keep = [e for e in (gt.get("guide_lyrics") or [])
                if e.get("start_sec") is not None and (e["end_sec"] <= a or e["start_sec"] >= b)]
        now = [dict(e, start_sec=round(e["start_sec"] + off, 4), end_sec=round(e["end_sec"] + off, 4))
               for e in ent]
        try:
            gt["guide_lyrics"] = LY.normalize(keep + now)
        except Exception:                        # noqa: BLE001  重なり: 今のものを優先
            gt["guide_lyrics"] = now

    def _restore_guide_lyrics(self, p):
        """ガイドのトラックの控え（ファイル上の秒）→ プロジェクトのガイドの歌詞（切り出し内の秒）。"""
        want = self._guide_lyrics_for(p)
        if want and want != p.lyrics.get("guide", []):
            p.set_lyrics_entries(want, "guide")

    def _guide_lyrics_for(self, p):
        gt = self._guide_track()
        ctl = gt.get("guide_lyrics") if gt else None
        if not ctl or p.guide is None:
            return None
        sr = float(p.guide["sr"])
        off = int(p.guide.get("offset_frames") or 0) / sr
        dur = float(p.guide["duration_sec"])
        if any(e.get("start_sec") is None for e in ctl):
            want = [dict(e) for e in ctl]
        else:
            want = [dict(e, start_sec=round(e["start_sec"] - off, 4), end_sec=round(e["end_sec"] - off, 4))
                    for e in ctl if e["start_sec"] - off >= 0 and e["end_sec"] - off <= dur]
        return LY.normalize(want)

    def guide_stale(self, t, p):
        """開いているプロジェクト p のガイドが、いまのセッションの指定（位置）と違うか。"""
        want, _ = self.guide_clip_for(t)
        if want is None:
            return p.guide is not None
        if p.guide is None:
            return True
        c = M.as_clip(want)
        g = self.track(self.guide)
        off = int(c.offset_frames or 0)
        n = int(c.length_frames) if c.length_frames is not None else int(g["source_frames"])
        return not (_same_path(p.guide.get("path"), c.source)
                    and int(p.guide.get("offset_frames") or 0) == off
                    and int(p.guide.get("frames") or 0) == n)

    # ------------------------------------------------------------ 取り消しの履歴（issue #16）
    def snapshot(self):
        """トラックの並び・中身とガイドの指定（取り消しの `session` の前後）。"""
        return {"tracks": [{k: copy.deepcopy(v) for k, v in t.items() if k != "guide_lyrics"}
                           for t in self.tracks], "guide": self.guide,
                "missing_guide": copy.deepcopy(self.missing_guide),
                "tempo": copy.deepcopy(self.tempo)}

    @staticmethod
    def structure(snap, include_mixer=False):
        """スナップショットのうち、取り消しの対象になるところ。"""
        drop = ("guide_lyrics",) if include_mixer else ("mute", "solo", "gain_db", "pan", "guide_lyrics")
        return {"tracks": [{k: v for k, v in t.items() if k not in drop} for t in snap["tracks"]],
                "guide": snap.get("guide"), "tempo": snap.get("tempo")}

    def restore(self, snap, include_mixer=False):
        """スナップショットに戻す。通常の履歴ではミキサー値を保つ。単体版のミキサー操作だけ値を戻す。

        DAW（ARA）のセッションでは、ARA のトラック（有無・位置・名前・素材）と DAW のテンポ（source = "daw"）は
        今のまま（DAW が決めたもの）。戻すのはガイドの指定・画面で変えたテンポと、ARA でないトラック。"""
        cur = {t["id"]: t for t in self.tracks}
        tracks = copy.deepcopy(snap["tracks"])
        if self.ara:
            tracks = ([copy.deepcopy(t) for t in self.tracks if t.get("ara_id")]
                      + [t for t in tracks if not t.get("ara_id")])
        for t in tracks:
            c = cur.get(t["id"])
            if c is None:
                continue
            if not include_mixer or self.ara:
                t["mute"], t["solo"] = c.get("mute", False), c.get("solo", False)
                t["gain_db"], t["pan"] = c.get("gain_db", 0.0), c.get("pan", 0.0)
            if c.get("guide_lyrics") is not None:
                t["guide_lyrics"] = copy.deepcopy(c["guide_lyrics"])
            else:
                t.pop("guide_lyrics", None)
        self.tracks = [_norm_track(t) for t in tracks]
        ids = {t["id"] for t in self.tracks}
        g = snap.get("guide")
        self.guide = g if g in ids else None
        self.missing_guide = copy.deepcopy(snap.get("missing_guide"))
        daw_tempo = self.ara and (self.tempo or {}).get("source") == "daw"
        if "tempo" in snap and not daw_tempo:    # テンポを持たない前の版のスナップショットでは今のまま
            self.tempo = copy.deepcopy(snap["tempo"])
        self.seq = max([self.seq] + [_num_id(t) for t in ids])
        if self.current not in ids:
            self.current = None

    def ensure_history(self):
        """履歴が無ければ（前の版の session.json）、各トラックの changeset から作る。"""
        if self.history is None:
            self.history = []
            self.history_seq = 0
            self.history_marks = {}
            rows = []
            for ti, t in enumerate(self.tracks):
                for ci, c in enumerate(self._track_changesets(t)):
                    if c.get("discarded"):
                        continue
                    rows.append((str(c.get("created_at") or ""), ti, ci, t["id"], c))
            rows.sort(key=lambda r: (r[0], r[1], r[2]))
            for _, _, _, tid, c in rows:
                self._append({"kind": "edit", "track": tid, "changesets": [c["id"]],
                              "label": short_label(c.get("label")),
                              "author": c.get("author", "ai"), "at": c.get("created_at"),
                              "undone": bool(c.get("undone"))})
                self._mark(tid, c["id"])
            self._trim()
        return self.history

    def _track_changesets(self, t):
        if not t.get("project_dir"):
            return []
        pj = os.path.join(self.project_dir_of(t), "project.json")
        try:
            with open(pj, encoding="utf-8") as f:
                return list(json.load(f).get("changesets") or [])
        except Exception:                        # noqa: BLE001
            return []

    def _append(self, entry):
        self.history_seq += 1
        entry = dict(entry, id="h%d" % self.history_seq)
        entry.setdefault("undone", False)
        self.history.append(entry)
        return entry

    def _mark(self, tid, cid):
        n = _cs_num(cid)
        if n > int(self.history_marks.get(tid) or 0):
            self.history_marks[tid] = n

    def _trim(self):
        if len(self.history) > HISTORY_MAX:
            self.history = self.history[-HISTORY_MAX:]

    def _drop_redo_tail(self):
        """新しい操作を入れる前: やり直しの列（末尾の取り消し済み）を捨てる。捨てた項目を返す。"""
        out = []
        while self.history and self.history[-1].get("undone"):
            out.append(self.history.pop())
        return out

    def record_edit(self, track_id, changeset_id, label, group=None, author="ai"):
        """トラックのプロジェクトに入った changeset を履歴に足す。group が直前の項目と同じなら、その項目に
        まとめる（複数ノートのピッチのドラッグ = 取り消し 1 回）。(項目, 捨てたやり直しの列)"""
        self.ensure_history()
        last = self.history[-1] if self.history else None
        if (group and last is not None and not last.get("undone") and last.get("group") == group
                and last.get("kind") == "edit" and last.get("track") == track_id):
            if changeset_id not in last["changesets"]:
                last["changesets"].append(changeset_id)
            self._mark(track_id, changeset_id)
            return last, []
        dropped = self._drop_redo_tail()
        e = self._append({"kind": "edit", "track": track_id, "changesets": [changeset_id],
                          "label": label, "author": author, "at": _now()})
        if group:
            e["group"] = group
        self._mark(track_id, changeset_id)
        self._trim()
        return e, dropped

    def record_archive(self, track_id, before, after, estimator_before, estimator_after,
                       mark_before, label="補正を取り込む", author="ai"):
        """明示的な補正インポートを、置換前の補正も含む一操作にする。"""
        self.ensure_history()
        dropped = self._drop_redo_tail()
        e = self._append({"kind": "archive", "track": track_id, "label": label,
                          "author": author, "at": _now(), "before": before, "after": after,
                          "estimator_before": estimator_before,
                          "estimator_after": estimator_after,
                          "mark_before": mark_before,
                          "mark_after": int(self.history_marks.get(track_id) or 0)})
        self._trim()
        return e, dropped

    def record_estimator(self, track_id, before, after):
        """単体版の F0 方式を、トラックごとの明示方式と解析の復元情報ごと記録する。"""
        self.ensure_history()
        dropped = self._drop_redo_tail()
        e = self._append({"kind": "estimator", "track": track_id, "label": "ピッチ検出の方式",
                          "author": "human", "at": _now(), "before": before, "after": after})
        self._trim()
        return e, dropped

    def record_session(self, label, track_id, before, after, current_before=None,
                       current_after=None, author="ai", group=None, include_mixer=False):
        """トラックの操作を履歴に足す（変わっていなければ足さない）。(項目 | None, 捨てたやり直しの列)

        group が直前の項目（取り消していない `session`）と同じなら、その項目の after を差し替えて 1 つにまとめる
        （テンポのドラッグ・続けたホイール。まとめた結果が前と同じに戻ったら項目ごと消す）。"""
        self.ensure_history()
        last = self.history[-1] if self.history else None
        if (group and last is not None and not last.get("undone") and last.get("group") == group
                and last.get("kind") == "session" and bool(last.get("include_mixer")) == include_mixer):
            last["after"] = after
            last["label"] = label
            last["current_after"] = current_after
            if self.structure(last["before"], include_mixer) == self.structure(after, include_mixer):
                self.history.pop()
                return None, []
            return last, []
        if self.structure(before, include_mixer) == self.structure(after, include_mixer):
            return None, []
        dropped = self._drop_redo_tail()
        e = self._append({"kind": "session", "track": track_id, "label": label, "author": author,
                          "at": _now(), "before": before, "after": after,
                          "current_before": current_before, "current_after": current_after,
                          "include_mixer": include_mixer})
        if group:
            e["group"] = group
        self._trim()
        return e, dropped

    # ------------------------------------------------------------ テンポ（issue #18）
    def detect_tempo(self):
        """テンポがまだ無ければ、トラックの WAV の iXML（PreSonus のテンポマップ）から読む。読めたら True。

        伴奏を先に見る（Fender Studio Pro のミックスダウンを伴奏に使う想定）。読みに行ったトラックは
        `tempo_checked` に控えて、次からは読まない（開くたびにファイルを読み直さない）。"""
        if self.tempo is not None:
            return False
        order = sorted(self.tracks, key=lambda t: 0 if t["kind"] == "inst" else 1)
        for t in order:
            if t["id"] in self.tempo_checked:
                continue
            self.tempo_checked.append(t["id"])
            try:
                r = bwf.read_tempo(t["path"]) if os.path.exists(t["path"]) else None
            except Exception as e:               # noqa: BLE001  読めないファイルは飛ばす
                log.get().warning("テンポマップを読めない: %s（%s）", t["path"], e)
                r = None
            if not r:
                continue
            clip_off = float((t.get("clip") or {}).get("offset_sec") or 0.0)
            head = float(t["offset_sec"]) - clip_off      # ファイルの頭のタイムライン上の位置
            self.tempo = norm_tempo({
                "bpm": r["bpm"], "num": 4, "den": 4,
                "start_sec": head - float(r["time_reference_sec"] or 0.0),
                "source": "ixml", "track": t["id"], "file": os.path.basename(t["path"]),
                "varies": r["varies"], "bpm_range": r["bpm_range"]})
            log.get().info("テンポを iXML から読んだ: %s（%.2f BPM・1 小節目 %.3f 秒）",
                           t["path"], self.tempo["bpm"], self.tempo["start_sec"])
            return True
        return False

    def forget_changeset(self, track_id, changeset_id):
        """changeset を捨てた（ポップアップの当て直し）: 履歴の項目から外す（空になった項目は消す）。"""
        if self.history is None:
            return
        for e in list(self.history):
            if e.get("kind") == "edit" and e.get("track") == track_id \
                    and changeset_id in e.get("changesets", []):
                e["changesets"] = [c for c in e["changesets"] if c != changeset_id]
                if not e["changesets"]:
                    self.history.remove(e)

    def entry_of(self, track_id, changeset_id):
        for e in reversed(self.history or []):
            if e.get("kind") == "edit" and e.get("track") == track_id \
                    and changeset_id in e.get("changesets", []):
                return e
        return None

    def sync_project(self, track_id, project, exclude=()):
        """履歴に入っていない新しい changeset（履歴を知らない経路で入った編集）を末尾に足す。"""
        self.ensure_history()
        mark = int(self.history_marks.get(track_id) or 0)
        added = False
        for c in project.changesets:
            if _cs_num(c.id) <= mark or c.undone or c.discarded or c.id in exclude:
                continue
            self._drop_redo_tail()
            self._append({"kind": "edit", "track": track_id, "changesets": [c.id],
                          "label": short_label(c.label), "author": c.author, "at": c.created_at})
            self._mark(track_id, c.id)
            added = True
        if added:
            self._trim()
        return added

    def undo_target(self):
        for e in reversed(self.history or []):
            if not e.get("undone"):
                return e
        return None

    def redo_target(self):
        target = None
        for e in reversed(self.history or []):
            if e.get("undone"):
                target = e
            else:
                break
        return target

    def history_summary(self):
        names = {t["id"]: t["name"] for t in self.tracks}

        def brief(e):
            if e is None:
                return None
            return {"id": e["id"], "label": e.get("label"), "kind": e.get("kind"),
                    "track": e.get("track"), "track_name": names.get(e.get("track")),
                    "author": e.get("author")}
        u, r = self.undo_target(), self.redo_target()
        return {"can_undo": u is not None, "can_redo": r is not None,
                "undo": brief(u), "redo": brief(r), "size": len(self.history or [])}

    # ------------------------------------------------------------ 要約（MCP・画面）
    def summary(self, current=None, project=None):
        tracks = []
        for t in self.tracks:
            d = {k: t.get(k) for k in ("id", "name", "kind", "path", "offset_sec", "duration_sec",
                                       "sr", "channels", "mute", "solo", "gain_db", "pan", "clip", "cuts", "mutes")}
            if t.get("ara_id"):                  # DAW（ARA）のトラック: AudioModification と DAW のトラック名
                d["ara_id"], d["group"] = t["ara_id"], t.get("group")
            d["guide"] = t["id"] == self.guide
            d["current"] = t["id"] == current
            d["audible"] = self.audible(t)
            d["project_dir"] = self.project_dir_of(t) if t.get("project_dir") else None
            if project is not None and t["id"] == current:
                d["edits"] = len(project.edits)
            tracks.append(d)
        a, b = self.timeline()
        return {"dir": self.dir, "path": self.path, "guide": self.guide, "current": current,
                "timeline_sec": [a, b], "tracks": tracks, "tempo": copy.deepcopy(self.tempo),
                "history": self.history_summary() if self.history is not None else None}


HISTORY_MAX = 500               # 履歴に残す操作の数（古いものから捨てる）
TEMPO_BPM_RANGE = (20.0, 400.0)
TEMPO_DENS = (2, 4, 8, 16)


def norm_tempo(t):
    """テンポの dict を整える（読めない・範囲外は None）。"""
    if not isinstance(t, dict):
        return None
    try:
        bpm = round(float(t.get("bpm")), 3)
        num = int(t.get("num") or 4)
        den = int(t.get("den") or 4)
        start = round(float(t.get("start_sec") or 0.0), 6)
    except (TypeError, ValueError):
        return None
    lo, hi = TEMPO_BPM_RANGE
    if not (lo <= bpm <= hi) or not (1 <= num <= 16) or den not in TEMPO_DENS:
        return None
    out = {"bpm": bpm, "num": num, "den": den, "start_sec": start,
           "source": t.get("source") if t.get("source") in ("ixml", "daw") else "manual"}
    if out["source"] == "ixml":
        for k in ("track", "file", "varies", "bpm_range"):
            if t.get(k) is not None:
                out[k] = copy.deepcopy(t[k])
    return out

# 前の版の changeset のラベル（技術的な書き方）→ 画面の「元に戻す: ○○」の短い名前
_LABELS = (("ピッチを描いた", "鉛筆"), ("ガイドへ寄せる", "ガイドに合わせる"),
           ("つなぎのなだらかさ", "なだらかさ"), ("原音に戻す", "オリジナルに戻す"),
           ("で分割", "分割"), ("を結合", "結合"), ("を接続", "つなぐ"), ("を切り離し", "切り離し"),
           ("端を", "ノートの長さ"), ("倍", "ノートの長さ"), ("移動", "ノートの移動"),
           ("境界", "音素の境界"), ("セント", "ピッチ"), ("ピッチ曲線", "ピッチ"), ("歌詞", "歌詞"),
           (" ms", "ノートの移動"))


def short_label(label):
    s = str(label or "")
    for k, v in _LABELS:
        if k in s:
            return v
    return s or "編集"


def _cs_num(cid):
    try:
        return int(str(cid).lstrip("c"))
    except ValueError:
        return 0


def _now():
    from .model import now_iso
    return now_iso()


def _num_id(tid):
    try:
        return int(str(tid).lstrip("t"))
    except ValueError:
        return 0


def _backup_broken(path, err):
    bak = path + ".broken-%s" % time.strftime("%Y%m%d-%H%M%S")
    replace_file(path, bak)                      # 裏の準備が読んでいる瞬間は Windows では動かせない（待って取り直す）
    log.get().warning("session.json を読めないので %s に退避して作り直す: %s", bak, err)


def open_session(take, guide=None, project_dir=None, reuse=True, lyrics=None, guide_lyrics=None,
                 author="ai"):
    """`open_project`（MCP）の中身。テイク（と任意のガイド）から**セッション**を開いて、
    テイクのトラックを編集対象にする。(Session | None, Project, 前回選んでいたトラック)。

    - テイクのプロジェクトのディレクトリに `session.json` が無ければ作る（既存のプロジェクトに
      ガイドがあれば、ガイドのトラックも足してガイドに指定する）。
    - `guide` を渡したら、そのファイルのトラック（無ければ足す）をガイドに指定する。
      渡さなければセッションのガイドのまま（段階 2 までの「ガイドを省いて開き直すと前のガイドのまま」と同じ）。
    - サンプル列（`media.Samples`）で渡されたときはセッションを作らない（DAW 連携の API 用。今までどおり）。
    """
    tclip = M.as_clip(take)
    gclip = M.as_clip(guide) if guide else None
    if tclip.is_samples or (gclip is not None and gclip.is_samples):
        p = Project.open(take, guide, project_dir=project_dir, reuse=reuse,
                         lyrics=lyrics, guide_lyrics=guide_lyrics)
        return None, p, None
    for label, c in (("テイク", tclip), ("ガイド", gclip)):
        if c is not None and not os.path.exists(c.source):
            raise ProjectError("%sの音声が見つからない: %s" % (label, os.path.abspath(c.source)))
    tpath = os.path.abspath(tclip.source)
    if project_dir is None:
        project_dir = _default_project_dir(tclip, sha256_file(tpath))
    project_dir = os.path.abspath(project_dir)
    os.makedirs(project_dir, exist_ok=True)
    s = None
    if Session.exists(project_dir):
        try:
            s = Session.load(project_dir)
        except SessionError:
            raise                                    # 新しい版で作られた: 上書きしない
        except Exception as e:                       # noqa: BLE001  壊れていたら退避して作り直す
            _backup_broken(os.path.join(project_dir, SESSION_FILE), e)
            s = None
    created = s is None
    if created:
        s = Session(project_dir)
    last_current = s.current
    before = None if created else s.snapshot()
    what = []                                    # 開いたことでトラックの並び・ガイドが変わったもの（履歴の名前）
    try:
        tc = _clip_dict(tclip)
    except M.MediaError as e:
        raise ProjectError(str(e))

    prim = s.primary()
    if prim is None:
        # 最初のテイクのトラックを外した後: 同じファイルを足し直したトラックがあればそれを使う
        # （そのトラックの編集はそのプロジェクトのまま）。無ければ最初のテイクとして足し直す
        prim = s.find(tpath, tc)
        if prim is not None and prim["kind"] != "vocal":
            prim["kind"] = "vocal"
        if s.guide and prim is not None and s.guide == prim["id"]:
            s.guide = None
    elif prim["kind"] != "vocal":
        prim["kind"] = "vocal"                   # 開いたテイクは編集する（伴奏にされていても戻す）
    if prim is None:
        prim = s.add_track(tpath, kind="vocal", clip=tc, at=0, project_dir=PRIMARY_DIR,
                           source_id=tclip.source_id)
        what.append("最初のテイクを戻す")
    elif not (_same_path(prim["path"], tpath) and _same_clip_dict(prim.get("clip"), tc)):
        # 同じディレクトリで素材が変わった／ファイルが移った: 最初のトラックを今のテイクに差し替える
        other = s.find(tpath, tc)
        if other is not None and other is not prim:
            s.remove_track(other["id"])
        info = sf.info(tpath)
        sha = sha256_file(tpath)
        if sha != prim.get("sha256"):
            what.append("テイクの差し替え")
            # 別の素材になった: 前のテイクの位置・切れ目・消した部分・ミュート／ソロ・音量・パン・ガイドの歌詞の控えは引き継がない
            prim.update(offset_sec=0.0, mute=False, solo=False, gain_db=0.0, pan=0.0, cuts=[], mutes=[])
            prim.pop("guide_lyrics", None)
        prim.update(path=tpath, sha256=sha, sr=int(info.samplerate),
                    channels=int(info.channels), source_frames=int(info.frames), clip=tc,
                    duration_sec=round(float(tc["length_sec"]) if tc else
                                       int(info.frames) / int(info.samplerate), 6),
                    name=os.path.splitext(os.path.basename(tpath))[0],
                    source_id=tclip.source_id)
    if created and gclip is None:
        # 段階 2 までのプロジェクト（1 テイク＋ガイド）: ガイドのトラックを足してガイドに指定する
        old = _old_guide(project_dir)
        if old is not None:
            gpath, gc = old
            if os.path.exists(gpath):
                gt = s.find(gpath, gc) or s.add_track(gpath, kind="vocal", clip=gc)
                s.guide = gt["id"]
            else:
                s.missing_guide = {"path": gpath, "clip": gc}
    if s.missing_guide and gclip is None and not s.guide:
        mg = s.missing_guide
        if os.path.exists(mg["path"]):           # 見つかった: ガイドのトラックにする
            gt = s.find(mg["path"], mg.get("clip")) or s.add_track(mg["path"], kind="vocal",
                                                                   clip=mg.get("clip"))
            s.guide = gt["id"]
            s.missing_guide = None
    if gclip is not None:
        s.missing_guide = None
    if gclip is not None:
        gpath = os.path.abspath(gclip.source)
        try:
            gc = _clip_dict(gclip)
        except M.MediaError as e:
            raise ProjectError(str(e))
        if _same_path(gpath, tpath) and _same_clip_dict(gc, tc):
            raise ProjectError("テイクとガイドが同じ音声: %s" % os.path.basename(gpath))
        gt = s.find(gpath, gc) or s.add_track(gpath, kind="vocal", clip=gc)
        if gt["kind"] != "vocal":
            gt["kind"] = "vocal"
        s.guide = gt["id"]
    s.current = prim["id"]
    s.detect_tempo()                             # テンポがまだ無ければ iXML から（issue #18）
    if before is not None:
        # 開いたことで変わったトラックの並び・ガイド・素材も取り消しの履歴に入れる（issue #32）。入れないと、
        # その前の操作を Ctrl+Z したときに戻すスナップショットに足し直したトラックが無く、また消える。
        # ファイルが移っただけ（同じ中身）は入れない（取り消すと前の場所＝無いファイルに戻ってしまう）
        if before.get("guide") != s.guide and "ガイドの指定" not in what:
            what.append("ガイドの指定")
        if what:
            s.record_session("・".join(what), prim["id"], before, s.snapshot(),
                             last_current, prim["id"], author=author)
    s.save()
    p, _why = s.open_project_for(prim, reuse=reuse, lyrics=lyrics, guide_lyrics=guide_lyrics)
    return s, p, last_current


def _old_guide(project_dir):
    """既存の project.json のガイド（ファイルのパスとクリップの範囲）。無ければ None。"""
    pj = os.path.join(project_dir, "project.json")
    if not os.path.exists(pj):
        return None
    try:
        with open(pj, encoding="utf-8") as f:
            g = M.normalize_media(json.load(f).get("guide"))
    except Exception:                            # noqa: BLE001
        return None
    if not g or g.get("source_kind") == "samples" or not g.get("path") or g.get("pad"):
        return None
    clip = None
    if int(g.get("offset_frames") or 0) or int(g["frames"]) != int(g.get("source_frames") or g["frames"]):
        clip = {"offset_sec": round(int(g["offset_frames"]) / int(g["sr"]), 6),
                "length_sec": round(int(g["frames"]) / int(g["sr"]), 6)}
    return g["path"], clip
