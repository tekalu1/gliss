# -*- coding: utf-8 -*-
"""Praat（praat-parselmouth）の TD-PSOLA による再合成。**既定のバックエンド**（2026-09-23〜）。

段階0（方式の評価）の Praat の書き出し（聴き比べで伸縮 1 位・ピッチ次点）と**同じ設定**で、
区間ごとに再合成する:

  - 解析: `To Manipulation`（time step 0.01 s、pitch floor / ceiling は RMVPE の F0 の
    1 / 99 パーセンタイルから段階0と同じ式で決める）。有声/無声の判定とパルス（周期の位置）は
    Praat 自身のもの（RMVPE の V/UV は使わない。段階0と同じ）。
  - ピッチ: PitchTier の各点に倍率を掛ける（一律なら段階0の "Multiply frequencies" と同じ値）。
    ピッチ曲線（curve_points）は点ごとの時刻で倍率を補間して掛ける。
  - 伸縮: DurationTier に一定値 ratio（区間ごとの伸縮はパイプラインが区間を切って渡す）。
  - 合成: `Get resynthesis (overlap-add)`。
  - 音量: 再合成した部分（余白込み）の RMS を原音の同じ部分に合わせる（段階0は全方式でクリップの
    RMS を原音に合わせてから聴き比べた。Praat の PSOLA は下げで約 1.4 dB 小さくなる）。

区間ごとの処理（速度と一貫性のため）:
  - 素材全体の `To Manipulation` は `prepare` で **1 回だけ**行い、パルスと PitchTier を持っておく
    （158 秒の素材で約 0.2 秒）。
  - `render` では区間の前後に `PAD_SEC` の余白を付けた部分だけを Sound にし、
    その部分の Manipulation に**全体の解析のパルスと PitchTier**を差し込んで再合成する。
    有声/無声と切り出す周期の位置は区間の切り方によらず素材全体の解析と同じになる（段階0の
    クリップ全体の書き出しとサンプル単位でほぼ一致する）。素材全体を毎回再合成しないので、
    区間の長さに比例した時間で済む。
  - 注意: Praat は新しいパルス列を PitchTier の始まりから周期を積分して作るので、**出力の周期の位相は
    窓の始まりに依存する**。同じ設定の区間を 2 つに割って別々に再合成すると、分割点の前後で位相が
    そろわない（つなぎ目は pipeline の 20 ms クロスフェード。自前 psola も同じ性質）。pipeline の `_join` が、
    有効にしたときは、つなぎ目ごとに入ってくる区間を動かして相互相関をそろえる（`pipeline.ALIGN_MAX_MS`。既定は無効）。

Praat が出せない区間は**自前の TD-PSOLA（psola）で合成**し、info の `fallback_reason` と
`warnings` に理由を書く（Renderer が warnings に集める）:
  - 伸縮の比が `PRAAT_MAX_RATIO`（3）を超える（Praat の overlap-add は 3 倍で頭打ちになる。実測で
    ratio 3.1 / 3.5 / 4 の出力がどれも 3 倍の長さ、8 は後半が無音）。2 段に分けて Praat で伸ばす案は
    採らない: 2 回目は再合成した音を解析し直すことになり（素材全体の解析のパルスを使えない）、
    PSOLA の窓掛けも 2 回重なって音が荒れるため。psola は 1 回の合成で任意の比を出せる。
  - 狙う F0 の最小値が `PRAAT_MIN_TARGET_F0` 未満（Praat は周期 1/50 s より長い周期を作れず、
    ピッチを黙って変えない。実測で 150 Hz → 50.0 Hz は効かず 50.1 Hz は効く）。
  - 素材が短すぎて `To Manipulation` が例外になる（0.05 s で "minimum pitch must not be less than ..."）、
    再合成で例外が出る、出力の長さが期待値から大きくずれる（保険）。

ステレオの書き出しでは、モノラル化した音の解析（パルス・PitchTier）を `ref` として全チャンネルで共有し、
音量合わせのゲインもモノラルの再合成で 1 回だけ決めて全チャンネルに同じ値を掛ける
（チャンネルごとに独立に決めると左右のバランスが区間ごとに動くため）。

ライセンス: Praat / parselmouth は GPL-3.0-or-later。このツールは GPL-3.0-or-later で配布する（2026-09-23 決定）ので製品に含めてよい。
"""
import numpy as np
import os
import struct
import tempfile

