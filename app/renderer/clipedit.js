// トラックビューのクリップの分割・部分のミュートの計算（エンジンの `project/session.py` の norm_cuts / norm_mutes /
// subtract_range と同じ規則。画面は操作の直後にこれで見かけを変え、エンジンの返り値で置き換える）。
// 秒はすべて「トラックの頭（クリップなら頭）が 0」の秒。DOM にも state.js にも触らない（単体で試せる）。

export const CUT_EPS = 1e-3;         // 切れ目・消した区間の端がこれより近ければ同じ点
export const CUT_MIN_EDGE = 0.02;    // トラックの両端からこれより内側にだけ切れ目を入れられる
export const MUTE_MIN = 1e-3;        // 消した区間の最小の長さ
export const CLIP_FADE = 0.005;      // 消した区間の前後のフェード（再生・書き出しとも 5 ms）

const r6 = (v) => Math.round(v * 1e6) / 1e6;

/** 切れ目（昇順・範囲の中だけ・近いものは 1 つに）。 */
export function normCuts(cuts, duration = null) {
  const out = [];
  for (const v of [...(cuts || [])].filter(Number.isFinite).sort((a, b) => a - b)) {
    if (v <= 0 || (duration != null && v >= duration)) continue;
    if (out.length && v - out[out.length - 1] < CUT_EPS) continue;
    out.push(r6(v));
  }
  return out;
}

/** 消した区間 [[始め, 終わり]…]（範囲に収め・昇順・重なる／接する区間は 1 つに）。 */
export function normMutes(mutes, duration = null) {
  const rows = [];
  for (const m of mutes || []) {
    let a = Number(m?.[0]); let b = Number(m?.[1]);
    if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
    a = Math.max(0, a);
    if (duration != null) b = Math.min(duration, b);
    if (b - a >= MUTE_MIN) rows.push([a, b]);
  }
  rows.sort((p, q) => p[0] - q[0] || p[1] - q[1]);
  const out = [];
  for (const [a, b] of rows) {
    const last = out[out.length - 1];
    if (last && a <= last[1] + CUT_EPS) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out.map(([a, b]) => [r6(a), r6(b)]);
}

/** 区間のリストから [a, b] を引く（戻す）。 */
export function subtractRange(mutes, a, b) {
  const out = [];
  for (const [x, y] of mutes) {
    if (y <= a + CUT_EPS / 2 || x >= b - CUT_EPS / 2) { out.push([x, y]); continue; }
    if (x < a - CUT_EPS / 2) out.push([x, a]);
    if (y > b + CUT_EPS / 2) out.push([b, y]);
  }
  return out;
}

/** 切れ目で分けた部分 [[始め, 終わり]…]。 */
export function pieces(cuts, duration) {
  const xs = [0, ...cuts, duration];
  const out = [];
  for (let i = 0; i + 1 < xs.length; i++) out.push([xs[i], xs[i + 1]]);
  return out;
}

/** [a, b] が消した区間で全部覆われているか。 */
export function covered(mutes, a, b) {
  return mutes.some(([x, y]) => x <= a + CUT_EPS && y >= b - CUT_EPS);
}

/** 部分 k を消す（to = true）／戻す（false）。新しい mutes を返す。 */
export function paintPiece(t, k, to) {
  const [a, b] = pieces(t.cuts || [], t.duration_sec || 0)[k];
  const dur = t.duration_sec || 0;
  const rest = subtractRange(t.mutes || [], a, b);
  return normMutes(to ? [...rest, [a, b]] : rest, dur);
}

/** 切れ目をつなぐ（エンジンの join_track と同じ: 両側が消えていれば消したまま、片側だけなら戻す）。 */
export function joinAt(t, cut) {
  const ps = pieces(t.cuts, t.duration_sec || 0);
  const i = t.cuts.indexOf(cut);
  const [left, right] = [ps[i], ps[i + 1]];
  const cuts = t.cuts.filter((c) => c !== cut);
  const mutes = covered(t.mutes, ...left) && covered(t.mutes, ...right)
    ? t.mutes : normMutes(subtractRange(t.mutes, left[0], right[1]), t.duration_sec || 0);
  return { cuts, mutes };
}

/** 再生で gain を 0 にする区間（タイムラインの秒）。フェードが重なるほど近い区間は 1 つにつなぐ。 */
export function gainRegions(mutes, start) {
  const out = [];
  for (const [a, b] of mutes || []) {
    const A = start + a; const B = start + b;
    const last = out[out.length - 1];
    if (last && A - last[1] < CLIP_FADE * 2) last[1] = Math.max(last[1], B);
    else out.push([A, B]);
  }
  return out;
}

/**
 * 1 回の予約（タイムラインの [from, to] を AudioContext の時刻 when から鳴らす）の gain を決める。
 * 区間の中は 0。フェードは区間の**外側**に置く（手前 5 ms で 1 → 0、後ろ 5 ms で 0 → 1。書き出しと同じ）。
 * param は AudioParam（か、setValueAtTime / linearRampToValueAtTime を持つもの）。
 */
export function planClipGain(param, regions, when, from, to) {
  const at = (t) => when + (t - from);
  param.setValueAtTime(regions.some(([a, b]) => from >= a && from < b) ? 0 : 1, when);
  for (const [a, b] of regions) {
    if (b <= from || a >= to) continue;
    if (a > from) {
      const t1 = at(a);
      param.setValueAtTime(1, Math.max(when, t1 - CLIP_FADE));
      param.linearRampToValueAtTime(0, t1);
    }
    if (b < to) {
      const t0 = at(b);
      param.setValueAtTime(0, t0);
      param.linearRampToValueAtTime(1, t0 + CLIP_FADE);
    }
  }
}
