"""GlissARATest -perf（編集してから画面・音に出るまでの時間）の出力を確かめる。

使い方:
    python <wt>\\plugin\\tests\\verify_ara_perf.py <out> [<out2> <out3>]   # 上限（perf-limits.json）と比べる
    python <wt>\\plugin\\tests\\verify_ara_perf.py --write-limits <out1> <out2> <out3>
                                                                         # 何回かの測りから上限を作り直す

<out>/perf.jsonl の 1 行目は曲と下ごしらえ、続く行が 1 回の測り（場面 same・walk・burst・during）。時刻は最初の編集を出した時からの
ミリ秒で、場面ごとに次の中央値を見る:
  t_edit   最初の編集の答えが画面に返るまで
  t_view   最後の編集の後の描画データが返るまで（画面が描き直せる時）
  t_sound  最後の編集のノートの音が、PlaybackRenderer（DAW の再生と同じく同期を待たない描画）で変わるまで
  view_bytes  最後の描画データのファイルの大きさ（差分で返っているか。時間と違い揺れない）
どの測りでも、編集と描画データの答えが全部返り、音が変わっていること（t_sound >= 0）も見る。
上限は、直した後の版で 3 回測った中央値の 1.5 倍（--write-limits。plugin/tests/aratest/perf-limits.json）。
時間は PC の負荷で揺れるので、出力を 3 つ渡すと、場面ごとに回ごとの中央値の中央値を上限と比べる（test-plugin.ps1 は
1 回目が上限を超えたときだけ 2 回足す）。答えと音の変化は、どの回でも揃っていること。
view_bytes の上限は、描画データが差分で返らなくなった（全体を毎回書く）ことを、時間の揺れに埋もれずに見つけるため。
"""
import json
import math
import os
import statistics
import sys

LIMITS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aratest", "perf-limits.json")
METRICS = ("t_edit", "t_view", "t_sound")
BYTES = "view_bytes"


def load(out):
    lines = [json.loads(s) for s in open(os.path.join(out, "perf.jsonl"), encoding="utf-8") if s.strip()]
    return lines[0], lines[1:]


def values(run):
    return {"t_edit": run["t_edit"][0] if run["t_edit"] else -1.0,
            "t_view": run["t_view"][-1] if run["t_view"] else -1.0,
            "t_sound": run["t_sound"],
            BYTES: run["view_bytes"][-1] if run["view_bytes"] else -1}


def medians(runs):
    by = {}
    for run in runs:
        by.setdefault(run["scenario"], []).append(values(run))
    return {sc: {m: statistics.median(v[m] for v in vs) for m in METRICS + (BYTES,)} for sc, vs in by.items()}


def median_of_medians(per_out):
    """[(head, 場面ごとの中央値)] → 場面ごとに、回ごとの中央値の中央値"""
    return {sc: {m: statistics.median(med[sc][m] for _, med in per_out if sc in med) for m in METRICS + (BYTES,)}
            for sc in per_out[0][1]}


def write_limits(outs):
    per_out = []
    for out in outs:
        head, runs = load(out)
        per_out.append((head, medians(runs)))
    seconds = per_out[0][0]["seconds"]
    med = median_of_medians(per_out)
    measured, limits, measured_bytes, limit_bytes = {}, {}, {}, {}
    for sc in med:
        measured[sc] = {m: round(med[sc][m], 1) for m in METRICS}
        limits[sc] = {m: int(math.ceil(measured[sc][m] * 1.5 / 10.0) * 10) for m in METRICS}
        measured_bytes[sc] = int(med[sc][BYTES])
        limit_bytes[sc] = int(math.ceil(measured_bytes[sc] * 1.5 / 100.0) * 100)
    doc = {"seconds": seconds, "runs": len(outs), "measured_median_ms": measured, "limit_ms": limits,
           "measured_median_view_bytes": measured_bytes, "limit_view_bytes": limit_bytes}
    open(LIMITS, "w", encoding="utf-8", newline="").write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return 0


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--write-limits":
        return write_limits(sys.argv[2:])

    outs = sys.argv[1:]
    failures = []

    def check(ok, label, detail=""):
        print(("PASS " if ok else "FAIL ") + label + ((" " + detail) if detail else ""))
        if not ok:
            failures.append(label)

    limits = json.load(open(LIMITS, encoding="utf-8"))
    per_out = []
    for k, out in enumerate(outs):
        head, runs = load(out)
        tag = "" if len(outs) == 1 else "[%d] " % (k + 1)
        check("error" not in head, tag + "the document was prepared", json.dumps(head.get("setup") or head))
        check(head.get("seconds") == limits["seconds"], tag + "same song length as the limits",
              "seconds=%s limits=%s" % (head.get("seconds"), limits["seconds"]))

        for run in runs:
            name = "%s%s %d" % (tag, run["scenario"], run["i"])
            complete = len(run["t_edit"]) == run["ops"] and len(run["t_view"]) == run["ops"]
            check(complete and run["t_sound"] >= 0, name + ": edits answered and the sound changed",
                  "edits=%d/%d views=%d t_sound=%.0f" % (len(run["t_edit"]), run["ops"], len(run["t_view"]), run["t_sound"]))
        per_out.append((head, medians(runs)))

    for sc, med in median_of_medians(per_out).items():
        limit = limits["limit_ms"].get(sc)
        if limit is None:
            check(False, sc + ": has limits")
            continue
        for m in METRICS:
            check(med[m] <= limit[m], "%s %s median" % (sc, m), "%.0f ms (limit %d)" % (med[m], limit[m]))
        limit = limits["limit_view_bytes"][sc]
        check(med[BYTES] <= limit, "%s %s median" % (sc, BYTES), "%.0f bytes (limit %d)" % (med[BYTES], limit))

    print("RESULT " + ("OK" if not failures else "FAILED: " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
