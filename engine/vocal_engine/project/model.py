# -*- coding: utf-8 -*-
"""編集リストのデータモデル（非破壊）。

編集は「元音声＋編集リスト」で表す。編集リストの 1 件は

    {id, kind, target, params, author, changeset, created_at, note}

target は音符（note_id）・時間範囲（start_sec, end_sec）・**音素境界（boundary_id）**のどれか。
kind は pitch_shift / pitch_curve / move / stretch（段階1）＋ **move_boundary**（段階2）。

`move_boundary` は「音素の境目を横に動かす」編集で、**隣り合う 2 つの音素の長さが同時に変わる**。
レンダリングでは前後 2 区間の伸縮に展開される（`render/pipeline.py`）。
展開に必要な値（両隣の区間と、その時点での伸縮比）は params に入れて自己完結させてあるので、
再アラインで音素の番号が変わっても、記録済みの編集の意味は変わらない。

**後ろをずらさない編集**（`project/timing.py`）のために 3 つ足した:

| kind | target | params | 意味 |
|---|---|---|---|
| `silence` | 範囲（start = end = その時刻） | `sec` | その時刻に無音を挟む（切り離したノートを縮めてできた隙間） |
| `crop` | 範囲 | — | その区間を出さない（切り離したノートを隙間へ伸ばしたぶん） |
| `connection` | 範囲（前のノートの尻〜次のノートの頭） | `a`, `b`, `connected` | 隣り合うノートの**接続／切り離し**の状態。時間には効かない。undo で戻るように編集リストに置く |

**ノートの変わり目・鉛筆・カット**のために 4 つ足した:

| kind | target | params | 意味 |
|---|---|---|---|
| `transition` | 範囲（前のノートの尻〜次のノートの頭） | `a`, `b`, `value` | 接続された境目の**なだらかさ**（0 = 段差、0.5 = 自動、1 = ゆっくり）。後勝ち。`project/pitch.py` |
| `pitch_draw` | 範囲（描いた区間） | `points`（[[素材の秒, MIDI], ...]）, `ramp_sec`（任意で `ramp_l_sec` / `ramp_r_sec`） | 鉛筆で描いたピッチ。範囲の中は描いた値に置き換え、両端は `ramp_sec` かけて元の曲線へつなぐ。`restore: true` は「元に戻す線」（描く線が原音そのもの。`set_pitch_curve` の mode="restore"） |
| `split` | 範囲（start = end = 分割の時刻） | `note_id`, `right_id` | ノートをその時刻で 2 つに分ける（`project/notes_edit.py`）。時刻で持つので解析し直しても残る |
| `merge` | 範囲（start = end = 境目の時刻） | `a`, `b` | 接して並ぶ 2 つのノートを 1 つにする |

**フェード**（issue #20。`project/fades.py`）: `fade` | 範囲（ノートの頭〜尻） | `side`（in / out）, `sec`（編集後の秒） |
ノートの頭か尻から sec 秒、音量だけを 0 から／0 へ（錨はノートの端の時刻。後勝ち）

**無音にする**（issue #17。右クリック「無音にする」・Del）:

| kind | target | params | 意味 |
|---|---|---|---|
| `mute` | 範囲（ノートの頭〜尻） | — | その区間の音を消す（長さ・位置は変えない。隣とは 20 ms のクロスフェード）。ピッチの編集は残る |
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

EDIT_KINDS = ("pitch_shift", "pitch_curve", "move", "stretch", "move_boundary",
              "silence", "crop", "connection", "transition", "pitch_draw", "split", "merge",
              "mute", "fade")
STRETCH_RATIO_RANGE = (0.02, 50.0)   # 内部の伸縮（timing.py が組む）で許す比。ツールの入口は 0.25〜4
AUTHORS = ("human", "ai")


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _copy_json(o):
    """JSON の値（dict・list・数値・文字列）の深いコピー。`asdict` と同じ結果で、ずっと速い
    （project.json を書くたびに編集の履歴を全部コピーしていて、編集 400 件で 0.04 秒かかっていた。issue #63）。"""
    t = type(o)
    if t is dict:
        return {k: _copy_json(v) for k, v in o.items()}
    if t is list:
        return [_copy_json(v) for v in o]
    return o


@dataclass
class Target:
    type: str                       # "note" | "range" | "boundary"
    note_id: str | None = None
    start_sec: float | None = None
    end_sec: float | None = None
    boundary_id: str | None = None
    phoneme_id: str | None = None

    def to_json(self):
        d = {"type": self.type, "note_id": self.note_id, "start_sec": self.start_sec,
             "end_sec": self.end_sec, "boundary_id": self.boundary_id, "phoneme_id": self.phoneme_id}
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_json(cls, d):
        return cls(type=d["type"], note_id=d.get("note_id"),
                   start_sec=d.get("start_sec"), end_sec=d.get("end_sec"),
                   boundary_id=d.get("boundary_id"), phoneme_id=d.get("phoneme_id"))

    @classmethod
    def note(cls, note_id):
        return cls(type="note", note_id=note_id)

    @classmethod
    def range(cls, start_sec, end_sec, phoneme_id=None):
        return cls(type="range", start_sec=float(start_sec), end_sec=float(end_sec),
                   phoneme_id=phoneme_id)

    @classmethod
    def boundary(cls, boundary_id, start_sec, end_sec):
        """境界そのもの。start/end は展開に使う「左右の音素をあわせた範囲」。"""
        return cls(type="boundary", boundary_id=boundary_id,
                   start_sec=float(start_sec), end_sec=float(end_sec))

    def describe(self):
        if self.type == "note":
            return "note %s" % self.note_id
        if self.type == "boundary":
            return "境界 %s" % self.boundary_id
        if self.phoneme_id:
            return "音素 %s（%.3f–%.3f s）" % (self.phoneme_id, self.start_sec or 0.0,
                                              self.end_sec or 0.0)
        return "range %.3f–%.3f s" % (self.start_sec or 0.0, self.end_sec or 0.0)


