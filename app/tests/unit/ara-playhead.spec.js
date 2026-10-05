import { test, expect } from '@playwright/test';

test('ARA position uses host timestamps, keeps song/source axes separate, and snaps to seek, loop and stop', async () => {
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
    const { S } = await import('../../renderer/state.js');
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
  } finally {
    if (original) Object.defineProperty(globalThis, 'performance', original);
    else delete globalThis.performance;
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.requestAnimationFrame = oldRaf;
    globalThis.cancelAnimationFrame = oldCancel;
  }
});
