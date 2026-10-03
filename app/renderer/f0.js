// ピッチ（F0）検出の方式（編集 > ピッチ検出の方式）。ユーザー設定（state.json の f0Estimator。取り消しの履歴に入れない）。
// エンジンの既定は、起動するときに環境変数 GLISS_F0_ESTIMATOR で渡す（main.mjs）。替えたら set_f0_estimator で
// エンジンの既定を替え、開いているトラックを解析し直す（エンジンは保存した解析が別の方式のものなら解析し直す）。
// RMVPE の重みが無いときは、RMVPE を選んでいてもエンジンは同梱の Gliss の F0 モデルで解析する（effective）。
import { S } from './state.js';
import { call, status } from './engine.js';
import { setF0Estimator } from './edits.js';

export const F0_ESTIMATORS = [
  { id: 'rmvpe', label: 'RMVPE（既定）' },
  { id: 'gliss', label: 'Gliss（試作）' },
  { id: 'praat', label: 'Praat' },
];

// chosen: 選んでいる方式、effective: エンジンが実際に使う方式、rmvpe: RMVPE の重みがあるか
const F = { chosen: 'rmvpe', effective: 'rmvpe', rmvpe: true };
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

/** 方式を選ぶ。開いているトラックがあれば解析し直す（編集の順番待ちの中で。終わるまで次の操作を始めない）。 */
export async function chooseF0(id) {
  if (id === 'rmvpe' && !F.rmvpe) {
    status('RMVPE のモデルがまだ無い（ヘルプ > モデルと追加の機能… で取得できる）');
    return;
  }
  if (id === F.chosen && id === F.effective) return;
  window.api?.saveState?.({ f0Estimator: id });
  if (!S.take) {
    adoptF0(await call('set_f0_estimator', { estimator: id }));
    status(`ピッチ検出の方式: ${f0Label(id)}（次に開くトラックから）`);
    return;
  }
  const r = await setF0Estimator(id, adoptF0);
  if (r !== null) status(`ピッチ検出の方式: ${f0Label(id)}（解析し直した）`);
}
