"""GlissARATest（plugin/tests/aratest）の出力を、エンジンの render_region と突き合わせる（C3 の完了の条件）。

使い方（エンジンの python・cwd は engine）:
    <main>\\.venv\\Scripts\\python.exe <wt>\\plugin\\tests\\verify_ara_engine.py <out> <作業場所 A> <作業場所 B>

見ること:
  1. アーカイブ（archive.json）の形: format "gliss-ara"・version 1・work_key・修飾の編集（changeset が 1 つ以上）
  2. A の描画 = 作業場所 A のエンジンの render_region（同じ範囲＝ソース全体、channels "all"）。ARA の描画はソースと同じ周波数
  3. A の描画が原音と違う（編集が鳴っている）
  4. B の描画（別の作業場所でアーカイブだけから戻したもの）= A の描画、かつ = 作業場所 B の render_region
  5. C（別の周波数での描画）が鳴っていて、長さが周波数の比に合い、A を同じ周波数に直したものと大きくは違わない
  6. DAW に返すノート（notes-n/a/b.json。ARA の content reader の kARAContentTypeNotes）:
     N（編集なし・ホストが解析を頼んだ）= 作業場所 A のエンジンの ara_notes の source_notes、品質は detected。
     リージョン 2（ソースの途中をソングの別の位置に）は、修飾のノートをリージョンの範囲で切ってソングの秒に写したもの。
     A（編集の後）= エンジンの ara_notes の notes、品質は adjusted、音程が N より 1 半音上（試験の編集は +100 セント）。
     B（アーカイブから戻した後）= A、かつ = 作業場所 B のエンジンの ara_notes

許容差: 2〜4 は float32 で 0（サンプル単位で同じ）。プラグインは ara_render_dirty の窓（render_region と同じ再合成）を
そのまま置き、窓の外はホストから読んだ原音（エンジンに渡した WAV と同じ float）を返すので、丸めの入る所が無い。
5 は周波数の変換（JUCE の WindowedSinc）と比べる側の変換（soxr でなく numpy の線形補間）が違うので、相関だけを見る。
"""
import json
import os
import sys

import numpy as np

# cwd（worktree の engine）の vocal_engine を読む（.venv には main の engine が editable で入っている）
sys.path.insert(0, os.getcwd())


def load(path):
    return np.fromfile(path, dtype="<f4")


def engine_reference(work_dir, work_key, ara_id, seconds):
    """作業場所のエンジンのセッションを開き、修飾のトラックの render_region（ソース全体）を返す。"""
    os.environ["VOCAL_ENGINE_WORK_DIR"] = work_dir
    os.environ.setdefault("VOCAL_ENGINE_PREP", "0")
    os.environ["GLISS_CLIENT"] = "ara"              # ara_* はプラグインのエンジンだけが呼べる
    import soundfile as sf
    from vocal_engine import mcp_server as m        # 先に読む（mcp_ara などは mcp_server の末尾から読まれる）
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine import mcp_tracks as mt
    from vocal_engine.analysis import f0 as F
    F.set_preferred_estimator("praat")
    md._clear()
    a._reset_render()
    r = a.ara_open(work_key)
    assert r.get("ok") is not False, r
    s = m._state["session"]
    t = s.find_ara(ara_id)
    assert t is not None, "the work place has no track for " + ara_id
    r = mt.select_track(t["id"])
    assert r.get("ok") is not False, r
    r = m.render_region(start_sec=0.0, end_sec=seconds, channels="all")
    assert r.get("ok") is not False, r
    y, sr = sf.read(r["path"], dtype="float32", always_2d=True)
    n = a.ara_notes([ara_id])
    assert n.get("ok") is not False, n
    md._clear()
    return y[:, 0], sr, n["notes"][ara_id]


def engine_notes(rows):
    """ara_notes の行 → プラグインが返すはずの ARAContentNote（[Hz, ノート番号, 音量, 頭, attack, 長さ, 信号の長さ]）。"""
    out = []
    for n in rows:
        d = n["end_sec"] - n["start_sec"]
        out.append([float(np.float32(n["hz"])), int(round(n["midi"])), float(np.float32(min(1.0, max(0.0, n["volume"])))),
                    n["start_sec"], 0.0, d, d])
    return out


