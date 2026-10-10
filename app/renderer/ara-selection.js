// DAW の選択（ホストが選んだリージョンのトラック）を、画面の編集対象へ当てるかの判断（ara.js の onSelection が使う）。
// 何も import しない（単体試験は tests/unit/ara-selection.spec.js）。
//
//  - 最後に当てた選択と同じ知らせ（ホストの選択の中身が同じ）は捨てる。同じトラックの同じリージョンの知らせのたびに、
//    編集対象を選び直して表示範囲をリージョンに戻さない（利用者が画面で別のトラックへ移っていても、同じ選択の知らせでは戻さない）
//  - 編集中（ドラッグ・確定中・開いている途中）に来た、今の編集対象と同じトラックの知らせは捨てる（あとで当て直さない）。
//    編集対象と違うトラックへ変わった知らせ（利用者が DAW で別のリージョンを選んだ）は、静かになってから当てる
//  - 選択が空（null）の知らせは何も変えない
export const selectionKey = (sel) => (sel && sel.track_id
  ? `${sel.track_id}|${sel.ara_id ?? ''}|${sel.region?.id ?? ''}` : null);

export function createSelectionGate() {
  let applied = null;
  return {
    /** 'skip-empty' | 'skip-same' | 'drop-editing' | 'apply'。editing: 編集中か、currentTrack: 今の編集対象のトラック id。 */
    decide(sel, { editing = false, currentTrack = null } = {}) {
      const key = selectionKey(sel);
      if (key === null) return 'skip-empty';
      if (key === applied) return 'skip-same';
      if (editing && sel.track_id === currentTrack) { applied = key; return 'drop-editing'; }
      return 'apply';
    },
    /** 当てた（当てる）選択を覚える。 */
    markApplied(sel) { const key = selectionKey(sel); if (key !== null) applied = key; },
    isApplied(sel) { const key = selectionKey(sel); return key !== null && key === applied; },
  };
}
