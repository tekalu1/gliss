# -*- coding: utf-8 -*-
"""外部の AI から、DAW の中の Gliss（ARA プラグイン）のドキュメントを操作する中継（engine/docs/MCP.md §3-5）。

DAW のドキュメントを持つのはプラグインが起動したエンジン（`GLISS_CLIENT=ara`）1 本だけ。AI（Claude Code など）が
起動するエンジンは別のプロセスなので、プラグインのエンジンが **127.0.0.1 の口**をトークン付きで開き、
AI のエンジンが呼び出しをそこへ転送する。

| 側 | すること |
|---|---|
| プラグインのエンジン（`ara`） | `ara_open` で口を開き（`127.0.0.1`・ポートは OS が選ぶ）、`<置き場>\\<pid>.json` に接続先・トークン・DAW・文書の鍵を書く。転送されたツールを**自分のロックの中で**、選んだ修飾のトラックに切り替えて実行し、元の編集対象に戻す。終了時に記録を消す |
| AI のエンジン | `ara_documents` で記録を読み（動いていない pid の記録は無視）、`ara_attach` で修飾を選ぶ。以後のツールは全部転送する（`ara_detach` で戻る） |

置き場: 環境変数 `GLISS_ARA_SESSIONS_DIR` → 既定 `%APPDATA%\\Gliss\\ara-sessions`。

許可（プラグインのエンジンの環境変数 `GLISS_ARA_AI`。DAW から引き継ぐ）:
`off`（口を開かない）/ `read`（読むだけ）/ `edit`（既定。編集まで）/ `save`（編集と保存・書き出し）。
「編集」「保存・書き出し」の分け方は単体の Gliss と同じ（`bridge.category`）。DAW の文書の作り・トラックの増減・
保存（`load_project`・`add_track`・`save_project` など。プラグインの画面と同じ禁止の一覧）は許可に関係なく断る。

プラグインは 1 秒ごとの `ara_revs` で版の変化を拾って再生とノートと保存用の写しを取り直す。`ara_revs` の
`external`（外部の変更の番号）が変わったら、プラグインの画面に `project-changed`・`session-changed` を知らせる。

通信: 1 回の呼び出しごとに 1 本つなぎ、UTF-8 の JSON を 1 行送って 1 行受ける。
`{"token", "op": "list" | "select" | "call", "ara_id"?, "track_id"?, "tool"?, "args"?}` →
`{"ok": true, "result": …}` / `{"ok": false, "error": …}`。
"""
import ctypes
import ctypes.wintypes
import hmac
import inspect
import json
import os
import secrets
import socket
import socketserver
import threading
import time

from . import bridge, log

SESSIONS_ENV = "GLISS_ARA_SESSIONS_DIR"
ALLOW_ENV = "GLISS_ARA_AI"
FORMAT = "gliss-ara-relay"
VERSION = 1
HOST = "127.0.0.1"
MAX_LINE = 32 * 1024 * 1024        # 1 行（要求・応答）の上限
CONNECT_TIMEOUT = 3.0
LIST_TIMEOUT = 5.0
CALL_TIMEOUT = 900.0               # 30 秒を超える処理はジョブで返るので、普通はこれより十分短い

# AI のエンジンで転送せずに自分で答えるツール
LOCAL_TOOLS = frozenset({"ara_documents", "ara_attach", "ara_detach"})
# DAW の文書では使えないツール（プラグインの画面と同じ。plugin/src/ara/EngineCalls.cpp の isForbidden）
FORBIDDEN = frozenset({"new_project", "load_project", "open_project", "save_project", "close_project",
                       "add_track", "remove_track", "export_wav", "render_tracks",
                       "split_track", "join_track", "mute_track_range"})