TIME_STEP = 0.01          # 段階0と同じ（To Manipulation の time step）
FLOOR_MIN = 40.0          # 段階0: floor = max(40, ...)
CEIL_MAX = 1600.0         # 段階0: ceiling = min(1600, max(ceil, floor * 2.5))
PAD_SEC = 0.10            # 区間の前後に付けて再合成する余白（窓のはみ出しと端の扱いを避ける）
TIER_MARGIN_SEC = 0.05    # PitchTier は余白のさらに外側の点も入れる（端の補間を全体と同じにする）
LEVEL_MATCH = True        # 再合成した部分（余白込み）の RMS を原音の同じ部分に合わせる（段階0の driver と同じ考え）
LEVEL_MAX_DB = 3.0        # 同・補正の上限（±dB。段階0で実際に掛かったのは +0.0〜+1.7 dB）
LEVEL_SILENT = 1e-5       # 同・これより静か（RMS）なら補正しない（約 -100 dBFS）
PRAAT_MAX_RATIO = 3.0     # Praat の overlap-add が出せる伸縮の上限（超えると 3 倍で頭打ち。実測）
PRAAT_MIN_TARGET_F0 = 51.0  # 狙う F0 がこれ未満なら psola へ（Praat は 50.0 Hz 以下を作れない。1 Hz の余裕）
LEN_TOL = 0.02            # Praat の出力長が期待値からこの比以上ずれたら psola へ（保険）
LEN_TOL_SEC = 0.010       # 同・ずれの許容の下限（秒）


def available():
    try:
        import parselmouth  # noqa: F401
        return True
    except Exception:
        return False


def pitch_range(f0, voiced):
    """段階0の `driver.Ctx` と同じ式で pitch floor / ceiling を決める（RMVPE の F0 から）。"""
    f0 = np.asarray(f0, dtype="float64")
    v = np.asarray(voiced, dtype=bool)
    n = min(len(f0), len(v))
    vv = f0[:n][v[:n] & (f0[:n] > 0)]
    f0_floor = float(max(50.0, np.percentile(vv, 1) * 0.75)) if vv.size else 65.0
    f0_ceil = float(min(1100.0, np.percentile(vv, 99) * 1.6)) if vv.size else 1100.0
    floor = max(FLOOR_MIN, f0_floor)
    ceil = min(CEIL_MAX, max(f0_ceil, floor * 2.5))
    return floor, ceil


