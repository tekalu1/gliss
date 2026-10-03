"""外部の AI の代わり: 別のプロセスのエンジン（`python -m vocal_engine.mcp`。GLISS_CLIENT なし）を stdio の MCP で起動し、
DAW（GlissARATest）で開いている Gliss の文書を一覧 → 選ぶ → 解析 → +100 セント、の順に呼ぶ（GlissARATest -relay が起動する）。

使い方（エンジンの python・cwd は engine。GLISS_ARA_SESSIONS_DIR はプラグインのエンジンと同じ）:
    <python> <wt>\\plugin\\tests\\relay_client.py <out>

結果は <out>\\relay-client.json。終了コード 0 = 全部の呼び出しが ok。
"""
import asyncio
import json
import os
import sys

MODIFICATION_ID = "audioModificationTestPersistentID 0"     # GlissARATest のドキュメントの修飾


def payload(res):
    sc = getattr(res, "structured_content", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for c in res.content:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"text": t}
    return {}


async def main(out):
    from mcp import StdioServerParameters
    from mcp.client import Client
    env = {k: v for k, v in os.environ.items() if k not in ("GLISS_CLIENT", "GLISS_ARA_HOST_PID")}
    env.update(PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(command=sys.executable, args=["-m", "vocal_engine.mcp"], cwd=os.getcwd(), env=env)
    log = {"calls": []}

    async def call(c, name, args=None):
        r = payload(await c.call_tool(name, args or {}))
        log["calls"].append({"tool": name, "args": args or {}, "ok": r.get("ok") is not False,
                             "error": r.get("error")})
        if r.get("ok") is False:
            raise RuntimeError("%s failed: %s" % (name, r.get("error")))
        return r

    ok = False
    try:
        async with Client(params, read_timeout_seconds=300) as c:
            docs = (await call(c, "ara_documents"))["documents"]
            log["documents"] = docs
            mine = [d for d in docs if any(t.get("ara_id") == MODIFICATION_ID for t in d.get("tracks", []))]
            if len(mine) != 1:
                raise RuntimeError("expected one document with %s, got %d" % (MODIFICATION_ID, len(mine)))
            log["attach"] = await call(c, "ara_attach", {"ara_id": MODIFICATION_ID, "document": mine[0]["work_key"]})
            await call(c, "analyze_take", {"background": False})
            before = (await call(c, "list_notes", {"kind": "note"}))["notes"]
            r = await call(c, "shift_pitch", {"cents": 100, "start_sec": 0, "end_sec": 6.2})
            log["shift"] = {"ara": r.get("ara"), "changeset": r.get("changeset")}
            after = (await call(c, "list_notes", {"kind": "note"}))["notes"]
            log["notes_before"], log["notes_after"] = len(before), len(after)
            await call(c, "ara_detach")
            ok = True
    except Exception as e:                          # noqa: BLE001
        log["error"] = "%s: %s" % (type(e).__name__, e)
    log["ok"] = ok
    with open(os.path.join(out, "relay-client.json"), "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