# 呼んでも曲を変えないツール（外部の変更の番号を進めない。EngineCalls.cpp の shouldSyncAfter と同じ考え）
READ_ONLY = frozenset({"export_view_data", "track_overview", "prep_status", "engine_info", "asr_status",
                       "project_status", "plan_edit", "render_audition", "inspect_lyrics_score",
                       "cancel_job", "render_region", "render_preview", "render_view", "remeasure",
                       "transcribe", "pause_prep", "select_track", "get_job"})
# セッション（トラックの一覧・ガイド・テンポ）を変えうるもの: 画面に session-changed も知らせる
SESSION_TOOLS = frozenset({"set_track", "set_guide_track", "set_tempo", "undo", "redo"})
LEVELS = ("off", "read", "edit", "save")
# 外部のツールが走る間、編集対象を切り替えるので一緒に退避する mcp_server._state のキー
_STATE_KEYS = ("project", "track", "renderer", "renderer_backend", "renderer_audio_sig", "region")


# ================================================================ 共通
def sessions_dir():
    v = os.environ.get(SESSIONS_ENV)
    if v:
        return os.path.abspath(v)
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Gliss", "ara-sessions")


def level():
    """`GLISS_ARA_AI` の値（知らない値は既定の edit）。"""
    v = (os.environ.get(ALLOW_ENV) or "edit").strip().lower()
    return v if v in LEVELS else "edit"


def allow():
    lv = LEVELS.index(level())
    return {"relay": lv >= 1, "edit": lv >= 2, "save": lv >= 3}


def _win():
    return os.name == "nt"


_k32 = []


def _kernel32():
    """kernel32（HANDLE を 64 bit のまま受けるよう型を付ける）。"""
    if not _k32:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _k32.append(k)
    return _k32[0]


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not _win():
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    k32 = _kernel32()
    h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # 権限が無いだけ = 生きている
    try:
        code = ctypes.wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259                         # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def process_path(pid):
    """プロセスの実行ファイルのパス（取れなければ None）。"""
    if not _win():
        try:
            return os.readlink("/proc/%d/exe" % int(pid))
        except (OSError, ValueError, TypeError):
            return None
    k32 = _kernel32()
    try:
        h = k32.OpenProcess(0x1000, False, int(pid))
    except (TypeError, ValueError):
        return None
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return None
        return buf.value
    finally:
        k32.CloseHandle(h)


def _host_pid():
    """DAW のプロセス ID（プラグインが渡す `GLISS_ARA_HOST_PID`。無ければ親）。"""
    try:
        v = int(os.environ.get("GLISS_ARA_HOST_PID") or 0)
    except ValueError:
        v = 0
    if v > 0:
        return v
    return os.getppid() if hasattr(os, "getppid") else None


def _jsonable(o):
    if hasattr(o, "item"):
        try:
            return o.item()
        except (TypeError, ValueError):
            pass
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _dumps(obj):
    return (json.dumps(obj, ensure_ascii=False, default=_jsonable) + "\n").encode("utf-8")


def _err(msg, **kw):
    return dict(ok=False, error=msg, **kw)


# ================================================================ プラグインのエンジン（口を開く側）
_srv_state = {"server": None, "thread": None, "file": None, "token": None, "port": None,
              "work_key": None, "name": None, "dir": None, "seq": 0, "session_seq": 0, "track_id": None}
