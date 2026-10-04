# -*- coding: utf-8 -*-
"""MCP: 補正後のノートごとの残差を測る・音程の編集をまとめて当てる。

| ツール | 何をするか |
|---|---|
| `measure_against_guide(start_sec?, end_sec?, note_ids?, render?, threshold_cents?, threshold_ms?, suggest_cents?, limit?)` | ノートごとの、ガイドとの音程の差（セント）・発音の頭のずれ（ms）の before / edited / after と対応の有無（`project/guide_measure.py`） |
| `apply_edits(edits, label?, author?)` | 複数の `shift_pitch` / `set_pitch_curve` を 1 つの changeset で当てる（取り消し 1 回） |

`mcp_server.py` の末尾から import される（`_tool` などはそちらのものを使う）。
"""
import json
import os
import uuid

from . import mcp_server as _srv
from .project import ProjectError

_ok = _srv._ok
_tool = _srv._tool
MAX_BATCH = 2000


def _over(row, threshold_cents, threshold_ms):
    def last(d):
        for k in ("after", "edited", "before"):
            if d.get(k) is not None:
                return d[k]
        return None
    pc, tm = last(row["pitch_cents"]), last(row["timing_ms"])
    return ((threshold_cents is not None and pc is not None and abs(pc) >= threshold_cents)
            or (threshold_ms is not None and tm is not None and abs(tm) >= threshold_ms))


@_tool
def measure_against_guide(start_sec: float = None, end_sec: float = None, note_ids: list = None,
                          render: bool = True, backend: str = "praat",
                          threshold_cents: float = None, threshold_ms: float = None,
                          suggest_cents: float = None, limit: int = 300,
                          background: bool = None) -> dict:
    """**補正の前後の、ノートごとのガイドとの残差**（音程のセント・発音の頭の ms・対応の有無）。

    物差しは correct_to_guide と同じ: 音程は音程で対応するガイドのノートの中心との差（+ がテイクの方が高い）、
    タイミングは「ガイドに合わせる」が合わせる**発音の頭の組**の、合わせる先（タイムライン上のガイドの頭）との差
    （+ がテイクの方が遅い。ノートの頭どうしの差ではない）。各値は 3 つ:
      before = 元の音、edited = 編集の中身から求めた値（再合成しない）、
      after = 編集後の音を再合成して測り直した値（render=True。F0 は解析と同じ方式、頭は拾い直したもの）。
    rows[]: note_id・start_sec/end_sec（編集前）・edited_start_sec/edited_end_sec（編集後）・guide（対応する
      ガイドのノート。無ければ reason）・pitch_cents・timing_ms・onset_paired（1 対 1 の発音の頭の組があるか。
      無ければ timing_reason）・shape_abs_cents_median（編集後の音とその時刻のガイドの音程の差の中央値）・confidence。
    summary: 音程・タイミングの before / edited / after の |値| の中央値・90% 点・最大。
    threshold_cents / threshold_ms: どちらかを超えるノートだけ rows に入れる（after → edited → before の順で
      ある値を見る。summary は全部のノート）。
    suggest_cents: |残差| がこれ以上のノートに、残差を打ち消す shift_pitch を suggested_edits に入れる
      （そのまま apply_edits(edits=suggested_edits) に渡せる。ビブラートなど揺れの形は保つ）。
    全部の行は JSON ファイル（path）にも書く。長い範囲はジョブ（get_job）。
    """
    from .project import guide_measure as GM
    p = _srv._project()
    p.reload_if_changed()
    t0, t1 = _srv._range(start_sec, end_sec)
    if background is None:
        background = bool(render) and (t1 - t0) * 2.0 > _srv.JOB_THRESHOLD_SEC

    def work():
        rr = None
        if render:
            from .render.base import resolve_backend_name
            from .render.region import RegionRenderer
            key = (resolve_backend_name(backend), "mono")
            rr = _srv._state["region"].get(key)
            if rr is None:
                rr = RegionRenderer.for_project(p, backend=backend, channels="mono")
                _srv._state["region"] = {key: rr}
        with _srv._prep_yield():
            res = GM.measure(p, start_sec, end_sec, note_ids=note_ids, render=bool(render),
                             backend=backend, renderer=rr)
        path = os.path.join(p.sub("renders"), "measure-%s.json" % uuid.uuid4().hex[:8])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
        rows = res["rows"]
        if threshold_cents is not None or threshold_ms is not None:
            rows = [r for r in rows if _over(r, threshold_cents, threshold_ms)]
        out = dict(res, path=os.path.abspath(path), total=len(res["rows"]), returned=min(len(rows), int(limit)),
                   rows=rows[:int(limit)], truncated=len(rows) > int(limit),
                   note="pitch_cents は + がテイクの方が高い。timing_ms は + がテイクの方が遅い（発音の頭の組。"
                        "correct_to_guide が合わせる物差し）。after は編集後の音を測り直した値")
        if suggest_cents is not None:
            sug = []
            for r in res["rows"]:
                v = r["pitch_cents"]["after"] if render else r["pitch_cents"]["edited"]
                if v is not None and abs(v) >= float(suggest_cents):
                    sug.append({"op": "shift_pitch", "note_id": r["note_id"], "cents": round(-v, 1)})
            out["suggested_edits"] = sug
        return out

    if background:
        return _srv._submit_job("measure_against_guide", work)
    return _ok(**work())