@dataclass
class Edit:
    id: str
    kind: str
    target: Target
    params: dict
    author: str = "ai"
    changeset: str | None = None
    created_at: str = field(default_factory=now_iso)
    note: str | None = None

    def __post_init__(self):
        if self.kind not in EDIT_KINDS:
            raise ValueError("未知の編集 kind: %r（使えるのは %s）" % (self.kind, ", ".join(EDIT_KINDS)))
        if self.author not in AUTHORS:
            raise ValueError("author は human か ai")
        validate_params(self.kind, self.params)

    def to_json(self):
        return {"id": self.id, "kind": self.kind, "target": self.target.to_json(),
                "params": _copy_json(self.params), "author": self.author, "changeset": self.changeset,
                "created_at": self.created_at, "note": self.note}

    @classmethod
    def from_json(cls, d):
        return cls(id=d["id"], kind=d["kind"], target=Target.from_json(d["target"]),
                   params=dict(d.get("params", {})), author=d.get("author", "ai"),
                   changeset=d.get("changeset"), created_at=d.get("created_at", now_iso()),
                   note=d.get("note"))

    def describe(self):
        p = self.params
        if self.kind == "pitch_shift":
            return "%s を %+.1f セント" % (self.target.describe(), p["cents"])
        if self.kind == "pitch_curve":
            return "%s にピッチ曲線 %d 点" % (self.target.describe(), len(p["points"]))
        if self.kind == "move":
            return "%s を %+.1f ms 移動" % (self.target.describe(), p["ms"])
        if self.kind == "stretch":
            return "%s を %.3f 倍に伸縮" % (self.target.describe(), p["ratio"])
        if self.kind == "silence":
            return "%.3f s に無音 %.0f ms" % (self.target.start_sec or 0.0, p["sec"] * 1000)
        if self.kind == "crop":
            return "%s を切り取り" % self.target.describe()
        if self.kind == "connection":
            return "%s｜%s を%s" % (p["a"], p["b"], "接続" if p["connected"] else "切り離し")
        if self.kind == "transition":
            return "%s｜%s のつなぎ %.2f" % (p["a"], p["b"], p["value"])
        if self.kind == "pitch_draw":
            if p.get("restore"):
                return "%s のピッチを元に戻した" % self.target.describe()
            return "%s にピッチを描いた（%d 点）" % (self.target.describe(), len(p["points"]))
        if self.kind == "split":
            return "%.3f s でノートを分割" % (self.target.start_sec or 0.0)
        if self.kind == "mute":
            return "%s を無音に" % self.target.describe()
        if self.kind == "fade":
            return "%s のフェード%s %.0f ms" % (self.target.describe(),
                                              "イン" if p["side"] == "in" else "アウト", p["sec"] * 1000)
        if self.kind == "merge":
            return "%s と %s を結合" % (p["a"], p["b"])
        if self.kind == "move_boundary":
            return "%s を %+.0f ms 移動（%s｜%s）" % (
                self.target.describe(), p["ms"], p.get("left_text") or "-",
                p.get("right_text") or "-")
        return self.kind


