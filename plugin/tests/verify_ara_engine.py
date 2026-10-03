"""GlissARATest（plugin/tests/aratest）の出力を、エンジンの render_region と突き合わせる（C3 の完了の条件）。

使い方（エンジンの python・cwd は engine）:
    <main>\\.venv\\Scripts\\python.exe <wt>\\plugin\\tests\\verify_ara_engine.py <out> <作業場所 A> <作業場所 B>

見ること:
  1. アーカイブ（archive.json）の形: format "gliss-ara"・version 1・work_key・修飾の編集（changeset が 1 つ以上）
  2. A の描画 = 作業場所 A のエンジンの render_region（同じ範囲＝ソース全体、channels "all"）。ARA の描画はソースと同じ周波数
  3. A の描画が原音と違う（編集が鳴っている）
  4. B の描画（別の作業場所でアーカイブだけから戻したもの）= A の描画、かつ = 作業場所 B の render_region
  5. C（別の周波数での描画）が鳴っていて、長さが周波数の比に合い、A を同じ周波数に直したものと大きくは違わない

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
    md._clear()
    return y[:, 0], sr


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
    ref_a, ref_sr = engine_reference(work_a, work_key, mod, seconds)
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
    ref_b, _ = engine_reference(work_b, work_key, mod, seconds)
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

    print("RESULT " + ("OK" if not failures else "FAILED: " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
