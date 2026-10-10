import { test, expect } from '@playwright/test';

// DAW の選択への追従の判断（renderer/ara-selection.js と ara.js の onSelection）。
// 編集の最中に、同じ選択の知らせ・編集中のトラックと同じ知らせで、別のトラックへ飛ばない。
test('selection gate: same selection, empty and edit-time notifications are not applied', async () => {
  const { createSelectionGate, selectionKey } = await import('../../renderer/ara-selection.js');
  const sel = (track, ara, region) => ({ track_id: track, ara_id: ara, region: { id: region } });
  const g = createSelectionGate();
  expect(selectionKey(null)).toBeNull();
  expect(g.decide(null)).toBe('skip-empty');                       // 空は何も変えない
  expect(g.decide({ track_id: null })).toBe('skip-empty');
  expect(g.decide(sel('t1', 'a1', 'r1'), { currentTrack: 't9' })).toBe('apply');
  g.markApplied(sel('t1', 'a1', 'r1'));
  expect(g.decide(sel('t1', 'a1', 'r1'), { currentTrack: 't9' })).toBe('skip-same');   // 同じ選択の知らせ
  expect(g.decide(sel('t1', 'a1', 'r1'), { editing: true, currentTrack: 't1' })).toBe('skip-same');
  // 同じトラックの別のリージョン: 当てる（DAW で同じ修飾の別のリージョンを選んだ）
  expect(g.decide(sel('t1', 'a1', 'r2'), { currentTrack: 't1' })).toBe('apply');
  // 編集中に来た、今の編集対象と同じトラックの知らせ: 捨てる（覚えて、あとで当て直さない）
  expect(g.decide(sel('t1', 'a1', 'r2'), { editing: true, currentTrack: 't1' })).toBe('drop-editing');
  expect(g.decide(sel('t1', 'a1', 'r2'), { editing: false, currentTrack: 't1' })).toBe('skip-same');
  // 編集中でも、別のトラックへ変わった知らせは当てる（静かになってから）
  expect(g.decide(sel('t2', 'a2', 'r3'), { editing: true, currentTrack: 't1' })).toBe('apply');
  expect(g.isApplied(sel('t2', 'a2', 'r3'))).toBe(false);
  g.markApplied(sel('t2', 'a2', 'r3'));
  expect(g.isApplied(sel('t2', 'a2', 'r3'))).toBe(true);
});

test('ARA onSelection: a repeated or edit-time notification does not reselect the track or reset the view', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldCustomEvent = globalThis.CustomEvent;
  const oldSetInterval = globalThis.setInterval;
  const listeners = {};
  const hostSelection = { value: null };
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
  globalThis.setInterval = () => 0;                       // updateChip の定期実行を始めない
  globalThis.window = {
    api: {
      mode: 'ara',
      onPlayhead: (fn) => { listeners.playhead = fn; },
      onSelection: (fn) => { listeners.selection = fn; },
      onCacheState: () => {},
      onEngineState: () => {},
      hostState: async () => ({ tracks: [], selection: hostSelection.value }),
    },
    dispatchEvent() {},
    addEventListener() {},
  };
  globalThis.document = { documentElement: { dataset: {} }, querySelector: () => null, addEventListener() {}, hidden: false };
  try {
    const { araBoot, pullHostState } = await import('../../renderer/ara.js');
    const { S } = await import('../../renderer/state.js');
    S.tracks = [{ id: 't1', kind: 'vocal', offset_sec: 0, duration_sec: 100 }, { id: 't2', kind: 'vocal', offset_sec: 0, duration_sec: 100 }];
    S.session = { current: 't1' };
    S.vd = { duration_sec: 100 };
    S.drag = null; S.opening = false;
    const selected = [];
    let idle = true;
    const host = {
      status() {}, render() {}, renderTracks() {}, renderToolbar() {}, follow() {}, movePlayhead() {},
      waitFor: async (fn) => { for (let i = 0; i < 50 && !fn(); i++) await new Promise((r) => setTimeout(r, 5)); },
      idle: () => idle, isDragging: () => false,
      selectTrack: async (id, opt) => { selected.push([id, opt]); S.session = { current: id }; return true; },
      loadSession: async () => {}, busyState() {}, prepOf() {}, syncMenu() {}, reopen: async () => {}, clearEmpty() {},
    };
    const region = (id, a, b) => ({ id, mod_start: a, mod_end: b, song_start: a, song_end: b });
    const sel1 = { track_id: 't1', ara_id: 'a1', region: region('r1', 10, 20) };
    const sel2 = { track_id: 't2', ara_id: 'a2', region: region('r2', 30, 40) };
    hostSelection.value = sel1;
    await araBoot(host, { selection: sel1, engineReady: true });
    const settle = () => new Promise((r) => setTimeout(r, 40));
    expect(selected).toHaveLength(1);                      // 起動で当てた
    // 同じ選択の知らせが何度来ても選び直さない（表示範囲をリージョンに戻さない）
    for (let i = 0; i < 5; i++) { listeners.selection({ ...sel1 }); await settle(); }
    expect(selected).toHaveLength(1);
    // 画面を見え直したときの引き直しも、選択が変わっていなければ当てない
    await pullHostState(); await settle();
    expect(selected).toHaveLength(1);
    // 編集中に来た、今の編集対象と同じトラックの知らせ（別のリージョン）は捨てる。編集が終わっても当て直さない
    idle = false;
    listeners.selection({ track_id: 't1', ara_id: 'a1', region: region('r1b', 50, 60) });
    idle = true; await settle();
    expect(selected).toHaveLength(1);
    // 空の知らせは何も変えない
    listeners.selection(null); await settle();
    expect(selected).toHaveLength(1);
    // 利用者が DAW で別のトラックのリージョンを選んだら、切り替える（編集中なら静かになってから）
    idle = false;
    listeners.selection({ ...sel2 });
    await settle();
    expect(selected).toHaveLength(1);                      // 編集中は待つ
    idle = true; await settle();
    expect(selected).toHaveLength(2);
    expect(selected[1][0]).toBe('t2');
    // 切り替えた後の同じ知らせは当てない
    listeners.selection({ ...sel2 }); await settle();
    expect(selected).toHaveLength(2);
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.CustomEvent = oldCustomEvent;
    globalThis.setInterval = oldSetInterval;
  }
});
