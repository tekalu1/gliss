# -*- coding: utf-8 -*-
"""画面（Gliss）と AI 側のエンジンの橋渡し（`bridge.json`）。

画面が起動するエンジンと、Claude Code などの AI が起動するエンジンは**別のプロセス**。
画面は自分のエンジンに `GLISS_CLIENT=app` を渡す（DAW の ARA プラグインが起動するエンジンは `GLISS_CLIENT=ara`。
画面と同じく許可に従わない）。それ以外のプロセス（= AI）は、
画面が書いた `bridge.json` を**ツールを呼ぶたびに**読み、次の 2 つに従う:

- `allow`: AI に許すこと。`edit`（編集。既定 true）/ `save`（保存・書き出し。既定 false）。
  許していないツールは `{"ok": false, "error": …}` を返す（Gliss の ヘルプ > AI とつなぐ で許可できる旨）。
- `project`: 画面で今開いている曲（`.gliss`・無題の作業場所・旧形式のディレクトリ）と編集中のトラック。
  `load_project()` を引数なしで呼ぶとこれを開く。

置き場: 環境変数 `GLISS_BRIDGE` → 既定 `%APPDATA%\\Gliss\\bridge.json`（画面の userData と同じ場所）。

```json
{
  "version": 1,
  "allow": {"edit": true, "save": false},
  "project": {"path": "D:\\\\曲\\\\x.gliss", "kind": "gliss", "name": "x",
              "track": "t1", "track_name": "take1"},
  "updated_at": "2026-09-27T12:00:00.000Z"
}
```

どのツールが「編集」「保存・書き出し」に入るかは `EDIT_TOOLS` / `SAVE_TOOLS` / `category()`（一覧は docs/MCP.md §4-1）。

`bridge.json` は画面（`app/ai-connect.mjs`）が書き、エンジンは読むだけ。DAW の ARA プラグインのエンジン（`ara`）の曲は
`bridge.json` に載らないので、AI の `load_project()` はプラグインの曲を開かない。
"""
import json
import os

CLIENT_ENV = "GLISS_CLIENT"
BRIDGE_ENV = "GLISS_BRIDGE"
DEFAULT_ALLOW = {"edit": True, "save": False}

# 編集: 曲の中身（ノート・歌詞・トラック・テンポ）を変える。取り消しの履歴に入るものと undo / redo
EDIT_TOOLS = frozenset({
    "set_lyrics", "import_lyrics", "set_note_syllable",
    "shift_pitch", "set_pitch_curve", "move_note", "stretch", "move_boundary", "correct_to_guide",
    "set_transition", "split_note", "merge_notes", "apply_plan", "set_connection",
    "mute_notes", "unmute_notes", "set_fade", "reset_to_original", "undo", "redo",
    "add_track", "remove_track", "set_track", "set_guide_track", "set_tempo",
    "split_track", "join_track", "mute_track_range",
})
# 保存・書き出し: ユーザーのファイルを書く（プロジェクトの中の一時ファイル = render_preview などは対象外）。
# prepare_asr_model は数 GB の聞き取り用モデルをダウンロードしてディスクに書くので、AI が勝手に落とさないようこちら
SAVE_TOOLS = frozenset({"save_project", "export_wav", "prepare_asr_model"})
# 引数しだいで入るもの（category() が決める）: close_project(discard=True) は編集、
# render_region(path=…) / export_view_data(path=…) / render_preview(name=<パス>) は保存・書き出し
CONDITIONAL_TOOLS = frozenset({"close_project", "render_region", "export_view_data", "render_preview"})
# DAW の ARA プラグインのエンジン専用（mcp_ara.py）。AI のプロセスからは許可に関係なく断る
# （プラグインの作業場所を別のプロセスから書き換えない。AI からプラグインの曲は触らない）
ARA_TOOLS = frozenset({"ara_open", "ara_set_modification", "ara_remove_modification", "ara_sync",
                       "ara_render_dirty", "ara_revs", "ara_archive", "ara_restore", "ara_notes"})

LABELS = {"edit": "編集", "save": "保存・書き出し"}


TRUSTED_CLIENTS = ("app", "ara")    # 許可に従わないクライアント（画面・DAW の ARA プラグイン）


def client():
    """エンジンを起動したクライアント（`GLISS_CLIENT`。"app" = 画面 / "ara" = DAW のプラグイン / "" = AI など）。"""
    return os.environ.get(CLIENT_ENV, "").strip().lower()


def is_app():
    """画面（Gliss）か DAW の ARA プラグインが起動したエンジンか（AI の許可に従わない）。"""
    return client() in TRUSTED_CLIENTS


def is_ara():
    """DAW の ARA プラグインが起動したエンジンか。"""
    return client() == "ara"


def path():
    v = os.environ.get(BRIDGE_ENV)
    if v:
        return os.path.abspath(v)
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Gliss", "bridge.json")


def read():
    """bridge.json の中身（無い・読めないときは {}）。"""
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def allow(d=None):
    d = read() if d is None else d
    a = d.get("allow") if isinstance(d.get("allow"), dict) else {}
    return {k: bool(a[k]) if k in a else v for k, v in DEFAULT_ALLOW.items()}


def project(d=None):
    """画面で開いている曲（無ければ None）。"""
    d = read() if d is None else d
    p = d.get("project")
    return p if isinstance(p, dict) and p.get("path") else None


def category(name, args=None):
    """ツールの呼び出しがどちらに入るか: "edit" / "save" / None（許可なしで呼べる）。"""
    a = args or {}
    if name in EDIT_TOOLS:
        return "edit"
    if name in SAVE_TOOLS:
        return "save"
    if name == "close_project":
        return "edit" if a.get("discard") else None
    if name in ("render_region", "export_view_data"):
        return "save" if a.get("path") else None
    if name == "render_preview":
        n = a.get("name")
        return "save" if n and (os.path.isabs(n) or os.path.dirname(n)) else None
    return None


def denied(name, args=None):
    """AI のプロセスで、許していないツールなら返り値（dict）。許していれば None。画面のプロセスは常に None。"""
    if is_app():
        return None
    if name in ARA_TOOLS:
        return {"ok": False, "tool": name, "permission": "ara",
                "error": "%s は DAW の Gliss（ARA プラグイン）のエンジン専用" % name}
    cat = category(name, args)
    if cat is None or allow()[cat]:
        return None
    return {"ok": False, "tool": name, "permission": cat,
            "error": "AI の%sは許可されていない。Gliss の ヘルプ > AI とつなぐ の「AI に許可: %s」で許可できる"
                     % (LABELS[cat], LABELS[cat])}


__all__ = ["ARA_TOOLS", "BRIDGE_ENV", "CLIENT_ENV", "CONDITIONAL_TOOLS", "EDIT_TOOLS", "SAVE_TOOLS",
           "TRUSTED_CLIENTS", "allow",
           "category", "client", "denied", "is_app", "is_ara", "path", "project", "read"]