class PraatContext:
    """素材 1 本（1 チャンネル）ぶんの Praat の解析結果。

    ref（同じ長さのモノラル化した音の PraatContext）を渡すと、解析（パルス・PitchTier・floor/ceiling）を
    それと共有し、音量合わせのゲインも ref の再合成で決める（ステレオ書き出しの全チャンネルで揃える）。
    解析が例外になったら `error` に理由を入れ、render は常に psola に落とす。
    """

    def __init__(self, x, sr, f0, voiced, hop_s=0.010, ref=None):
        self.x = np.ascontiguousarray(np.asarray(x, dtype="float64"))
        self.sr = int(sr)
        self.n = len(self.x)
        self.duration = self.n / float(self.sr)
        self.f0, self.voiced, self.hop_s = f0, voiced, float(hop_s)
        self.level_ref = ref
        self.error = None
        self._psola = None
        self._gain_cache = {}
        self.pulses = None
        self.pt_t = self.pt_v = np.zeros(0)
        self.n_pulses = 0
        self.floor, self.ceil = pitch_range(f0, voiced)
        if ref is not None:
            self.floor, self.ceil, self.error = ref.floor, ref.ceil, ref.error
            self.pulses, self.pt_t, self.pt_v, self.n_pulses = ref.pulses, ref.pt_t, ref.pt_v, ref.n_pulses
            return
        try:
            import parselmouth
            from parselmouth.praat import call
            snd = parselmouth.Sound(self.x, sampling_frequency=float(self.sr))
            self.duration = float(snd.xmax)
            manip = call(snd, "To Manipulation", TIME_STEP, self.floor, self.ceil)
            self.pulses = call(manip, "Extract pulses")
            pt = call(manip, "Extract pitch tier")
            k = int(call(pt, "Get number of points"))
            if k > 1000:
                try:
                    self.pt_t, self.pt_v = _pitch_tier_points(pt, k)
                except (OSError, ValueError):
                    self.pt_t = np.array([call(pt, "Get time from index", i + 1) for i in range(k)], dtype="float64")
                    self.pt_v = np.array([call(pt, "Get value at index", i + 1) for i in range(k)], dtype="float64")
            else:
                self.pt_t = np.array([call(pt, "Get time from index", i + 1) for i in range(k)], dtype="float64")
                self.pt_v = np.array([call(pt, "Get value at index", i + 1) for i in range(k)], dtype="float64")
            self.n_pulses = int(call(self.pulses, "Get number of points"))
        except Exception as e:                       # 素材が短すぎる等
            self.error = _short_error(e)
            from .. import log
            log.get().warning("Praat の解析ができないので自前の psola で合成する: %s", self.error)

    def psola(self):
        """フォールバック用の自前 psola の解析（初めて要るときに 1 回だけ作る）。"""
        if self._psola is None:
            from .psola import PsolaAnalysis
            self._psola = PsolaAnalysis(self.x, self.sr, self.f0, self.voiced, self.hop_s)
        return self._psola

    def stats(self):
        return {"pitch_floor": round(self.floor, 1), "pitch_ceiling": round(self.ceil, 1),
                "time_step": TIME_STEP, "pulses": self.n_pulses, "pitch_tier_points": len(self.pt_t),
                "error": self.error, "shared_analysis": self.level_ref is not None}


def _pitch_tier_points(tier, count):
    """長尺PitchTierの点をPraatバイナリ形式で一括取得する。"""
    fd, path = tempfile.mkstemp(prefix="gliss-pitch-tier-", suffix=".bin")
    os.close(fd)
    try:
        tier.save_as_binary_file(path)
        with open(path, "rb") as f:
            data = f.read()
        header = b"ooBinaryFile\tPitchTier"
        offset = len(header) + 2 * 8 + 4
        if (not data.startswith(header) or len(data) != offset + 16 * count
                or struct.unpack_from(">i", data, len(header) + 16)[0] != count):
            raise ValueError("Praat PitchTierの点数が一致しない")
        values = np.frombuffer(data, dtype=">f8", count=2 * count, offset=offset).astype("float64")
        return values[::2], values[1::2]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class _Fallback(Exception):
    """Praat では出せない（理由をメッセージに持つ）。"""


def _short_error(e):
    msg = " ".join(str(e).split())
    return msg[:160] if msg else type(e).__name__


def render_segment(ctx, start_sec, end_sec, cents=0.0, ratio=1.0, curve_points=None):
    """[start_sec, end_sec) を ratio 倍の長さ・指定のピッチで再合成して (y, info) を返す。

    curve_points は [(start_sec からの相対秒（元の時間軸）, セント), ...]。与えたら cents より優先。
    出力は round((end - start) * sr * ratio) サンプル（Praat の出力をその位置で切り出す）。
    Praat で出せない区間は自前 psola で合成し、info["fallback_reason"] / info["warnings"] に理由を書く。
    """
    ratio = float(ratio)
    try:
        if ctx.error is not None:
            raise _Fallback("Praat の解析ができない（%s）" % ctx.error)
        if ratio > PRAAT_MAX_RATIO + 1e-9:
            raise _Fallback("伸縮の比 %.3g が Praat の上限 %.3g を超える" % (ratio, PRAAT_MAX_RATIO))
        y, info, _ = _render_praat(ctx, start_sec, end_sec, cents, ratio, curve_points)
        return y, info
    except _Fallback as e:
        reason = str(e)
    except Exception as e:                           # parselmouth の例外（区間が短すぎる等）
        reason = "Praat の再合成で例外（%s）" % _short_error(e)
    return _render_psola(ctx, start_sec, end_sec, cents, ratio, curve_points, reason)


