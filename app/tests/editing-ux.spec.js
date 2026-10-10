import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repo = path.dirname(appDir);
const root = path.join(repo, 'output', 'editing-ux');
const take = path.join(root, 'synthetic.wav');

function writeTone(file) {
  const sr = 48000, frames = sr * 4;
  const notes = [[0.25, 0.70, 220], [0.82, 1.27, 247], [1.39, 1.84, 262],
    [1.96, 2.41, 294], [2.53, 2.98, 330]];
  const buf = Buffer.alloc(44 + frames * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28);
  buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(frames * 2, 40);
  for (let i = 0; i < frames; i++) {
    const t = i / sr;
    const note = notes.find(([start, end]) => t >= start && t < end);
    const env = note ? Math.min(1, (t - note[0]) / 0.025, (note[1] - t) / 0.025) : 0;
    const sample = note ? 0.35 * env * (Math.sin(2 * Math.PI * note[2] * t)
      + 0.3 * Math.sin(4 * Math.PI * note[2] * t)) : 0;
    buf.writeInt16LE(Math.round(sample * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let app, win;
test.describe.configure({ mode: 'serial' });
test.beforeAll(async () => {
  fs.mkdirSync(root, { recursive: true });
  writeTone(take);
  const env = { ...process.env, VOCAL_EDITOR_MUTE: '1', VOCAL_EDITOR_IGNORE_MOUSE: '1',
    VOCAL_EDITOR_HIDDEN: '1', VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_PROJECTS: path.join(root, 'projects'),
    ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({ args: [appDir, '--take', take, '--project-dir', path.join(root, 'project'),
    '--user-data-dir', path.join(root, 'userdata'), '--mute'], env });
  win = await app.firstWindow();
  await win.waitForFunction(() => window.__app?.ready() && window.__app.S.pitched.length > 0,
    null, { timeout: 240000 });
});
test.afterAll(async () => {
  await app?.close();
  if (!path.resolve(root).startsWith(path.resolve(repo) + path.sep)) throw new Error('fixture outside worktree');
  fs.rmSync(root, { recursive: true, force: true });
});

test('音素境界の表示はスナップと独立し、操作名を示す', async () => {
  const state = await win.evaluate(() => {
    const a = window.__app;
    const original = { bounds: a.S.bounds, boundById: a.S.boundById, sel: a.S.sel,
      showAllBounds: a.S.showAllBounds };
    const note = a.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15);
    const boundary = { id: 'synthetic-boundary', index: 0,
      edited_sec: (note.edited_start_sec + note.edited_end_sec) / 2, movable: true };
    a.S.bounds = [boundary];
    a.S.boundById = new Map([[boundary.id, boundary]]);
    a.S.sel = [];
    a.S.showAllBounds = false;
    a.render();
    const snap = a.grid().step;
    const before = document.querySelectorAll('#roll [data-bound-line]').length;
    a.onMenu({ cmd: 'phoneme-bounds' });
    const after = document.querySelectorAll('#roll [data-bound-line]').length;
    a.onMenu({ cmd: 'phoneme-bounds' });
    a.S.sel = [note.id]; a.render();
    const selected = document.querySelectorAll('#roll [data-bound-line]').length;
    const nextSnap = a.grid().step;
    const label = document.querySelector('#undoLabel').textContent;
    Object.assign(a.S, original); a.render();
    return { snap, nextSnap, before, after, selected, label };
  });
  expect(state.before).toBe(0);
  expect(state.after).toBe(1);
  expect(state.selected).toBe(1);
  expect(state.nextSnap).toBe(state.snap);
  expect(state.label).toContain('元に戻す');
});

test('端は計画待ち中も仮表示され、直後のUndoで適用されない', async () => {
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  expect(id).toBeTruthy();
  const before = await win.evaluate(() => {
    window.__app.S.busy = true; // 前の編集が終わるまで計画を待つ状態を作る
    return window.__app.S.vd.edits.length;
  });
  try {
    const edge = win.locator(`#roll rect[data-note="${id}"][data-edge="end"]`);
    const b = await edge.boundingBox();
    const x = b.x + b.width / 2, y = b.y + b.height / 2;
    await win.mouse.move(x, y); await win.mouse.down();
    await win.mouse.move(x + 35, y, { steps: 5 });
    expect(await win.locator('#roll [data-edge-draft]').count()).toBe(1);
    expect(await win.evaluate(() => window.__app.plan())).toBeNull();
    await win.mouse.up();
    await win.keyboard.press('Control+z');
    await win.evaluate(() => { window.__app.S.busy = false; });
    await win.waitForFunction(() => window.__app.idle(), null, { timeout: 30000 });
    expect(await win.evaluate(() => window.__app.S.vd.edits.length)).toBe(before);
    expect(await win.locator('#roll [data-edge-draft]').count()).toBe(0);
  } finally {
    await win.evaluate(() => { window.__app.S.busy = false; });
  }
});

test('端の一操作はplan・apply・画面反映を記録し、Undoで戻る', async () => {
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  const before = await win.evaluate((noteId) => window.__app.notes().find((n) => n.id === noteId).editedEnd, id);
  const edge = win.locator(`#roll rect[data-note="${id}"][data-edge="end"]`);
  const b = await edge.boundingBox();
  const x = b.x + b.width / 2, y = b.y + b.height / 2;
  await win.mouse.move(x, y); await win.mouse.down();
  await win.mouse.move(x + 24, y, { steps: 6 });
  await win.mouse.up();
  await win.waitForFunction(() => window.__app.idle() && !!window.__app.S.lastEdgeTiming,
    null, { timeout: 60000 });
  const result = await win.evaluate((noteId) => ({
    end: window.__app.notes().find((n) => n.id === noteId).editedEnd,
    timing: window.__app.S.lastEdgeTiming,
  }), id);
  expect(result.end).toBeGreaterThan(before + 0.005);
  expect(result.timing.planMs).toBeGreaterThanOrEqual(0);
  expect(result.timing.applyMs).toBeGreaterThanOrEqual(0);
  expect(result.timing.viewMs).toBeGreaterThanOrEqual(0);
  fs.writeFileSync(path.join(repo, 'output', 'editing-ux-plan-timing.json'), JSON.stringify(result.timing, null, 2));
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 60000 });
  const undone = await win.evaluate((noteId) => window.__app.notes().find((n) => n.id === noteId).editedEnd, id);
  expect(Math.abs(undone - before)).toBeLessThan(0.002);
});

test('鉛筆の一フレームの線も一操作として確定・Undoできる', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  const b = await win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).boundingBox();
  const x = b.x + b.width / 2, y = b.y + b.height / 2 - 15;
  const before = await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length);
  await win.mouse.move(x, y); await win.mouse.down(); await win.mouse.up();
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 60000 });
  const after = await win.evaluate(() => ({
    count: window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length,
    stroke: window.__app.S.stroke, phase: window.__app.S.strokePhase,
  }));
  expect(after.count).toBe(before + 1);
  expect(after.stroke).toBeNull();
  await win.keyboard.press('Control+z');
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 60000 });
  expect(await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length)).toBe(before);
});