def _batch_specs(p, edits):
    """apply_edits の 1 件ずつ → (外す id, 入れる spec)。"""
    from .project import pitch as PI
    rm, specs = [], []
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            raise ProjectError("edits[%d] は {op, ...} の形" % i)
        op = e.get("op") or e.get("kind")
        try:
            if op in ("shift_pitch", "pitch_shift"):
                t = _srv._resolve_target(e.get("note_id"), e.get("start_sec"), e.get("end_sec"))
                specs.append({"kind": "pitch_shift", "target": t,
                              "params": {"cents": float(e["cents"])}, "note": e.get("note")})
            elif op == "set_pitch_curve":
                mode = e.get("mode", "offset")
                if mode == "draw":
                    r, s, _ = PI.draw_specs(p, e["points"],
                                            ramp_sec=float(e.get("ramp_ms", 40.0)) / 1000.0)
                    rm += [x for x in r if x not in rm]
                    specs += s
                elif mode == "offset":
                    t = _srv._resolve_target(e.get("note_id"), e.get("start_sec"), e.get("end_sec"))
                    specs.append({"kind": "pitch_curve", "target": t,
                                  "params": {"points": e["points"]}, "note": e.get("note")})
                else:
                    raise ProjectError("mode は offset か draw")
            else:
                raise ProjectError("op は shift_pitch か set_pitch_curve")
        except (KeyError, ValueError, TypeError, PI.PitchError, ProjectError) as ex:
            raise ProjectError("edits[%d]（%s）: %s" % (i, op, ex))
    return rm, specs


@_tool
def apply_edits(edits: list, label: str = None, author: str = "ai", group: str = None) -> dict:
    """**音程の編集をまとめて当てる**（1 つの changeset = undo 1 回）。

    edits: [{"op": "shift_pitch", "cents": -12.5, "note_id": "n012"},
            {"op": "shift_pitch", "cents": 8, "start_sec": 41.20, "end_sec": 41.38},
            {"op": "set_pitch_curve", "mode": "offset", "points": [[0, 0], [0.2, -20]], "note_id": "n013"},
            {"op": "set_pitch_curve", "mode": "draw", "points": [[42.10, 70.0], [42.30, 70.2]], "ramp_ms": 40}]
    中身は 1 件ずつ shift_pitch / set_pitch_curve を呼んだのと同じ（順番も同じ。鉛筆の後のずらしは鉛筆の線にも足される）。
    1 件でも不正なら何も当てずに ok=false（どれが悪いかを error に書く）。measure_against_guide の
    suggested_edits をそのまま渡せる。
    """
    if not edits:
        raise ProjectError("edits が空")
    if len(edits) > MAX_BATCH:
        raise ProjectError("edits は %d 件まで" % MAX_BATCH)
    p = _srv._project()
    p.reload_if_changed()
    rm, specs = _batch_specs(p, edits)
    cs = p.apply_changes(rm, specs, author=author,
                         label=label or "音程の編集をまとめて（%d 件）" % len(specs))
    _srv._rec(p, cs, "ピッチ", group=group)
    return _ok(changeset=cs.id, applied=len(specs), replaced=len(rm), total_edits=len(p.edits),
               next="measure_against_guide / render_preview で確かめる。戻すなら undo")


TOOLS = [measure_against_guide, apply_edits]