def _render_psola(ctx, start_sec, end_sec, cents, ratio, curve_points, reason):
    from .psola import render_segment as psola_segment
    y, info = psola_segment(ctx.psola(), start_sec, end_sec, cents=cents, ratio=ratio,
                            curve_points=curve_points)
    info = dict(info or {})
    msg = "%.3f〜%.3f s は Praat ではなく自前の psola で合成した: %s" % (start_sec, end_sec, reason)
    info.update({"backend": "psola", "requested_backend": "praat", "fallback_reason": reason,
                 "warnings": list(info.get("warnings", [])) + [msg]})
    return y, info


def _render_praat(ctx, start_sec, end_sec, cents, ratio, curve_points, gain_only=False):
    """Praat で再合成する。(core, info, gain_db) を返す。出せないときは _Fallback を投げる。"""
    import parselmouth
    from parselmouth.praat import call
    sr, n = ctx.sr, ctx.n
    a = max(0, int(round(start_sec * sr)))
    b = min(n, int(round(end_sec * sr)))
    want = max(1, int(round((b - a) * ratio)))
    if b <= a:
        return np.zeros(want), {"backend": "praat", "empty": True}, 0.0
    pad = int(round(PAD_SEC * sr))
    pa = max(0, a - pad)
    pb = min(n, b + pad)
    xa, xb = pa / sr, pb / sr

    sub = parselmouth.Sound(ctx.x[pa:pb], sampling_frequency=float(sr), start_time=xa)
    manip = call(sub, "To Manipulation", TIME_STEP, ctx.floor, ctx.ceil)

    # パルス: 素材全体の解析のものを、この部分の範囲に絞って差し込む
    pp = call(ctx.pulses, "Copy", "pulses")
    if xa > 0:
        call(pp, "Remove points between", 0.0, xa)
    if xb < ctx.duration:
        call(pp, "Remove points between", xb, ctx.duration + 1.0)
    call([manip, pp], "Replace pulses")

    # PitchTier: 全体の点を倍率付きで作り直す
    lo = np.searchsorted(ctx.pt_t, xa - TIER_MARGIN_SEC, side="left")
    hi = np.searchsorted(ctx.pt_t, xb + TIER_MARGIN_SEC, side="right")
    tt = ctx.pt_t[lo:hi]
    vv = ctx.pt_v[lo:hi]
    if curve_points:
        ct = np.array([float(start_sec) + float(t) for t, _ in curve_points], dtype="float64")
        cf = np.array([2.0 ** (float(c) / 1200.0) for _, c in curve_points], dtype="float64")
        fac = np.interp(tt, ct, cf)
    else:
        fac = np.full(len(tt), 2.0 ** (float(cents) / 1200.0))
    if len(tt):
        target = vv * fac
        _check_min_f0(target)
        pt = call("Create PitchTier", "pitch", min(xa, tt[0]), max(xb, tt[-1]))
        for t, v in zip(tt, target):
            call(pt, "Add point", float(t), float(v))
        call([manip, pt], "Replace pitch tier")
    else:
        # 全体の解析に点が無い（ほぼ無声）ときは、部分の解析の PitchTier に一律の倍率を掛ける
        g = (float(np.interp(0.5 * (start_sec + end_sec), ct, cf)) if curve_points
             else 2.0 ** (float(cents) / 1200.0))
        if abs(g - 1.0) > 1e-12:
            pt = call(manip, "Extract pitch tier")
            k = int(call(pt, "Get number of points"))
            if k:
                _check_min_f0(np.array([call(pt, "Get value at index", i + 1) for i in range(k)]) * g)
            call(pt, "Multiply frequencies", xa, xb, g)
            call([manip, pt], "Replace pitch tier")

    # DurationTier: 一定の ratio（1.0 のときは入れない = 等倍）
    if abs(ratio - 1.0) > 1e-12:
        dt = call("Create DurationTier", "dur", xa, xb)
        call(dt, "Add point", xa, ratio)
        call([dt, manip], "Replace duration tier")

    # Praat の overlap-add は長さを変えると無声の区間を**乱数で**選んで継ぐので、同じ区間を 2 回
    # 再合成すると音が変わる（DAW 連携 段階 0 で見つけた。`docs/daw-stage0.md` §2）。区間と比から
    # 決まる種で毎回初期化して決定的にする（差分更新のキャッシュ・書き出し・再生が同じ音になり、
    # 多チャンネルでも全チャンネルが同じ選び方になる）
    try:
        _seed_praat(a, b, ratio)
    except Exception:                                # 種を入れられなくても再合成はする（乱数のまま）
        pass
    out = call(manip, "Get resynthesis (overlap-add)")
    y = np.asarray(out.values, dtype="float64")[0]
    expect = (pb - pa) * ratio
    if abs(len(y) - expect) > max(LEN_TOL * expect, LEN_TOL_SEC * sr):
        raise _Fallback("Praat の出力が %d サンプルで、期待の %d から大きくずれた"
                        % (len(y), int(round(expect))))

    gain_db = 0.0
    if LEVEL_MATCH:
        gain_db = None
        if ctx.level_ref is not None and not gain_only:
            gain_db = _shared_gain(ctx.level_ref, start_sec, end_sec, cents, ratio, curve_points)
        if gain_db is None:
            rx = float(np.sqrt(np.mean(ctx.x[pa:pb] ** 2)))
            ry = float(np.sqrt(np.mean(y ** 2))) if len(y) else 0.0
            gain_db = 0.0
            if rx > LEVEL_SILENT and ry > LEVEL_SILENT:
                gain_db = float(np.clip(20.0 * np.log10(rx / ry), -LEVEL_MAX_DB, LEVEL_MAX_DB))
            elif gain_only:
                gain_db = None                      # モノラルが無音（逆相など）なら各チャンネルで決める
        if gain_only:
            return None, None, gain_db
        y = y * 10.0 ** (gain_db / 20.0)
    off = int(round((a - pa) * ratio))
    core = y[off:off + want]
    if len(core) < want:
        core = np.concatenate([core, np.zeros(want - len(core))])
    return core, {"backend": "praat", "out_samples": int(want), "src_samples": int(b - a),
                  "praat_out_samples": int(len(y)), "pitch_points": int(len(tt)),
                  "level_match_db": round(gain_db, 3),
                  "shared_level": ctx.level_ref is not None}, gain_db