test('選択ノートの試聴はキーボードの押下中だけ要求する', async () => {
  const id = await win.evaluate(() => {
    const a = window.__app;
    const note = a.S.pitched.find((n) => n.pitch_editable);
    a.S.sel = [note.id]; a.render();
    return note.id;
  });
  await win.locator('#mock').focus();
  await win.keyboard.down('p');
  await win.waitForFunction((noteId) => window.__app.previewState().note === noteId, id);
  expect(await win.locator('#auditionState').textContent()).toContain('準備');
  await win.keyboard.up('p');
  await win.waitForFunction(() => window.__app.previewState().note == null);
  expect(await win.locator('#auditionState').textContent()).toBe('');
});

test('鉛筆が明示的に拒否されたら描線を保持し、再試行できる', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  const b = await win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).boundingBox();
  const x = b.x + b.width / 2, y = b.y + b.height / 2 - 13;
  const before = await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length);
  await win.evaluate(() => { window.__app.S.busy = true; });
  try {
    await win.mouse.move(x, y); await win.mouse.down();
    await win.mouse.move(x + 18, y - 3, { steps: 4 });
    await win.mouse.up();
    await win.evaluate(() => {
      const pts = window.__app.S.stroke.points;
      window.__editingUxTimes = pts.map((p) => p[0]);
      for (const p of pts) p[0] = pts[0][0]; // engine が短すぎる範囲として明示的に拒否する
      window.__app.S.busy = false;
    });
    await win.waitForFunction(() => window.__app.idle() && window.__app.S.strokePhase === 'failed',
      null, { timeout: 30000 });
    expect(await win.locator('#strokeActions').isVisible()).toBe(true);
    expect(await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length)).toBe(before);
    await win.evaluate(() => {
      const pts = window.__app.S.stroke.points;
      pts.forEach((p, i) => { p[0] = window.__editingUxTimes[i]; });
    });
    await win.locator('#strokeRetry').click();
    await win.waitForFunction(() => window.__app.S.strokePhase === 'committed' && !window.__app.S.stroke,
      null, { timeout: 30000 });
    expect(await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length)).toBe(before + 1);
    await win.keyboard.press('Control+z');
    await win.waitForFunction(() => window.__app.idle(), null, { timeout: 30000 });
  } finally {
    await win.evaluate(() => { window.__app.S.busy = false; });
  }
});

