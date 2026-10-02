# -*- coding: utf-8 -*-
"""MCP サーバーを stdio で立てて 1 往復する。

`open_project` → `analyze_take` → `shift_pitch` → `render_preview` → `remeasure`
"""
import json
import os

import pytest

from conftest import GUIDE, TAKE, VENV_PY, needs_clips, needs_model

pytestmark = [needs_clips, needs_model,
              pytest.mark.skipif(not os.path.exists(VENV_PY), reason=".venv の python が無い")]

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def payload(res):
    """CallToolResult → dict（サーバーは JSON テキストで返す）。"""
    if getattr(res, "structured_content", None):
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for c in res.content:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"text": t}
    return {}


def _client():
    """stdio でサーバーを起動するクライアント。

    async generator の fixture にすると anyio のキャンセルスコープが
    別タスクで閉じられて落ちるので、各テストの中で with する。
    """
    from mcp import StdioServerParameters
    from mcp.client import Client
    params = StdioServerParameters(
        command=VENV_PY, args=["-m", "vocal_engine.mcp"], cwd=ENGINE,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return Client(params, read_timeout_seconds=300)


async def call(c, name, args=None):
    return payload(await c.call_tool(name, args or {}))


async def test_tools_are_listed():
  async with _client() as client:
    # サーバーの表示名はアプリ名（issue #29。パッケージは内部名の vocal_engine のまま）
    info = client.server_info
    assert info is not None and info.name == "gliss"
    assert (client.instructions or "").startswith("Gliss")
    names = [t.name for t in (await client.list_tools()).tools]
    for want in ("open_project", "analyze_take", "get_pitch", "list_notes", "list_deviations",
                 "get_phonemes", "set_lyrics", "move_boundary",
                 "shift_pitch", "set_pitch_curve", "move_note", "stretch",
                 "correct_to_guide", "reset_to_original", "undo", "render_preview",
                 "render_view", "remeasure", "list_changes", "get_job", "cancel_job"):
        assert want in names
    t = {x.name: x for x in (await client.list_tools()).tools}["shift_pitch"]
    props = t.input_schema["properties"]
    assert "cents" in props and "note_id" in props
    assert "a" not in props and "kw" not in props    # ラッパの引数が漏れていないこと


async def test_round_trip(tmp_path):
  async with _client() as client:
    r = await call(client, "open_project", {"take_path": TAKE, "guide_path": GUIDE,
                                            "project_dir": str(tmp_path / "proj")})
    assert r["ok"] and r["take"]["sha256"]
    assert os.path.exists(r["log"])

    a = await call(client, "analyze_take")
    assert a["ok"] and a["f0"]["n_voiced"] > 0
    assert a["notes"]["pitched"] > 0
    assert a["phonemes"]["supported"] is True
    assert a["phonemes"]["has_lyrics"] is True

    notes = await call(client, "list_notes", {"kind": "note"})
    nid = notes["notes"][0]["id"]

    p0 = await call(client, "get_pitch", {"start_sec": 0.0, "end_sec": 1.0})
    assert p0["ok"] and "median_hz" in p0
    assert not any(isinstance(v, list) for k, v in p0.items() if k != "range_sec")

    s = await call(client, "shift_pitch", {"cents": 200, "note_id": nid})
    assert s["ok"] and s["total_edits"] == 1
    cs = s["changeset"]

    pv = await call(client, "render_preview", {"start_sec": 0.0, "end_sec": 2.0})
    assert pv["ok"] and os.path.exists(pv["path"])
    assert pv["edited_chunks"] >= 1
    assert pv["backend"] == "praat"                   # 既定は Praat（2026-09-23 から）
    pv2 = await call(client, "render_preview", {"start_sec": 0.0, "end_sec": 2.0, "backend": "psola"})
    assert pv2["ok"] and pv2["backend"] == "psola"    # 自前は引数で選べる

    rm = await call(client, "remeasure", {"start_sec": 0.0, "end_sec": 2.0})
    assert rm["ok"] and rm["after"]["n_voiced"] > 0

    vw = await call(client, "render_view", {"start_sec": 0.0, "end_sec": 2.0})
    assert vw["ok"] and os.path.exists(vw["path"]) and vw["path"].endswith(".png")

    ch = await call(client, "list_changes")
    assert len(ch["changesets"]) == 1

    u = await call(client, "undo", {"changeset_id": cs})
    assert u["ok"] and u["total_edits"] == 0


async def test_errors_come_back_as_json():
  async with _client() as client:
    r = await call(client, "list_notes")          # プロジェクトを開いていない
    assert r["ok"] is False and "open_project" in r["error"]


async def test_background_job(tmp_path):
    """長い処理の逃げ道（job_id → get_job）が動くこと。"""
    import asyncio
    async with _client() as client:
        await call(client, "open_project", {"take_path": TAKE,
                                            "project_dir": str(tmp_path / "proj")})
        await call(client, "analyze_take")
        r = await call(client, "render_preview", {"background": True})
        assert r["status"] == "running" and r["job_id"]
        for _ in range(60):
            j = await call(client, "get_job", {"job_id": r["job_id"]})
            if j["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)
        assert j["status"] == "done", j
        assert os.path.exists(j["path"])
        bad = await call(client, "get_job", {"job_id": "job-nope"})
        assert bad["ok"] is False