def _check_min_f0(target):
    target = np.asarray(target, dtype="float64")
    target = target[target > 0]
    if target.size and float(target.min()) < PRAAT_MIN_TARGET_F0:
        raise _Fallback("狙う F0 の最小値 %.1f Hz が Praat の下限（約 50 Hz）を下回る"
                        "（Praat ではピッチが変わらない）" % float(target.min()))


def _shared_gain(ref, start_sec, end_sec, cents, ratio, curve_points):
    """モノラル化した音（ref）の再合成で音量合わせのゲインを決める（全チャンネル共通。区間ごとにキャッシュ）。"""
    cp = (tuple((round(float(t), 9), round(float(c), 9)) for t, c in curve_points)
          if curve_points else None)
    key = (round(float(start_sec), 9), round(float(end_sec), 9), round(float(cents), 9),
           round(float(ratio), 12), cp)
    if key not in ref._gain_cache:
        try:
            ref._gain_cache[key] = _render_praat(ref, start_sec, end_sec, cents, ratio, curve_points,
                                                 gain_only=True)[2]
        except Exception:
            ref._gain_cache[key] = None             # 出せなければ各チャンネルが自分で決める
        while len(ref._gain_cache) > 256:
            ref._gain_cache.pop(next(iter(ref._gain_cache)))
    return ref._gain_cache[key]


def _seed_praat(a, b, ratio):
    """Praat の乱数を区間（サンプル位置）と伸縮比から決まる種で初期化する。"""
    from parselmouth.praat import run
    seed = (int(a) * 1000003 + int(b) * 7919 + int(round(float(ratio) * 1e6))) % 2147483647
    run("random_initializeWithSeedUnsafelyButPredictably (%d)" % seed)
