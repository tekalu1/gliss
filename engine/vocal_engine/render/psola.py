# -*- coding: utf-8 -*-
"""自前の TD-PSOLA（段階0の聴き比べで選ばれた方式）。

このファイルは自前実装で、GPL のコード（PyPI の psola、Praat / parselmouth）を使っていない
（ライセンスはリポジトリ本体と同じ GPL-3.0-or-later）。

考え方:
  - RMVPE の F0（10 ms）からピッチマーク（基本周期ごとの時刻）を打つ。
  - 合成は「出力時間軸を歩きながら、そのときの入力位置にいちばん近い
    ピッチマークのまわりを Hann 窓で切り出して重ねる」だけ。
      * ピッチシフト → 出力側のマーク間隔を 1/α にする（窓長は入力の周期のまま）
      * 伸縮         → 出力時間 → 入力時間の写像で切り出し位置を決める
    窓長が入力の周期のままなので、**フォルマントは自動的に保たれる**。
  - 無声・無音は OLA（伸縮するときだけ WSOLA で位相を合わせる）。

段階0で PSOLA が E（短い音節を繰り返す叫びの素材）の伸縮で崩れた
（F0 RMSE 180〜283 セント）原因は、Praat のピッチマークが
20〜50 ms ごとに来る破裂音の無声部で崩れることだった。
ここでは **有声区間ごとに独立にマークを打ち直す**（区間の最大振幅点を起点に
前後へ周期送り）ことで、短い無声区間をまたいだ誤りが起きないようにしている。

段階3（「ノイズが多い」「ロボット声」への対処）:
  - マークを**周期ごとのエネルギーの山（声門パルス）**へ寄せる（`_align_to_energy`）。
    基本波の山で打ったマークはパルスから約半周期ずれていて、粒の窓の中心が周期の谷に来ていた。
  - 粒を**サブサンプル精度**で置く（`FRAC_PLACE`）。整数に丸めると出力の周期に ±0.5 サンプルの
    揺れが乗り、高調波の間にノイズ床が立っていた。
  - 合成の歩幅は「次のマークまでの実距離」（`dr`）。窓長（clip あり）とは分ける。
  - 出力の短時間 RMS を入力に揃える（`LEVEL_MATCH`、40 ms のゆっくりした補正）。
"""
import numpy as np

MIN_F0 = 50.0
MAX_F0 = 1100.0
UNVOICED_HALF_MS = 10.0      # 無声側の窓の半長
WSOLA_SEARCH_MS = 5.0        # 無声側の位相合わせの探索幅
MARK_SEARCH = 0.35           # 次のマークの探索幅（周期に対する比）
JUMP_CENTS = 0.0             # >0 なら F0 の跳びで有声区間をさらに割る（実測では悪化したので既定は無効）
ALIGN_MARKS = True           # マークを周期ごとのエネルギーの山（パルス）へ寄せる（段階3）
ALIGN_HP_HZ = 300.0          # パルス検出用のハイパス
ALIGN_SMOOTH = 8             # 位相の円周平均に使う前後のマーク数（<0 なら有声区間全体）
ALIGN_REFINE = 0.10          # 平均位置からの吸着幅（周期に対する比）
ALIGN_MAX_DEV = 0.15         # >0 なら、ずらした後の間隔が元の間隔から この比 以上 変わったら元の間隔に戻す
NORM = "wsum"                # 窓の和での正規化: wsum（旧）/ none / smoothw
NORM_SMOOTH_MS = 10.0        # smoothw の平滑化幅
FRAC_PLACE = True            # 粒をサブサンプル精度で置く（段階3。整数に丸めると高域がノイズになる）
FRAC_TAPS = 16               # 同・窓付き sinc の長さ
LEVEL_MATCH = True           # 出力の短時間 RMS を入力（時間を合わせたもの）に揃える（段階3）
LEVEL_WIN_MS = 40.0          # 同・RMS を測る窓
LEVEL_FLOOR_DB = -60.0       # 同・これより静かなところは補正しない
LEVEL_MAX_DB = 6.0           # 同・補正の上限（±dB）


