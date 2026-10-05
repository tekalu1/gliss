// ピッチ（F0）検出の方式（編集 > ピッチ検出の方式）。単体版の全体選択は state.json に保存し、
// 起動時に GLISS_F0_ESTIMATOR でエンジンに渡す。ARA での選択は現在の修飾だけに適用し、
// 履歴で戻せる。現在の修飾の選択は起動時の既定として保存しない。
// 選んでいなければ、エンジンは曲ごとに前に解析した方式で解析する（まだ解析していない曲は既定の RMVPE。
// RMVPE の重みを後から取っても、解析・編集済みの曲の音符の区切りを変えない）。チェックは開いている曲の方式。
// RMVPE の重みが無いときは、RMVPE を選んでいてもエンジンは同梱の Gliss の F0 モデルで解析する（effective）。
import { S } from './state.js';
import { call, status } from './engine.js';
import { setF0Estimator } from './edits.js';
import { ARA } from './ara.js';

export const F0_ESTIMATORS = [
  { id: 'rmvpe', label: 'RMVPE（既定）' },
  { id: 'gliss', label: 'Gliss' },
  { id: 'praat', label: 'Praat' },
];

// chosen: 選んでいる方式（選んでいなければ既定）、effective: 開いている曲でエンジンが実際に使う方式、
// rmvpe: RMVPE の重みがあるか
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

export async function refreshF0({ syncSaved = false } = {}) {
  try {
    const info = await call('engine_info', {});
    adoptF0(info);
    // 単体版の全体選択だけが起動時の既定を変える。ARA の現在修飾は保存しない。
    if (syncSaved && !ARA && Object.hasOwn(info, 'f0_estimator_chosen')) {
      await window.api?.saveState?.({ f0Estimator: info.f0_estimator_chosen });
    }
  } catch { /* エンジンに聞けなければ前のまま */ }
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
  if (!S.take) {
    adoptF0(await call('set_f0_estimator', { estimator: id }));
    await refreshF0();
    await window.api?.saveState?.({ f0Estimator: id });
    status(`ピッチ検出の方式: ${f0Label(id)}（次に開くトラックから）`);
    return;
  }
  const r = await setF0Estimator(id, adoptF0);
  await refreshF0({ syncSaved: true });
  if (r !== null) status(`ピッチ検出の方式: ${f0Label(id)}（解析し直した）`);
}