test('鉛筆の応答不明時は描線を保持して再送を保留する', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  await app.evaluate(({ ipcMain }) => {
    globalThis.__editingUxOriginalCall = ipcMain._invokeHandlers.get('engine.call');
    globalThis.__editingUxDrawCalls = 0;
    ipcMain.removeHandler('engine.call');
    ipcMain.handle('engine.call', (event, name, args) => {
      if (name === 'set_pitch_curve') {
        globalThis.__editingUxDrawCalls += 1;
        throw new Error('synthetic response lost');
      }
      return globalThis.__editingUxOriginalCall(event, name, args);
    });
  });
  try {
    const b = await win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).boundingBox();
    await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - 12);
    await win.mouse.down(); await win.mouse.up();
    await win.waitForFunction(() => window.__app.idle() && window.__app.S.strokePhase === 'checking',
      null, { timeout: 30000 });
    expect(await win.locator('#strokeActions').isVisible()).toBe(true);
    expect(await win.locator('#strokeRetry').isDisabled()).toBe(true);
    await win.evaluate(() => window.__app.refresh());
    const calls = await app.evaluate(() => globalThis.__editingUxDrawCalls);
    expect(calls).toBe(1);
    expect(await win.evaluate(() => window.__app.S.strokePhase)).toBe('checking');
    await win.locator('#strokeCancel').click();
  } finally {
    await app.evaluate(({ ipcMain }) => {
      ipcMain.removeHandler('engine.call');
      ipcMain.handle('engine.call', globalThis.__editingUxOriginalCall);
      globalThis.__editingUxOriginalCall = null;
    });
  }
});

test('鉛筆の応答と同じトラックの再読込が競合しても描線が残らない', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  const b = await win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).boundingBox();
  const before = await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length);
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - 12);
  await win.mouse.down(); await win.mouse.up();
  await Promise.all(Array.from({ length: 3 }, () => win.evaluate(() => window.__app.refresh())));
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.stroke,
    null, { timeout: 30000 });
  expect(await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length)).toBe(before + 1);
  await win.keyboard.press('Control+z');
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 30000 });
});

test('トラック切替後は待機中の描線を旧トラックへ送らない', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const id = await win.evaluate(() => window.__app.S.pitched.find((n) => n.end_sec - n.start_sec > 0.15)?.id);
  const b = await win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).boundingBox();
  const before = await win.evaluate(() => { window.__app.S.busy = true;
    return window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length; });
  const originalTrack = await win.evaluate(() => window.__app.S.session.current);
  try {
    await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - 12);
    await win.mouse.down(); await win.mouse.up();
    await win.evaluate(() => { window.__app.S.session.current = 'synthetic-other-track'; window.__app.S.busy = false; });
    await win.waitForFunction(() => !window.__app.S.stroke && window.__app.idle(), null, { timeout: 30000 });
    expect(await win.evaluate(() => window.__app.S.vd.edits.filter((e) => e.kind === 'pitch_draw').length)).toBe(before);
  } finally {
    await win.evaluate((trackId) => { window.__app.S.session.current = trackId; window.__app.S.busy = false;
      window.__app.render(); }, originalTrack);
  }
});

test('暗色の1280pxと360pxで操作表示を確認する', async () => {
  await win.evaluate(() => { window.__app.S.sel = [window.__app.S.pitched[0].id]; window.__app.render(); });
  for (const width of [1280, 360]) {
    await win.setViewportSize({ width, height: 800 });
    await win.screenshot({ path: path.join(repo, 'output', `editing-ux-${width}.png`) });
    const label = await win.locator('#undoLabel').boundingBox();
    expect(label.x + label.width).toBeLessThanOrEqual(width);
  }
  expect(await win.locator('#bAudition').isVisible()).toBe(true);
  expect(await win.locator('#undoLabel').textContent()).toContain('元に戻す');
});