def _lowpass(x, sr, hi=900.0, lo=55.0):
    """ピッチマーク用の帯域制限（ゼロ位相なのでピーク位置はずれない）。"""
    from scipy.signal import butter, sosfiltfilt
    nyq = sr / 2.0
    hi = min(hi, nyq * 0.95)
    lo = max(lo, 1.0)
    sos = butter(4, [lo / nyq, hi / nyq], btype="band", output="sos")
    return sosfiltfilt(sos, x)


SMOOTH_F0 = True


def _median3_voiced(f0):
    """有声フレームだけ 3 点メディアン。RMVPE の孤立したオクターブ誤りで
    ピッチマークの間隔が 1 フレームだけ倍/半分になるのを防ぐ。"""
    f = np.asarray(f0, dtype="float64").copy()
    v = f > 0
    for i in range(1, len(f) - 1):
        if v[i] and v[i - 1] and v[i + 1]:
            f[i] = float(np.median(f0[i - 1:i + 2]))
    return f


def _runs(mask):
    mask = np.asarray(mask, dtype=bool)
    out = []
    if len(mask) == 0:
        return out
    s, cur = 0, bool(mask[0])
    for i in range(1, len(mask)):
        if bool(mask[i]) != cur:
            out.append((s, i, cur))
            s, cur = i, bool(mask[i])
    out.append((s, len(mask), cur))
    return out