def validate_params(kind, params):
    if kind == "pitch_shift":
        c = float(params.get("cents", 0.0))
        if abs(c) > 2400:
            raise ValueError("cents は ±2400（±2 オクターブ）まで")
        params["cents"] = c
    elif kind == "pitch_curve":
        pts = params.get("points")
        if not pts or len(pts) < 2:
            raise ValueError("pitch_curve の points は [[相対秒, セント], ...] で 2 点以上")
        clean = []
        for t, c in pts:
            clean.append([float(t), float(c)])
        clean.sort(key=lambda p: p[0])
        params["points"] = clean
    elif kind == "move":
        ms = float(params.get("ms", 0.0))
        if abs(ms) > 2000:
            raise ValueError("move は ±2000 ms まで")
        params["ms"] = ms
    elif kind == "stretch":
        r = float(params.get("ratio", 1.0))
        lo, hi = STRETCH_RATIO_RANGE
        if not (lo <= r <= hi):
            raise ValueError("stretch の ratio は %.2f〜%.0f" % (lo, hi))
        params["ratio"] = r
    elif kind == "silence":
        v = float(params.get("sec", 0.0))
        if not (0.0 < v <= 30.0):
            raise ValueError("silence の sec は 0〜30 秒")
        params["sec"] = v
    elif kind == "crop":
        pass
    elif kind == "connection":
        for k in ("a", "b"):
            if not params.get(k):
                raise ValueError("connection の params に %s（ノート id）が無い" % k)
        params["connected"] = bool(params.get("connected"))
    elif kind == "transition":
        for k in ("a", "b"):
            if not params.get(k):
                raise ValueError("transition の params に %s（ノート id）が無い" % k)
        v = float(params.get("value", 0.5))
        if not (0.0 <= v <= 1.0):
            raise ValueError("transition の value は 0〜1（0 = 段差、0.5 = 自動、1 = ゆっくり）")
        params["value"] = v
    elif kind == "pitch_draw":
        pts = params.get("points")
        if not pts or len(pts) < 2:
            raise ValueError("pitch_draw の points は [[素材の秒, MIDI], ...] で 2 点以上")
        clean = sorted([[float(t), float(m)] for t, m in pts], key=lambda q: q[0])
        for _, m in clean:
            if not (12.0 <= m <= 120.0):
                raise ValueError("pitch_draw の音程は MIDI ノート番号（12〜120）")
        params["points"] = clean
        params["ramp_sec"] = float(params.get("ramp_sec", 0.04))
        if not (0.0 <= params["ramp_sec"] <= 0.5):
            raise ValueError("pitch_draw の ramp_sec は 0〜0.5 秒")
        for k in ("ramp_l_sec", "ramp_r_sec"):         # 片側だけ短い（「オリジナルに戻す」で切った側）
            if params.get(k) is not None:
                params[k] = float(params[k])
                if not (0.0 <= params[k] <= 0.5):
                    raise ValueError("pitch_draw の %s は 0〜0.5 秒" % k)
    elif kind == "split":
        pass
    elif kind == "mute":
        pass
    elif kind == "fade":
        if params.get("side") not in ("in", "out"):
            raise ValueError("fade の side は in か out")
        v = float(params.get("sec", 0.0))
        if not (0.0 < v <= 30.0):
            raise ValueError("fade の sec は 0〜30 秒")
        params["sec"] = v
    elif kind == "merge":
        for k in ("a", "b"):
            if not params.get(k):
                raise ValueError("merge の params に %s（ノート id）が無い" % k)
    elif kind == "move_boundary":
        for k in ("left_sec", "boundary_sec", "right_sec", "left_ratio", "right_ratio"):
            if k not in params:
                raise ValueError("move_boundary の params に %s が無い"
                                 "（move_boundary ツールから作ること）" % k)
            params[k] = float(params[k])
        params["ms"] = float(params.get("ms", 0.0))
        if abs(params["ms"]) > 2000:
            raise ValueError("move_boundary は ±2000 ms まで")
        for k in ("left_ratio", "right_ratio"):
            if not (0.05 <= params[k] <= 20.0):
                raise ValueError("move_boundary の %s が外れている: %.3f" % (k, params[k]))
    return params


# 描画の版（保存した編集から音を作る仕組みの版。`Project.render_version`・アーカイブの `render_version`）。
# 1: 0.1.0-beta.6 まで。2: ピッチ曲線を重ねたときのつなぎ目で、前後の Segment の点を切る（`pitch._clip_curve`。
# 隣のノートのずらし量が漏れない）。曲は作ったときの版のまま鳴らし、上げるのは利用者が明示したとき（`set_render_version`）だけ
RENDER_VERSION = 2


@dataclass
class Changeset:
    """取り消しの単位。ops は編集リストへの操作の列（追加・削除）。"""
    id: str
    label: str
    author: str
    created_at: str
    ops: list                       # [{"op": "add", "edit": {...}} | {"op": "remove", "edit_id": "e003"}]
    undone: bool = False
    discarded: bool = False         # 当て直しで捨てた（undone のまま、redo の対象にもしない）

    def to_json(self):
        return {"id": self.id, "label": self.label, "author": self.author, "created_at": self.created_at,
                "ops": _copy_json(self.ops), "undone": self.undone, "discarded": self.discarded}

    def to_json_shallow(self):
        """`to_json` と同じ値で、ops をコピーしない（すぐ JSON に書き出すだけのとき。`Project.save`）。"""
        return {"id": self.id, "label": self.label, "author": self.author, "created_at": self.created_at,
                "ops": self.ops, "undone": self.undone, "discarded": self.discarded}

    @classmethod
    def from_json(cls, d):
        return cls(id=d["id"], label=d.get("label", ""), author=d.get("author", "ai"),
                   created_at=d.get("created_at", now_iso()), ops=list(d.get("ops", [])),
                   undone=bool(d.get("undone", False)),
                   discarded=bool(d.get("discarded", False)))

    def summary(self):
        adds = sum(1 for o in self.ops if o.get("op") == "add")
        rms = sum(1 for o in self.ops if o.get("op") == "remove")
        return {"id": self.id, "label": self.label, "author": self.author,
                "created_at": self.created_at, "added": adds, "removed": rms,
                "undone": self.undone, "discarded": self.discarded}
