"""GlissARATest -relay（外部の AI からの編集の通し）の出力を確かめる。

使い方（エンジンの python・cwd は engine）:
    <main>\\.venv\\Scripts\\python.exe <wt>\\plugin\\tests\\verify_ara_relay.py <out> <作業場所> <プラグインのログのフォルダ>

見ること:
  1. 外部のクライアント（relay_client.py）が、文書の一覧で GlissARATest の修飾を見つけ、選び、解析し、+100 セントを当てた
  2. 編集の前の描画（render-r0）= 原音（外部が触る前は何も変えない）
  3. 編集の後の描画（render-r1）が原音と違い、作業場所のエンジンの render_region とサンプル単位で同じ
  4. 保存（archive-r.json。DAW のソングに入るもの）に外部の編集（作者 ai の changeset）が入っている
  5. DAW に返すノート（notes-r.json）が adjusted で、全部 1 半音上
  6. プラグインのログに、外部の変更を拾って画面に知らせた行（sync: external edit）がある
"""
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from verify_ara_engine import engine_notes, engine_reference, load, same_notes   # noqa: E402


def main():
    out, work, trace = sys.argv[1], sys.argv[2], sys.argv[3]
    failures = []

    def check(ok, label, detail=""):
        print(("PASS " if ok else "FAIL ") + label + ((" " + detail) if detail else ""))
        if not ok:
            failures.append(label)

    summary = json.load(open(os.path.join(out, "summary-r.json"), encoding="utf-8"))
    client = json.load(open(os.path.join(out, "relay-client.json"), encoding="utf-8"))
    mod, sr = summary["modification_id"], summary["source_rate"]

    # 1. 外部のクライアント
    docs = client.get("documents") or []
    tracks = [t for d in docs for t in d.get("tracks", [])]
    check(client.get("ok") and any(t.get("ara_id") == mod for t in tracks), "external client: list -> attach -> analyze -> shift",
          f"calls={[c['tool'] for c in client.get('calls', [])]} error={client.get('error')}")
    check((client.get("shift") or {}).get("ara", {}).get("ara_id") == mod, "the edit went to the DAW document",
          json.dumps((client.get("shift") or {}).get("ara"), ensure_ascii=False))

    # 2. 編集の前 = 原音
    src = load(os.path.join(out, "source.f32"))
    r0, r1 = load(os.path.join(out, "render-r0.f32")), load(os.path.join(out, "render-r1.f32"))
    check(len(r0) == len(src) and np.array_equal(r0, src), "render before the external edit == source",
          f"frames={len(r0)}/{len(src)}")

    # 3. 編集の後
    edited = float(np.abs(r1.astype(np.float64) - src.astype(np.float64)).max()) if len(r1) == len(src) else 0.0
    check(edited > 0.01, "the external edit is audible", f"max|R1-source|={edited:.3g}")
    archive = json.loads(open(os.path.join(out, "archive-r.json"), encoding="utf-8").read().rstrip(chr(0)))
    work_key = archive["document"]["work_key"]
    ref, ref_sr, eng = engine_reference(work, work_key, mod, len(src) / sr)
    check(len(ref) == len(r1) and ref_sr == sr and np.array_equal(ref, r1), "render after == engine render_region",
          f"frames={len(r1)}/{len(ref)}")

    # 4. 保存
    css = (((archive.get("modifications") or {}).get(mod) or {}).get("archive") or {}).get("changesets") or []
    check(len(css) >= 1 and all(c.get("author") == "ai" for c in css), "the archive (saved in the song) has the external edit",
          f"changesets={[(c.get('id'), c.get('author')) for c in css]}")

    # 5. ノート
    notes = json.load(open(os.path.join(out, "notes-r.json"), encoding="utf-8"))
    m = notes["modifications"][mod]
    ok, why = same_notes(m["notes"], engine_notes(eng["notes"]))
    shift = [g[1] - w[1] for g, w in zip(m["notes"], engine_notes(eng["source_notes"]))]
    check(m["grade"] == 2 and ok and shift and all(s == 1 for s in shift), "notes returned to the DAW follow the external edit",
          f"{why} shift={shift}")

    # 6. プラグインのログ
    lines = []
    for p in glob.glob(os.path.join(trace, "gliss-ara-*.log")):
        with open(p, encoding="utf-8", errors="replace") as f:
            lines += [x.rstrip() for x in f if "external edit" in x]
    check(bool(lines), "the plug-in noticed the external edit (project-changed to the editor)", lines[0] if lines else "")

    print("RESULT " + ("OK" if not failures else "FAILED: " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