def in_region(notes, region):
    """修飾（ソースの秒）のノートをリージョンの範囲で切り、ソングの秒に写す（Gliss は時間を伸ばさない）。"""
    ms = region["mod_start"]
    me = ms + min(region["mod_duration"], region["song_duration"])
    out = []
    for f, pitch, vol, st, _att, dur, _sig in notes:
        a, b = max(st, ms), min(st + dur, me)
        if b - a < 1e-9:
            continue
        out.append([f, pitch, vol, a - ms + region["song_start"], 0.0, b - a, b - a])
    return out


def same_notes(got, want):
    """(同じか, 説明)。秒は 1e-9、Hz は float の丸め、ノート番号は一致。"""
    if len(got) != len(want):
        return False, f"{len(got)} vs {len(want)} notes"
    worst = 0.0
    for g, w in zip(got, want):
        if g[1] != w[1] or abs(g[0] - w[0]) > 1e-3 or abs(g[2] - w[2]) > 1e-6:
            return False, f"note {g} vs {w}"
        worst = max(worst, *(abs(g[k] - w[k]) for k in (3, 4, 5, 6)))
    return worst < 1e-9, f"{len(got)} notes, max|dt|={worst:.2g}"


def main():
    out, work_a, work_b = sys.argv[1], sys.argv[2], sys.argv[3]
    summary = json.load(open(os.path.join(out, "summary.json"), encoding="utf-8"))
    # ARA のストリームには JUCE の writeString が文字列の終わりの NUL まで書く
    archive = json.loads(open(os.path.join(out, "archive.json"), encoding="utf-8").read().rstrip(chr(0)))
    src = load(os.path.join(out, "source.f32"))
    ra, rb, rc = (load(os.path.join(out, f"render-{k}.f32")) for k in "abc")
    sr = summary["source_rate"]
    mod = summary["modification_id"]
    failures = []

    def check(ok, label, detail=""):
        print(("PASS " if ok else "FAIL ") + label + ((" " + detail) if detail else ""))
        if not ok:
            failures.append(label)

    # 1. アーカイブの形
    entry = archive.get("modifications", {}).get(mod, {})
    changesets = len(((entry or {}).get("archive") or {}).get("changesets") or [])
    check(archive.get("format") == "gliss-ara" and archive.get("version") == 1 and archive.get("document", {}).get("work_key")
          and changesets >= 1, "archive", f"work_key={archive.get('document', {}).get('work_key')} changesets={changesets}")

    seconds = len(src) / sr
    work_key = archive["document"]["work_key"]

    # 2. A = render_region（作業場所 A）
    ref_a, ref_sr, eng_a = engine_reference(work_a, work_key, mod, seconds)
    same_len = len(ref_a) == len(ra) and ref_sr == sr
    diff_a = float(np.abs(ref_a.astype(np.float64) - ra.astype(np.float64)).max()) if same_len else float("inf")
    check(same_len and np.array_equal(ref_a, ra), "render A == engine render_region (work place A)",
          f"frames={len(ra)}/{len(ref_a)} max|diff|={diff_a:.3g}")

    # 3. 編集が鳴っている
    edited = float(np.abs(ra.astype(np.float64) - src.astype(np.float64)).max())
    check(edited > 0.01, "the edit is audible in render A", f"max|A-source|={edited:.3g}")

    # 4. アーカイブの往復
    diff_ab = float(np.abs(ra.astype(np.float64) - rb.astype(np.float64)).max()) if len(ra) == len(rb) else float("inf")
    check(len(ra) == len(rb) and np.array_equal(ra, rb), "render B (restored from the archive) == render A", f"max|diff|={diff_ab:.3g}")
    ref_b, _, eng_b = engine_reference(work_b, work_key, mod, seconds)
    check(len(ref_b) == len(rb) and np.array_equal(ref_b, rb), "render B == engine render_region (work place B)")

    # 5. 別の周波数
    rate_c = summary["rate_c"]
    expect = int(round(len(ra) * rate_c / sr))
    t_c = np.arange(len(rc)) / rate_c
    a_on_c = np.interp(t_c, np.arange(len(ra)) / sr, ra.astype(np.float64))
    core = slice(int(0.05 * len(rc)), int(0.95 * len(rc)))
    corr = float(np.corrcoef(a_on_c[core], rc[core].astype(np.float64))[0, 1])
    rms = (float(np.sqrt(np.mean(rc.astype(np.float64) ** 2))), float(np.sqrt(np.mean(ra.astype(np.float64) ** 2))))
    check(abs(len(rc) - expect) <= 2 and corr > 0.99 and abs(rms[0] / rms[1] - 1) < 0.05, f"render C at {rate_c:g} Hz",
          f"frames={len(rc)} (expected {expect}) corr={corr:.5f} rms={rms[0]:.4f}/{rms[1]:.4f}")

    # 6. DAW に返すノート
    detected, adjusted = 1, 2                     # kARAContentGradeDetected / kARAContentGradeAdjusted
    notes_doc = {}
    for k in "nab":
        path = os.path.join(out, f"notes-{k}.json")
        notes_doc[k] = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None
    check(all(notes_doc.values()), "notes files", ", ".join(k for k, v in notes_doc.items() if v))
    if all(notes_doc.values()):
        n, a_, b_ = notes_doc["n"], notes_doc["a"], notes_doc["b"]
        src_id = summary["source_id"]
        want_n = engine_notes(eng_a["source_notes"])
        check(eng_a["state"] == "ready" and len(want_n) >= 4, "engine notes (work place A)",
              f"{len(want_n)} analyzed, {len(eng_a['notes'])} edited, pitches {[w[1] for w in want_n]}")

        grades = (n["sources"][src_id]["grade"], n["modifications"][mod]["grade"], [r["content"]["grade"] for r in n["regions"]])
        check(grades == (detected, detected, [detected, detected]), "N: grades are detected (analysis only)", str(grades))
        ok, why = same_notes(n["modifications"][mod]["notes"], want_n)
        check(ok, "N: modification notes == engine source_notes", why)
        ok, why = same_notes(n["sources"][src_id]["notes"], want_n)
        check(ok, "N: source notes == engine source_notes", why)
        for i, region in enumerate(n["regions"]):
            ok, why = same_notes(region["content"]["notes"], in_region(want_n, region))
            check(ok, f"N: region {i + 1} (song {region['song_start']:g} s, source {region['mod_start']:g} s) == trimmed and moved", why)
        check(len(n["regions"]) == 2 and 0 < len(n["regions"][1]["content"]["notes"]) < len(want_n),
              "N: the trimmed region has fewer notes", f"{len(n['regions'][1]['content']['notes'])} of {len(want_n)}")

        want_a = engine_notes(eng_a["notes"])
        grades = (a_["sources"][src_id]["grade"], a_["modifications"][mod]["grade"], [r["content"]["grade"] for r in a_["regions"]])
        check(grades == (detected, adjusted, [adjusted]), "A: grades are adjusted after the edit (the source stays detected)", str(grades))
        ok, why = same_notes(a_["modifications"][mod]["notes"], want_a)
        check(ok and eng_a["edited"], "A: modification notes == engine notes (edited)", why)
        ok, why = same_notes(a_["regions"][0]["content"]["notes"], in_region(want_a, a_["regions"][0]))
        check(ok, "A: region notes == engine notes", why)
        ok, why = same_notes(a_["sources"][src_id]["notes"], want_n)
        check(ok, "A: source notes are still the analysis", why)
        shift = [g[1] - w[1] for g, w in zip(a_["modifications"][mod]["notes"], want_n)]
        ratio = [g[0] / w[0] for g, w in zip(a_["modifications"][mod]["notes"], want_n)]
        check(len(shift) == len(want_n) and all(s == 1 for s in shift) and all(abs(r - 2 ** (1 / 12)) < 0.003 for r in ratio),
              "A: the edit (+100 cents) raised every note by a semitone", f"pitch shift {shift}")

        ok, why = same_notes(b_["regions"][0]["content"]["notes"], a_["regions"][0]["content"]["notes"])
        check(ok and b_["regions"][0]["content"]["grade"] == adjusted, "B: region notes (restored from the archive) == A", why)
        ok, why = same_notes(b_["modifications"][mod]["notes"], engine_notes(eng_b["notes"]))
        check(ok, "B: modification notes == engine notes (work place B)", why)

    print("RESULT " + ("OK" if not failures else "FAILED: " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
