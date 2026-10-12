import { test, expect } from '@playwright/test';

test('ARA position uses host timestamps, keeps song/output axes separate, and snaps to seek, loop and stop', async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'performance');
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldRaf = globalThis.requestAnimationFrame;
  const oldCancel = globalThis.cancelAnimationFrame;
  let now = 0;
  const frames = new Map(); let nextFrame = 0;
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { now: () => now } });
  globalThis.window = { api: { mode: 'ara', hostState: async () => ({ tracks: [{ track_id: 'vocal', regions: [
    { song_start: 10, song_end: 12, mod_start: 2, mod_end: 3 },
  ] }] }) }, dispatchEvent() {} };
  globalThis.document = { documentElement: { dataset: {} }, querySelector: () => null };
  globalThis.requestAnimationFrame = (fn) => { const id = ++nextFrame; frames.set(id, fn); return id; };
  globalThis.cancelAnimationFrame = (id) => frames.delete(id);
  const frame = () => { const [id, fn] = frames.entries().next().value || []; if (fn) { frames.delete(id); fn(); } };
  try {
    const { onPlayhead, araEditorHead, pullHostState } = await import('../../renderer/ara.js');
    const { S, invalidateWarp } = await import('../../renderer/state.js');
    S.session = { current: 'vocal' };
    S.tracks = [{ id: 'vocal', kind: 'vocal', offset_sec: 10 }];
    S.off = 10; S.head = 0; S.playing = false; S.loop = null;
    await pullHostState();
    const send = (song, stamp, sequence, playing = true, loop = null) => onPlayhead({
      song_sec: song, stamp_ms: stamp, sequence, playing, loop,
    });

    send(10, 1000, 1);
    expect(S.head).toBe(10);
    expect(araEditorHead()).toBeCloseTo(12, 6);
    now = 33; frame();
    expect(S.head).toBeCloseTo(10.033, 6);
    now = 90; send(10.066, 1066, 2); // 24 ms of arrival jitter
    expect(S.head).toBeCloseTo(10.09, 6);
    expect(araEditorHead()).toBeCloseTo(12.045, 6);
    now = 99; send(10.099, 1099, 3);
    expect(S.head).toBeGreaterThanOrEqual(10.09);
    now = 132; send(10.132, 1132, 4);
    expect(S.head).toBeGreaterThanOrEqual(10.099);
    send(99, 1133, 3); // a stale notification cannot rewind the bar
    expect(S.head).toBeLessThan(11);

    now = 180; send(11.3, 1180, 5); // genuine seek
    expect(S.head).toBeCloseTo(11.3, 6);
    expect(araEditorHead()).toBeCloseTo(12.65, 6);
    now = 210; send(10.1, 1210, 6, true, [10, 12]); // loop wrap
    expect(S.head).toBeCloseTo(10.1, 6);
    now = 240; send(10.11, 1240, 7, false); // stopped location is exact
    expect(S.head).toBeCloseTo(10.11, 6);
    now = 300; frame();
    expect(S.head).toBeCloseTo(10.11, 6);
    // ARA mod time indexes rendered output PCM; applying time_map again would double a timing edit.
    S.vd = { time_map: { src_sec: [0, 4], out_sec: [0, 8] } };
    expect(araEditorHead() - S.off).toBeCloseTo(2.055, 6);
    S.bounds = [{ index: 0, edited_sec: 2 }, { index: 1, edited_sec: 3 }];
    S.local.btime.set(0, 2.2); S.local.btime.set(1, 3.2);
    invalidateWarp();
    // 未確定の境界ドラッグはノートの仮形だけを動かし、実 PCM の再生位置は動かさない。
    expect(araEditorHead() - S.off).toBeCloseTo(2.055, 6);
    S.head = 9; // song gap: no lower playhead or follow target
    expect(araEditorHead()).toBeNull();
    S.local.btime.clear(); S.bounds = []; invalidateWarp();
  } finally {
    if (original) Object.defineProperty(globalThis, 'performance', original);
    else delete globalThis.performance;
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.requestAnimationFrame = oldRaf;
    globalThis.cancelAnimationFrame = oldCancel;
  }
});

test('同じ修飾の重なる・離れたイベントで選択内の位置を優先し、別のイベントだけなら薄くする', async () => {
  const { araHeadForSong, araRegionForMod } = await import('../../renderer/ara.js');
  const selected = { id: 'selected', song_start: 160, song_end: 164, mod_start: 0.5, mod_end: 2.5 };
  const other = { id: 'other', song_start: 161, song_end: 165, mod_start: 2.5, mod_end: 4.5 };
  const regions = [selected, other];
  expect(araHeadForSong(regions, 160, selected)).toEqual({ head: 0.5, secondary: false });
  expect(araHeadForSong(regions, 162, selected)).toEqual({ head: 1.5, secondary: false });
  expect(araHeadForSong(regions, 164, selected)).toEqual({ head: 4, secondary: true });
  expect(araHeadForSong(regions, 164.5, selected)).toEqual({ head: 4.25, secondary: true });
  expect(araHeadForSong(regions, 165, selected)).toEqual({ head: null, secondary: false });
  expect(araHeadForSong(regions, 159, selected)).toEqual({ head: null, secondary: false });
  expect(araHeadForSong(regions, 162)).toEqual({ head: 3, secondary: false });
  const shifted = { ...other, song_start: 170, song_end: 174 };
  expect(araHeadForSong([selected, shifted], 172, selected)).toEqual({ head: 3.5, secondary: true });
  expect(araHeadForSong([selected, shifted], 166, selected)).toEqual({ head: null, secondary: false });
  expect(araHeadForSong([selected, shifted], 172)).toEqual({ head: 3.5, secondary: false });
  // シークとループは従来の範囲判定を保ち、ルーラーの延長に影響されない。
  expect(araRegionForMod(regions, 1.5, selected.id, 162)).toBe(selected);
  expect(araRegionForMod(regions, 3, selected.id, 162)).toBe(other);
  expect(araRegionForMod(regions, 5, selected.id, 162)).toBeNull();
});