class PsolaAnalysis:
    """波形＋F0 から、ピッチマークと周期を作って持っておく。"""

    def __init__(self, x, sr, f0, voiced, hop_s=0.010):
        self.x = np.ascontiguousarray(np.asarray(x, dtype="float64"))
        self.sr = int(sr)
        self.hop_s = float(hop_s)
        self.f0 = _median3_voiced(np.asarray(f0, dtype="float64")) if SMOOTH_F0             else np.asarray(f0, dtype="float64")
        self.voiced = np.asarray(voiced, dtype=bool) & (self.f0 > 0)
        self.n = len(self.x)
        self._f0_samp = self._f0_per_sample()
        self.marks, self.periods = self._pitch_marks()

    # ------------------------------------------------------------ 補助
    def _f0_per_sample(self):
        """サンプルごとの F0。

        **有声区間ごとに独立に**対数補間する（区間の外は端の値で止める）。
        全フレームをまたいで補間すると、区間の端のサンプルの F0 が
        「次の有声区間の F0」に引っ張られ、ピッチマークの間隔がずれる。
        （実測: A の +3 半音で、区間端の静かなフレームに +60〜+650 セントの
        正のバイアスが出ていた。）
        """
        n_f = len(self.f0)
        t_f = np.arange(n_f) * self.hop_s
        out = np.zeros(self.n)
        v = self.voiced
        if not v.any():
            return out
        for s, e, is_v in _runs(v):
            if not is_v:
                continue
            a = int(round(s * self.hop_s * self.sr))
            b = min(self.n, int(round(e * self.hop_s * self.sr)))
            if b <= a:
                continue
            t_s = np.arange(a, b) / self.sr
            # np.interp は範囲外を端の値で止めるので、区間外へ漏れない
            out[a:b] = np.exp(np.interp(t_s, t_f[s:e], np.log(np.maximum(self.f0[s:e], 1e-6))))
        return out

    def period_at(self, sample):
        """サンプル位置の基本周期（サンプル数）。無声なら 0。"""
        i = int(np.clip(sample, 0, self.n - 1))
        f = self._f0_samp[i]
        if f <= 0:
            return 0
        return float(self.sr / float(np.clip(f, MIN_F0, MAX_F0)))

    def is_voiced_at(self, sample):
        i = int(np.clip(round(sample / self.sr / self.hop_s), 0, len(self.voiced) - 1))
        return bool(self.voiced[i])

    # ------------------------------------------------------------ ピッチマーク
    def _voiced_blocks(self):
        """有声区間を、さらに **F0 の跳び（アタック）** で分ける。

        素材 E（短い音節を繰り返す叫び）は音節の頭で F0 が 655 Hz → 350 Hz と
        1 オクターブ近く跳ぶ。跳びをまたいでマークを打つと、
        そのあたりの周期が滅茶苦茶になる（実測で +1186 セントの外れが出た）。
        """
        blocks = []
        f0 = self.f0
        for s, e, is_v in _runs(self.voiced):
            if not is_v:
                continue
            cut = [s]
            for i in range(s + 1, e):
                if JUMP_CENTS > 0 and f0[i] > 0 and f0[i - 1] > 0:
                    if abs(1200.0 * np.log2(f0[i] / f0[i - 1])) > JUMP_CENTS:
                        cut.append(i)
            cut.append(e)
            for a, b in zip(cut[:-1], cut[1:]):
                if b > a:
                    blocks.append((a, b))
        return blocks

    def _pitch_marks(self):
        """有声ブロックごとに、最大振幅点を起点として周期送りでマークを打つ。"""
        if not self.voiced.any():
            return np.zeros(0, dtype=np.int64), np.zeros(0)
        marks = []   # ブロックごとのマーク列
        self._block_bounds = []
        hop = self.hop_s * self.sr
        for s, e in self._voiced_blocks():
            a = int(round(s * hop))
            b = min(self.n, int(round(e * hop)))
            if b - a < 8:
                continue
            # 区間ごとに基本波のまわりだけ通す（ほぼ正弦になるのでピークが安定する）
            fmed = float(np.median(self.f0[s:e][self.f0[s:e] > 0])) if (self.f0[s:e] > 0).any() else 200.0
            pad = min(a, self.n - b, int(0.05 * self.sr))
            xf_seg = _lowpass(self.x[a - pad:b + pad], self.sr,
                              hi=min(1.7 * fmed, self.sr * 0.45), lo=max(40.0, 0.6 * fmed))
            xf = np.zeros(self.n)
            xf[a - pad:b + pad] = xf_seg
            seg = xf[a:b]
            anchor = a + int(np.argmax(np.abs(seg)))
            # 正のピークを起点にする（負側が最大だった場合は近傍の正ピークへ）
            T = self.period_at(anchor) or (self.sr / 200.0)
            w = max(2, int(round(T * 0.5)))
            lo, hi = max(a, anchor - w), min(b, anchor + w + 1)
            if hi > lo:
                anchor = lo + int(np.argmax(xf[lo:hi]))
            run_marks = [anchor]
            # 前方向
            m = anchor
            while True:
                T = self.period_at(m)
                if T <= 0:
                    break
                cand = m + T
                if cand >= b - 1:
                    break
                w = max(1, int(round(T * MARK_SEARCH)))
                lo = max(a, int(round(cand)) - w)
                hi = min(b, int(round(cand)) + w + 1)
                if hi <= lo:
                    break
                nm = lo + int(np.argmax(xf[lo:hi]))
                if nm <= m:
                    nm = min(b - 1, m + max(1, int(round(T))))
                run_marks.append(nm)
                m = nm
            # 後ろ方向
            m = anchor
            back = []
            while True:
                T = self.period_at(m)
                if T <= 0:
                    break
                cand = m - T
                if cand <= a:
                    break
                w = max(1, int(round(T * MARK_SEARCH)))
                lo = max(a, int(round(cand)) - w)
                hi = min(b, int(round(cand)) + w + 1)
                if hi <= lo:
                    break
                nm = lo + int(np.argmax(xf[lo:hi]))
                if nm >= m:
                    nm = max(a, m - max(1, int(round(T))))
                back.append(nm)
                m = nm
            run_marks = sorted(set(back[::-1] + run_marks))
            marks.append(run_marks)
            self._block_bounds.append((a, b))
        if ALIGN_MARKS:
            marks = [self._align_to_energy(sorted(set(r)), a_b)
                     for r, a_b in zip(marks, self._block_bounds)]
        runs_of_marks = [sorted(set(int(m) for m in r)) for r in marks if r]
        flat = sorted({m for r in runs_of_marks for m in r})
        marks = np.array(flat, dtype=np.int64)
        periods = np.array([self.period_at(m) for m in marks], dtype="float64")
        # マークがどの有声区間のものかを覚えておく（区間をまたいだ間隔を周期にしない）
        run_id = np.zeros(len(marks), dtype=np.int64)
        pos = {m: i for i, m in enumerate(flat)}
        for ri, r in enumerate(runs_of_marks):
            for m in r:
                run_id[pos[m]] = ri
        self.run_id = run_id
        self.pl, self.pr = self._mark_periods(marks, periods, run_id)
        # 合成の歩幅は「次のマークまでの実際の距離」（窓長のような clip はしない）。
        # clip した値で歩くと、無編集でも粒が元の位置からずれて復元が崩れる。
        dr = np.maximum(periods, 8.0).copy()
        if len(marks) > 1:
            same = run_id[1:] == run_id[:-1]
            dr[:-1][same] = np.diff(marks).astype("float64")[same]
        self.dr = dr
        return marks, periods

    def _energy_env(self):
        """周期ごとの「声門パルス」の位置を見るための短時間エネルギー。

        300 Hz 以上を通して 2 乗し、0.5 ms の Hann で均す。基本波（マークを打つのに使う
        帯域）の山と、エネルギーの山（パルス）は位相が違う。
        """
        if getattr(self, "_env", None) is None:
            from scipy.signal import butter, sosfiltfilt
            sos = butter(4, ALIGN_HP_HZ / (self.sr / 2.0), btype="high", output="sos")
            xh = sosfiltfilt(sos, self.x)
            k = max(1, int(round(0.0005 * self.sr)))
            self._env = np.convolve(xh * xh, np.hanning(2 * k + 1), mode="same")
        return self._env

    def _align_to_energy(self, run, bounds):
        """マークを**周期ごとのエネルギーの山**へ移す。

        基本波の山で打ったマークは、実測でパルス（エネルギーの山）から平均で
        約半周期ずれていた（A/C/G/E で circular mean 0.46〜0.48 周期）。
        マークが周期の谷にあると、Hann 窓の中心は谷、パルスは窓の裾に来る。
        すると 1 つのパルスが隣り合う 2 つの粒の裾の和で作られ、間隔を変えた瞬間に
        パルスが 2 つに割れる（ピッチを動かすと高調波の間にノイズが出る・声がにじむ）。

        手順: 各マークで ±½ 周期の中のエネルギー最大点までのずれ（周期に対する位相）を測り、
        前後 ±ALIGN_SMOOTH 個で円周平均して滑らかにし、その位置の ±ALIGN_REFINE 周期で
        最大点へ吸着する。周期送りで得た間隔の安定性は保ったまま、中心だけをパルスに寄せる。
        """
        if len(run) < 2:
            return run
        a, b = bounds
        env = self._energy_env()
        run = np.asarray(run, dtype=np.int64)
        d = np.diff(run).astype("float64")
        P = np.empty(len(run))
        P[0] = d[0]
        P[-1] = d[-1]
        if len(run) > 2:
            P[1:-1] = 0.5 * (d[:-1] + d[1:])
        ph = np.zeros(len(run), dtype=complex)
        for i, m in enumerate(run):
            h = int(P[i] // 2)
            lo, hi = max(0, m - h), min(self.n, m + h + 1)
            if hi - lo < 3:
                continue
            j = lo + int(np.argmax(env[lo:hi]))
            wgt = float(env[j])
            ph[i] = wgt * np.exp(2j * np.pi * (j - m) / P[i])
        out = []
        K = ALIGN_SMOOTH
        zall = ph.sum()
        for i, m in enumerate(run):
            z = zall if K < 0 else ph[max(0, i - K):i + K + 1].sum()
            if abs(z) <= 0:
                out.append(int(m))
                continue
            off = np.angle(z) / (2 * np.pi) * P[i]
            c = int(round(m + off))
            w = max(1, int(round(ALIGN_REFINE * P[i])))
            lo, hi = max(a, c - w), min(b, c + w + 1)
            if hi - lo < 1:
                out.append(int(np.clip(c, a, b - 1)))
                continue
            out.append(lo + int(np.argmax(env[lo:hi])))
        if ALIGN_MAX_DEV > 0:
            # 間隔の番人: 基本波で打った間隔（F0 に忠実）から大きく外れたら元の間隔で送る
            for i in range(1, len(out)):
                di = float(run[i] - run[i - 1])
                if abs((out[i] - out[i - 1]) / di - 1.0) > ALIGN_MAX_DEV:
                    out[i] = int(min(b - 1, out[i - 1] + di))
        # 順序が崩れた（まれ）ものは落とす
        res = [out[0]]
        for i in range(1, len(out)):
            if out[i] - res[-1] >= max(4.0, 0.4 * (self.period_at(res[-1]) or P[-1])):
                res.append(out[i])
        return res

    @staticmethod
    def _mark_periods(marks, periods, run_id):
        """マークごとの左右の実測周期（窓の半長）。

        合成の歩幅と窓長は **F0 から計算した周期ではなく、実測のマーク間隔**を使う。
        F0 由来の周期で歩くと、出力のパルス列の間隔が元のパルス位置とずれて
        「置いた間隔」と「粒の中身の周期」が食い違い、F0 推定が乱れる
        （実測: 恒等変換でも F0 RMSE 67〜141 セント、MCD 1.8 dB だった）。
        """
        k = len(marks)
        if k == 0:
            return np.zeros(0), np.zeros(0)
        T0 = np.maximum(periods, 8.0)
        d = np.diff(marks).astype("float64") if k > 1 else np.zeros(0)
        pl = np.empty(k)
        pr = np.empty(k)
        if k == 1:
            pl[0] = pr[0] = T0[0]
        else:
            pl[0] = d[0]
            pl[1:] = d
            pr[:-1] = d
            pr[-1] = d[-1]
            # 有声区間の境目をまたぐ間隔は「周期」ではないので F0 由来の値に戻す。
            # （またいだままだと、区間の切れ目で間隔が半周期になり、
            #   そのフレームだけ 1 オクターブ上に飛ぶ。実測で E の 1.08 s に出ていた。）
            cross = run_id[1:] != run_id[:-1]
            if cross.any():
                idx = np.where(cross)[0]
                pr[idx] = T0[idx]
                pl[idx + 1] = T0[idx + 1]
        pl = np.clip(pl, 0.5 * T0, 1.8 * T0)
        pr = np.clip(pr, 0.5 * T0, 1.8 * T0)
        return np.maximum(pl, 4.0), np.maximum(pr, 4.0)

    def nearest_mark(self, sample):
        """サンプル位置にいちばん近いマークの index。マークが無ければ None。"""
        if len(self.marks) == 0:
            return None
        i = int(np.searchsorted(self.marks, sample))
        if i == 0:
            return 0
        if i >= len(self.marks):
            return len(self.marks) - 1
        return i if (self.marks[i] - sample) < (sample - self.marks[i - 1]) else i - 1

    def stats(self):
        d = np.diff(self.marks) if len(self.marks) > 1 else np.array([0.0])
        return {"n_marks": int(len(self.marks)),
                "median_period_ms": round(float(np.median(d)) / self.sr * 1000.0, 3) if len(d) else None,
                "voiced_ratio": round(float(np.mean(self.voiced)), 4)}


def _hann(n):
    return np.hanning(n + 2)[1:-1] if n > 0 else np.zeros(0)


def _asym_window(nl, nr):
    """左 nl・右 nr の非対称 Hann。

    隣り合う粒を実測のマーク間隔で並べると、立ち上がり半分と
    立ち下がり半分の和がちょうど 1 になる（COLA）。
    つまり **無編集なら原音が完全に復元される**。
    """
    l = 0.5 * (1.0 - np.cos(np.pi * (np.arange(nl) + 0.5) / nl)) if nl > 0 else np.zeros(0)
    r = 0.5 * (1.0 + np.cos(np.pi * (np.arange(nr) + 0.5) / nr)) if nr > 0 else np.zeros(0)
    return np.concatenate([l, r])


def synthesize(ana, src_start, src_end, ratio=1.0, pitch_factor=1.0,
               pitch_curve=None, wsola=True, step_mode="marks", snap=True):
    """[src_start, src_end) サンプルを、長さ ratio 倍・ピッチ pitch_factor 倍で合成する。

    pitch_curve: (times_sec_abs, factor) の 2 本の配列。与えたら pitch_factor より優先。
    返り値は (out, info)。
    """
    x, sr, n = ana.x, ana.sr, ana.n
    src_start = int(max(0, src_start))
    src_end = int(min(n, src_end))
    src_len = src_end - src_start
    if src_len <= 0:
        return np.zeros(0), {"grains": 0}
    out_len = max(1, int(round(src_len * float(ratio))))
    out = np.zeros(out_len + sr)          # 末尾に余裕（最後の窓のはみ出し）
    wsum = np.zeros(out_len + sr)
    wcov = np.zeros(out_len + sr)          # smoothw 用: 粒の間隔で重み付けした被覆

    def inv(t_out):                        # 出力サンプル → 入力サンプル
        return src_start + t_out / float(ratio)

    def pfac(t_src):
        if pitch_curve is None:
            return float(pitch_factor)
        tt, ff = pitch_curve
        return float(np.interp(t_src / sr, tt, ff))

    uv_half = max(8, int(round(UNVOICED_HALF_MS / 1000.0 * sr)))
    search = max(1, int(round(WSOLA_SEARCH_MS / 1000.0 * sr)))
    t_out = 0.0
    grains = 0
    voiced_grains = 0
    guard = 0
    prev_voiced = False
    prev_block = None
    max_grains = int(out_len / max(2.0, sr / MAX_F0)) + 64

    def place(grain_src_center, nl, nr, center_out, cov=1.0):
        """入力の [c-nl, c+nr) を非対称 Hann で切り出し、出力の center_out に置く。

        center_out は小数でよい。FRAC_PLACE なら端数ぶんを窓付き sinc で遅らせて置く
        （整数に丸めると、出力の周期に ±0.5 サンプルの揺らぎが乗り、高域の位相が乱れる）。
        """
        g0, g1 = grain_src_center - nl, grain_src_center + nr
        a, b = max(0, g0), min(n, g1)
        if b <= a:
            return
        w = _asym_window(nl, nr)
        grain = np.zeros(nl + nr)
        grain[a - g0:(a - g0) + (b - a)] = x[a:b]
        grain *= w
        ci = int(np.floor(center_out)) if FRAC_PLACE else int(round(center_out))
        frac = float(center_out) - ci if FRAC_PLACE else 0.0
        ww = w
        o0 = ci - nl
        if frac > 1e-3:
            h = _frac_kernel(frac)
            grain = np.convolve(grain, h)
            ww = np.convolve(w, h)
            o0 -= FRAC_TAPS // 2 - 1
        L = len(grain)
        oa, ob = max(0, o0), min(len(out), o0 + L)
        if ob > oa:
            out[oa:ob] += grain[oa - o0:ob - o0]
            wsum[oa:ob] += ww[oa - o0:ob - o0]
            wcov[oa:ob] += cov * ww[oa - o0:ob - o0]

    while t_out < out_len and guard < max_grains * 4:
        guard += 1
        t_src = inv(t_out)
        mi = ana.nearest_mark(t_src)
        use_voiced = False
        if mi is not None:
            m = int(ana.marks[mi])
            nl = int(round(ana.pl[mi]))
            nr = int(round(ana.pr[mi]))
            # マークが遠い（＝無声・無音の中）なら OLA に落とす
            if max(nl, nr) > 0 and abs(m - t_src) <= max(nl, nr):
                use_voiced = True

        if use_voiced:
            new_block = (prev_block is not None and int(ana.run_id[mi]) != prev_block)
            if snap and (not prev_voiced or new_block):
                # 有声に入るところで、粒の中心を元の位置へスナップする
                # （原音との位相ずれを毎回リセットして、つなぎ目と恒等変換を綺麗にする）
                # （旧版はここで引数の snap を上書きしていた。0.0 になると以後スナップしない）
                snap_to = (m - src_start) * float(ratio)
                if abs(snap_to - t_out) <= max(nl, nr) and snap_to >= 0:
                    t_out = snap_to
            if step_mode == "f0":
                base = ana.periods[mi] if ana.periods[mi] > 0 else nr
            elif step_mode == "blend":
                base = 0.5 * (nr + (ana.periods[mi] if ana.periods[mi] > 0 else nr))
            else:
                base = float(ana.dr[mi])
            step = base / max(1e-6, pfac(t_src))
            place(m, nl, nr, t_out, cov=max(1.0, step) / max(1.0, 0.5 * (nl + nr)))
            voiced_grains += 1
            prev_voiced = True
            prev_block = int(ana.run_id[mi])
        else:
            L = uv_half
            p = int(round(t_src))
            if wsola and abs(ratio - 1.0) > 1e-6 and t_out >= L:
                p = _wsola_offset(x, out, p, int(round(t_out)), L, search)
            place(p, L, L, int(round(t_out)))
            step = float(L)
            prev_voiced = False
            prev_block = None
        grains += 1
        t_out += max(1.0, step)

    if NORM == "none":
        y = out[:out_len].copy()
    elif NORM == "smoothw":
        k = max(3, int(round(NORM_SMOOTH_MS / 1000.0 * sr)) | 1)
        h = np.hanning(k + 2)[1:-1]
        c = np.convolve(wcov[:out_len], h / h.sum(), mode="same")
        y = out[:out_len] / np.maximum(c, 0.5)
    else:
        den = np.maximum(wsum[:out_len], 0.25)
        y = out[:out_len] / den
    if LEVEL_MATCH and (abs(ratio - 1.0) > 1e-9 or pitch_curve is not None
                        or abs(pitch_factor - 1.0) > 1e-9):
        y = _match_level(y, x[src_start:src_end], sr, float(ratio))
    return y, {"grains": grains, "voiced_grains": voiced_grains,
               "out_samples": int(out_len), "src_samples": int(src_len)}


def _match_level(y, xs, sr, ratio):
    """y の短時間パワーを、入力 xs を ratio 倍に伸ばした時間軸の短時間パワーに揃える。

    ピッチを上げると粒が重なって窓の和が 1 を超え、wsum で割ると 1 周期あたりの振幅が下がる
    （旧版は +3 半音で 3〜4 dB、マークを直した後でも 1.5〜2.8 dB 小さくなっていた。
    parselmouth / pcnsf は原音と同じ音量）。40 ms の窓で測ったゆっくりした補正なので、
    周期の中の波形は変えない。
    """
    k = max(3, int(round(LEVEL_WIN_MS / 1000.0 * sr)) | 1)
    h = np.hanning(k + 2)[1:-1]
    h /= h.sum()
    if len(xs) < 2 or len(y) < 2:
        return y
    # mode="same" は窓より短い入力で窓の長さを返すので、full から入力と同じ長さを切り出す
    c = k // 2
    e_out = np.convolve(y * y, h, mode="full")[c:c + len(y)]
    e_in = np.convolve(xs * xs, h, mode="full")[c:c + len(xs)]
    e_in_o = np.interp(np.arange(len(y)) / ratio, np.arange(len(xs)), e_in)
    floor = 10.0 ** (LEVEL_FLOOR_DB / 10.0)
    g = np.sqrt((e_in_o + floor) / (e_out + floor))
    lim = 10.0 ** (LEVEL_MAX_DB / 20.0)
    g = np.clip(g, 1.0 / lim, lim)
    return y * g


_FRAC_CACHE = {}


def _frac_kernel(frac):
    """端数 frac (0..1) サンプルだけ遅らせる窓付き sinc（FRAC_TAPS 点、和 = 1）。"""
    key = int(round(frac * 64))
    if key not in _FRAC_CACHE:
        f = key / 64.0
        n = np.arange(FRAC_TAPS) - (FRAC_TAPS // 2 - 1) - f
        h = np.sinc(n) * np.blackman(FRAC_TAPS + 2)[1:-1]
        _FRAC_CACHE[key] = h / h.sum()
    return _FRAC_CACHE[key]


def _wsola_offset(x, out, p, t_out, L, search):
    """無声側の位相合わせ。既に書いた出力の直前 L サンプルと相関が最大の位置を選ぶ。"""
    from scipy.signal import correlate
    a0 = max(0, t_out - L)
    tail = out[a0:t_out]
    if len(tail) < L // 2:
        return p
    lo = max(0, p - L - search)
    hi = min(len(x), p - L + search + len(tail))
    seg = x[lo:hi]
    if len(seg) < len(tail) + 1:
        return p
    c = correlate(seg, tail, mode="valid")
    norm = np.sqrt(np.maximum(correlate(seg ** 2, np.ones(len(tail)), mode="valid"), 1e-12))
    k = int(np.argmax(c / norm))
    return int(lo + k + L)


def render_segment(ana, start_sec, end_sec, cents=0.0, ratio=1.0, curve_points=None):
    """秒で指定した区間を編集して返す。curve_points は [(相対秒, セント), ...]。"""
    sr = ana.sr
    s = int(round(start_sec * sr))
    e = int(round(end_sec * sr))
    pc = None
    if curve_points:
        tt = np.array([start_sec + float(t) for t, _ in curve_points], dtype="float64")
        ff = np.array([2.0 ** (float(c) / 1200.0) for _, c in curve_points], dtype="float64")
        pc = (tt, ff)
    return synthesize(ana, s, e, ratio=ratio,
                      pitch_factor=2.0 ** (float(cents) / 1200.0), pitch_curve=pc)