_srv_lock = threading.Lock()


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30                                         # 要求の 1 行を読むまで

    def handle(self):
        if self.client_address[0] != HOST:               # 127.0.0.1 にしか bind しないが、念のため
            return
        try:
            line = self.rfile.readline(MAX_LINE + 1)
        except OSError:
            return
        if not line:
            return
        if len(line) > MAX_LINE:
            out = _err("要求が大きすぎる")
        else:
            try:
                req = json.loads(line.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("JSON のオブジェクトでない")
            except (ValueError, UnicodeDecodeError) as e:
                req, out = None, _err("要求を読めない: %s" % e)
            if req is not None:
                token = _srv_state["token"]
                given = req.get("token")
                if not token or not isinstance(given, str) or not hmac.compare_digest(given, token):
                    log.get("relay").warning("トークンの合わない接続を断った")
                    out = _err("トークンが違う", permission="token")
                else:
                    self.connection.settimeout(None)     # 実行は長くかかりうる（応答を書くまで待たせる）
                    try:
                        out = _handle(req)
                    except Exception as e:               # noqa: BLE001  中継は落とさない
                        log.get("relay").exception("中継の要求で失敗")
                        out = _err(str(e))
        try:
            self.wfile.write(_dumps(out))
            self.wfile.flush()
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def _write_record():
    st = _srv_state
    ppid = _host_pid()
    daw = process_path(ppid) if ppid else None
    rec = {"format": FORMAT, "version": VERSION, "pid": os.getpid(), "engine": process_path(os.getpid()),
           "host": HOST, "port": st["port"], "token": st["token"],
           "daw_pid": ppid, "daw": os.path.basename(daw) if daw else None,
           "work_key": st["work_key"], "document": st["name"], "dir": st["dir"],
           "allow": allow(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    d = sessions_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%d.json" % os.getpid())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    st["file"] = path


def start(work_key, name, wd):
    """`ara_open` の後（プラグインのエンジンだけ）: 口を開いて記録を書く（開いていれば文書の情報を書き直す）。"""
    if not bridge.is_ara() or not allow()["relay"]:
        return None
    with _srv_lock:
        st = _srv_state
        st.update(work_key=str(work_key), name=name, dir=wd)
        if st["server"] is None:
            srv = _Server((HOST, 0), _Handler)
            st["server"], st["port"] = srv, srv.server_address[1]
            st["token"] = secrets.token_hex(32)
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                                 name="ara-relay", daemon=True)
            t.start()
            st["thread"] = t
            import atexit
            atexit.register(stop)
        try:
            _write_record()
        except OSError as e:
            log.get("relay").warning("中継の記録を書けない: %s", e)
        log.get("relay").info("外部の AI の中継: %s:%d（%s・%s）", HOST, st["port"], level(), st["file"])
        return {"port": st["port"], "file": st["file"], "allow": allow()}


def stop():
    """口を閉じて記録を消す（何度呼んでもよい）。"""
    with _srv_lock:
        st = _srv_state
        srv, path = st["server"], st["file"]
        st.update(server=None, thread=None, file=None, token=None, port=None)
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:                                # noqa: BLE001
            pass
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def external():
    """`ara_revs` に載せる外部の変更の番号（プラグインが画面に知らせるのに使う）。口を開いていなければ None。"""
    st = _srv_state
    if st["server"] is None:
        return None
    return {"seq": st["seq"], "session_seq": st["session_seq"], "track_id": st["track_id"]}


def _session():
    from . import mcp_ara
    from . import mcp_server as _srv
    s = _srv._state.get("session")
    if mcp_ara._doc() is None or s is None:
        raise RuntimeError("DAW のドキュメントが開かれていない")
    return s


def _track_row(s, t, editing=None):
    from . import prep
    pdir = s.project_dir_of(t)
    st = None
    try:
        st = prep.status(s.dir, t["id"])
    except Exception:                                    # noqa: BLE001
        st = None
    analyzed = os.path.exists(os.path.join(pdir, "cache", "take-analysis.json"))
    return {"track_id": t["id"], "ara_id": t.get("ara_id"), "name": t.get("name"),
            "daw_track": t.get("group"), "kind": t.get("kind"),
            "duration_sec": t.get("duration_sec"), "sr": t.get("sr"), "channels": t.get("channels"),
            "offset_sec": t.get("offset_sec"), "guide": t["id"] == s.guide,
            "analyzed": analyzed, "prep": st, "editing_in_plugin": t["id"] == editing}


def _list():
    from . import mcp_tracks as _mt
    s = _session()
    editing = _mt.current_track_id()
    st = _srv_state
    rows = [_track_row(s, t, editing) for t in list(s.tracks) if t.get("ara_id")]
    ppid = _host_pid()
    daw = process_path(ppid) if ppid else None
    return {"document": st["name"], "work_key": st["work_key"], "dir": st["dir"], "engine_pid": os.getpid(),
            "daw_pid": ppid, "daw": os.path.basename(daw) if daw else None,
            "allow": allow(), "tracks": rows}


def _find(s, ara_id=None, track_id=None):
    t = None
    if ara_id:
        t = s.find_ara(ara_id)
    elif track_id:
        t = next((x for x in s.tracks if x["id"] == track_id), None)
    if t is None or not t.get("ara_id"):
        raise RuntimeError("DAW の文書にその修飾が無い（%s）。DAW で消したか、別の文書。ara_documents で確かめる"
                           % (ara_id or track_id))
    if t.get("kind") != "vocal":
        raise RuntimeError("そのトラックは編集できない（%s）" % t.get("name"))
    return t


def _tools():
    from . import mcp_server as _srv
    return {f.__name__: f for f in _srv.TOOLS}


def denied(name, args):
    """外部からの呼び出しを断るなら返り値（dict）。許すなら None。"""
    if name in FORBIDDEN or name.startswith("ara_"):
        return _err("%s は DAW の文書では使えない（DAW の文書の作り・トラックの増減・保存は DAW がする）" % name,
                    tool=name, permission="ara")
    cat = bridge.category(name, args)
    a = allow()
    if cat is not None and not a[cat]:
        return _err("DAW の Gliss は AI の%sを許可していない（DAW を起動するときの環境変数 %s=%s で許可できる）"
                    % (bridge.LABELS[cat], ALLOW_ENV, "save" if cat == "save" else "edit"),
                    tool=name, permission=cat)
    return None


def _restore(s, saved, current_before):
    """外部のツールの後: プラグインの画面の編集対象に戻す。"""
    from . import mcp_server as _srv
    st = _srv._state
    tid = saved["track"]
    if st.get("track") != tid or st.get("project") is not saved["project"]:
        old = saved["project"]
        t = None
        if tid:
            try:
                t = s.track(tid)
            except Exception:                            # noqa: BLE001  消えた: 元の状態をそのまま戻す
                t = None
        if t is None or old is None:
            st.update(saved)
        elif (os.path.normcase(os.path.abspath(old.dir)) == os.path.normcase(os.path.abspath(s.project_dir_of(t)))
              and not s.guide_stale(t, old)):
            st.update(saved)
        else:                                            # 外部の操作で開き直しが要る（ガイドの指定が変わった…）
            p, _why = s.open_project_for(t)
            st.update(project=p, track=tid)
            _srv._invalidate_renderer()
    if s.current != current_before:
        s.current = current_before
        s.save()


def execute(tool, args, ara_id=None, track_id=None):
    """外部のツールを、選んだ修飾のトラックで実行する（プラグインのエンジンの中）。"""
    from . import mcp_server as _srv
    fn = _tools().get(tool)
    if fn is None or tool in LOCAL_TOOLS:
        return _err("知らないツール: %s" % tool, tool=tool)
    args = args if isinstance(args, dict) else {}
    no = denied(tool, args)
    if no is not None:
        log.get("relay").info("%s denied (%s)", tool, no.get("permission"))
        return no
    sig = inspect.signature(fn)
    try:
        ba = sig.bind_partial(**args)
    except TypeError as e:
        return _err("引数が合わない: %s" % e, tool=tool)
    if "author" in sig.parameters:
        ba.arguments["author"] = "ai"                    # 画面の履歴で AI の操作に印を付ける
    if not getattr(fn, "engine_lock", True):
        # ロックを取らないツール（get_job・engine_info…）: 走っているジョブの終わりを待たない。編集対象に依らない
        r = fn(*ba.args, **ba.kwargs)
        if tool == "get_job" and isinstance(r, dict) and r.get("status") in ("done", "error", "canceled"):
            _srv_state["seq"] += 1                       # 外部が始めた解析・書き出しが終わった（画面を描き直させる）
        return r
    with _srv._lock:
        s = _session()
        if tool == "select_track":                       # 外部の選択を変えるだけ（プラグインの画面の編集対象は変えない）
            want = _find(s, None, args.get("track_id"))
            return {"ok": True, "selected": True, "ara_id": want["ara_id"],
                    "track": _track_row(s, want, _srv._state.get("track")),
                    "next": "analyze_take（解析済みならすぐ返る）→ list_notes / list_deviations"}
        t = _find(s, ara_id, track_id)
        saved = {k: _srv._state.get(k) for k in _STATE_KEYS}
        current_before = s.current
        if saved["track"] != t["id"]:
            p, _why = s.open_project_for(t)
            _srv._state.update(project=p, track=t["id"])
            _srv._invalidate_renderer()
        try:
            r = fn(*ba.args, **ba.kwargs)
        finally:
            _restore(s, saved, current_before)
        ok = isinstance(r, dict) and r.get("ok") is not False
        if ok and tool not in READ_ONLY and not tool.startswith(("list_", "get_")):
            _srv_state["seq"] += 1
            _srv_state["track_id"] = t["id"]
            if tool in SESSION_TOOLS:
                _srv_state["session_seq"] += 1
        log.get("relay").info("外部の %s（%s）%s", tool, t.get("ara_id"), "ok" if ok else "失敗")
        return r


def _handle(req):
    op = req.get("op")
    if op == "list":
        return {"ok": True, "result": _list()}
    if op == "select":
        from . import mcp_server as _srv
        with _srv._lock:
            s = _session()
            t = _find(s, req.get("ara_id"), req.get("track_id"))
            return {"ok": True, "result": _track_row(s, t, _srv._state.get("track"))}
    if op == "call":
        tool = req.get("tool")
        if not isinstance(tool, str) or not tool:
            return _err("tool が無い")
        try:
            return {"ok": True, "result": execute(tool, req.get("args") or {}, req.get("ara_id"),
                                                  req.get("track_id"))}
        except RuntimeError as e:
            return _err(str(e))
    return _err("知らない op: %s" % op)


# ================================================================ AI のエンジン（転送する側）
_sel = {"work_key": None, "ara_id": None, "document": None}


def _records():
    """生きているプラグインのエンジンの記録（死んだ pid のものは無視し、消す）。"""
    d = sessions_dir()
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(d, n)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("format") != FORMAT:
            continue
        pid = rec.get("pid")
        alive = pid_alive(pid)
        if alive and rec.get("engine"):
            # pid が別のプロセスに使い回されていたら、そのプロセスにはトークンを送らない
            now = process_path(pid)
            alive = now is None or os.path.normcase(now) == os.path.normcase(rec["engine"])
        if not alive:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        out.append(rec)
    return out


def _request(rec, req, timeout):
    req = dict(req, token=rec.get("token"))
    with socket.create_connection((HOST, int(rec["port"])), timeout=CONNECT_TIMEOUT) as c:
        c.settimeout(timeout)
        c.sendall(_dumps(req))
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = c.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise OSError("応答が大きすぎる")
    if not buf:
        raise OSError("応答が無い")
    return json.loads(buf.decode("utf-8"))


def documents():
    """開いている DAW の文書（プラグインのエンジンごと）。つながらないものは error 付き。"""
    out = []
    for rec in _records():
        d = {"document": rec.get("document"), "work_key": rec.get("work_key"), "daw": rec.get("daw"),
             "daw_pid": rec.get("daw_pid"), "engine_pid": rec.get("pid")}
        try:
            r = _request(rec, {"op": "list"}, LIST_TIMEOUT)
            if r.get("ok"):
                d.update(r["result"])
            else:
                d["error"] = r.get("error")
        except (OSError, ValueError) as e:
            d["error"] = "つながらない: %s" % e
        out.append(d)
    return out


def attached():
    return _sel["work_key"] is not None


def selection():
    return dict(_sel) if attached() else None


def _record_for(work_key):
    for rec in _records():
        if rec.get("work_key") == work_key:
            return rec
    return None


def attach(ara_id=None, track_id=None, document=None):
    docs = [d for d in documents() if not d.get("error")]
    if not docs:
        raise RuntimeError("DAW で開いている Gliss の文書が無い（DAW のイベントに Gliss を挿して開く。"
                           "プラグインのエンジンが %s=off だと出ない）" % ALLOW_ENV)
    cands = docs
    if document is not None:
        key = str(document)
        cands = [d for d in docs if key in (d.get("work_key"), d.get("document"), str(d.get("engine_pid")))]
        if not cands:
            raise RuntimeError("その文書が無い: %s（ara_documents の work_key / document / engine_pid）" % key)
    if ara_id or track_id:
        cands = [d for d in cands if any((ara_id and r["ara_id"] == ara_id) or (track_id and r["track_id"] == track_id)
                                         for r in d.get("tracks", []))]
        if not cands:
            raise RuntimeError("その修飾が無い: %s（ara_documents で確かめる）" % (ara_id or track_id))
    if len(cands) > 1:
        raise RuntimeError("文書が %d つある。document（work_key）を指定する" % len(cands))
    d = cands[0]
    rows = [r for r in d.get("tracks", []) if r.get("kind") == "vocal"]
    if ara_id or track_id:
        row = next(r for r in rows if (ara_id and r["ara_id"] == ara_id) or (track_id and r["track_id"] == track_id))
    elif len(rows) == 1:
        row = rows[0]
    else:
        row = next((r for r in rows if r.get("editing_in_plugin")), None)
        if row is None:
            raise RuntimeError("修飾が %d つある。ara_id を指定する" % len(rows))
    rec = _record_for(d["work_key"])
    r = _request(rec, {"op": "select", "ara_id": row["ara_id"]}, LIST_TIMEOUT)
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or "選べない")
    _sel.update(work_key=d["work_key"], ara_id=row["ara_id"], document=d.get("document"))
    return d, r["result"]


def detach():
    was = selection()
    _sel.update(work_key=None, ara_id=None, document=None)
    return was


def forward(name, args):
    """選んだ DAW の文書のプラグインのエンジンでツールを呼ぶ。"""
    sel = selection()
    rec = _record_for(sel["work_key"])
    where = {"document": sel["document"], "work_key": sel["work_key"], "ara_id": sel["ara_id"]}
    if rec is None:
        return _err("DAW の文書（%s）が見つからない。DAW で閉じたか、プラグインのエンジンが止まった。"
                    "ara_documents で確かめて ara_attach し直すか、ara_detach で単体の Gliss に戻る"
                    % (sel["document"] or sel["work_key"]), tool=name, ara=where)
    try:
        r = _request(rec, {"op": "call", "tool": name, "args": args, "ara_id": sel["ara_id"]}, CALL_TIMEOUT)
    except (OSError, ValueError) as e:
        return _err("DAW の文書のエンジンにつながらない: %s" % e, tool=name, ara=where)
    if not r.get("ok"):
        return _err(r.get("error") or "中継に失敗", tool=name, ara=where)
    out = r.get("result")
    if isinstance(out, dict):
        if name == "select_track" and out.get("ok") and out.get("ara_id"):
            _sel["ara_id"] = out["ara_id"]
            where["ara_id"] = out["ara_id"]
        out = dict(out, ara=where)
    return out


__all__ = ["ALLOW_ENV", "FORBIDDEN", "LOCAL_TOOLS", "SESSIONS_ENV", "allow", "attach", "attached", "denied",
           "detach", "documents", "execute", "external", "forward", "level", "pid_alive", "selection",
           "sessions_dir", "start", "stop"]
