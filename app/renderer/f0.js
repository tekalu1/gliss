// ピッチ（F0）検出の方式（編集 > ピッチ検出の方式）。ユーザー設定（state.json の f0Estimator。取り消しの履歴に入れない）。
// 選んだ方式は、起動するときに環境変数 GLISS_F0_ESTIMATOR でエンジンに渡す（main.mjs）。替えたら set_f0_estimator で
// エンジンの方式を替え、開いているトラックを解析し直す（エンジンは保存した解析が別の方式のものなら解析し直す）。
// 選んでいなければ、エンジンは曲ごとに前に解析した方式で解析する（まだ解析していない曲は既定の Gliss。
// 既定を替えても、解析・編集済みの曲の音符の区切りを変えない）。チェックは開いている曲の方式。
// RMVPE の重みが無いときは、RMVPE を選んでいてもエンジンは同梱の Gliss の F0 モデルで解析する（effective）。
import { S } from './state.js';
import { call, status } from './engine.js';
import { setF0Estimator } from './edits.js';

export const F0_ESTIMATORS = [
  { id: 'gliss', label: 'Gliss（既定）' },
  { id: 'rmvpe', label: 'RMVPE' },
  { id: 'praat', label: 'Praat' },
];

// chosen: 選んでいる方式（選んでいなければ既定）、effective: 開いている曲でエンジンが実際に使う方式、
// rmvpe: RMVPE の重みがあるか
const F = { chosen: 'gliss', effective: 'gliss', rmvpe: true };
let changed = () => {};

export function f0State() { return { ...F }; }
export function onF0Change(fn) { changed = fn; }
export function f0Label(id) { return F0_ESTIMATORS.find((e) => e.id === id)?.label || id; }

/** engine_info・set_f0_estimator の結果を写す。 */
export function adoptF0(r) {
  if (!r || r.ok === false) return;
  F.chosen = r.f0_estimator ?? r.estimator ?? F.chosen;
  F.effective = r.f0_estimator_effective ?? r.effective ?? F.effective;
  if (r.rmvpe_model_found !== undefined) F.rmvpe = !!r.rmvpe_model_found;
  changed();
}

export async function refreshF0() {
  try { adoptF0(await call('engine_info', {})); } catch { /* エンジンに聞けなければ前のまま */ }
}

/** analyze_take の結果（解析した方式）をチェックに写す。曲ごとに方式が違うことがある（前に解析した方式のまま）。 */
export function adoptAnalysis(r) {
  const est = r?.f0?.estimator;
  if (!est || est === F.effective || !F0_ESTIMATORS.some((e) => e.id === est)) return;
  F.effective = est;
  changed();
}

/** 方式を選ぶ。開いているトラックがあれば解析し直す（編集の順番待ちの中で。終わるまで次の操作を始めない）。 */
export async function chooseF0(id) {
  if (id === 'rmvpe' && !F.rmvpe) {
    status('RMVPE のモデルがまだ無い（ヘルプ > モデルと追加の機能… で取得できる）');
    return;
  }
  // 開いている曲がもうその方式（選び直しても何も変えない）。曲が無ければ、選んでいる方式と同じなら何もしない
  if (S.take ? id === F.effective : id === F.chosen && id === F.effective) return;
  window.api?.saveState?.({ f0Estimator: id });
  if (!S.take) {
    adoptF0(await call('set_f0_estimator', { estimator: id }));
    status(`ピッチ検出の方式: ${f0Label(id)}（次に開くトラックから）`);
    return;
  }
  const r = await setF0Estimator(id, adoptF0);
  if (r !== null) status(`ピッチ検出の方式: ${f0Label(id)}（解析し直した）`);
}
