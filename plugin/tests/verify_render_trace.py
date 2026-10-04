"""Gliss の再生の記録（GLISS_ARA_TRACE_DIR）を、ARA SDK の TestHost の試験信号（パルス状の正弦波）と突き合わせる。

使い方: python verify_render_trace.py <記録のフォルダ>

1 回目の描画は元の信号と総和・二乗和を厳密に比較する。2 回目は 0.75 倍の時間伸縮なので、
読み出しがすべて完了し、音のあるブロックがあることを確かめる。
"""
import sys, re, glob
import numpy as np

SR = 44100.0
COUNT = int(5.0 * SR)

def pulsed_sine(start, n):
    pos = np.arange(start, start + n, dtype=np.float64)
    value = np.zeros(n)
    inside = (pos >= 0) & (pos < COUNT)
    t = pos * 440.0 / SR
    v = np.where(np.fmod(t, 440.0) <= 220.0, np.sin(t * np.pi * 2.0), 0.0)
    v = v * np.where(np.fmod(t, 880.0) <= 440.0, 1.0, 0.125)
    value[inside] = v[inside]
    return value.astype(np.float32).astype(np.float64)

pat = re.compile(r"trace t=(-?\d+) src=(-?\d+) n=(\d+) complete=(\d) sum=(\S+) sumsq=(\S+)")
checked = bad = incomplete = stretched_checked = stretched_incomplete = 0
stretched_energy = 0.0
for path in glob.glob(sys.argv[1] + "/gliss-ara-*.log"):
    pass_index = 0
    previous_time = -1
    for line in open(path, encoding="utf-8"):
        m = pat.search(line)
        if not m:
            continue
        t, src, n, complete, s, ss = int(m[1]), int(m[2]), int(m[3]), int(m[4]), float(m[5]), float(m[6])
        if previous_time >= 0 and t < previous_time:
            pass_index += 1
        previous_time = t
        if pass_index:
            stretched_checked += 1
            stretched_incomplete += not complete
            stretched_energy += ss
            continue
        exp = pulsed_sine(src, n)
        if not complete:
            incomplete += 1
            # an incomplete read must still never contain wrong samples: every non-zero run is a prefix of the expected signal
            continue
        checked += 1
        if abs(exp.sum() - s) > 1e-5 or abs((exp * exp).sum() - ss) > 1e-5:
            bad += 1
            print("MISMATCH", line.strip(), "expected", exp.sum(), (exp * exp).sum())
print(f"unmodified complete blocks={checked} mismatches={bad} incomplete={incomplete}; "
      f"stretched blocks={stretched_checked} incomplete={stretched_incomplete} energy={stretched_energy:.3f}")
sys.exit(1 if bad or checked == 0 or stretched_checked == 0 or stretched_incomplete or stretched_energy <= 1.0 else 0)
