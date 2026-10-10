// すべての操作を元に戻す（issue #16）と、ツールと順番待ちの残課題（issue #6）の画面側。
//
//   (U1) 元に戻す: ツールチップ・メニューに「元に戻す: ピッチ」、戻したらステータス行に 1 回「元に戻した: ピッチ」
//   (U2) まだ当たっていない操作は Ctrl+Z で列から外すだけ（画面からすぐ消え、エンジンには当たらない）
//   (U3) 1 曲 1 本の履歴: 別のトラックの操作を戻すと、そのトラックに切り替えてから戻す
//   (U4) トラックの名前・ガイドの指定も戻せる、ミュート／ソロは履歴に入らない
//   (U5) 歌詞も戻せる
//   (U6) 書き出しは順番待ちの編集が当たってから（「前の編集を当ててから書き出します」）
//   (U7) Claude Code（MCP）からの編集も同じ履歴に入り、画面の Ctrl+Z で戻る
//   (T1) 鍵盤とルーラーの上は鉛筆・はさみでも矢印のカーソル
//   (T2) 鉛筆の描き直し: 覆われた前の線を外したプレビュー = 離した後（端 40 ms も）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-undo');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');
const TOL_ST = 0.01;

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await showRange(2.2, 3.5);
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null,
    { timeout: 240000 });
}
async function showRange(a, b) {
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [a, b]);
}
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
const notes = () => win.evaluate(() => window.__app.notes());
const pitchOf = async (id) => (await notes()).find((n) => n.id === id).edited;
const shown = async (id) => (await win.evaluate(() => window.__app.shapes())).find((n) => n.id === id).pitch;
const edits = () => win.evaluate(() => window.__app.S.vd.edits);
const hist = () => win.evaluate(() => window.__app.hist());
const status = () => win.evaluate(() => window.__app.status());
const current = () => win.evaluate(() => window.__app.S.session.current);

async function dragUp(id, dy) {
  const b = await blob(id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - dy, { steps: 6 });
  await win.mouse.up();
}
async function undoAll() {
  await settle();
  for (let i = 0; i < 40; i++) {
    const h = await hist();
    if (!h?.can_undo) break;
    await win.evaluate(() => window.__app.undo());
    await settle();
  }
}
function maxDiff(a, b, lo = 0, hi = Infinity) {
  let m = 0;
  for (let i = Math.max(0, lo); i < Math.min(a.length, b.length, hi); i++) {
    if (a[i] == null || b[i] == null) continue;
    m = Math.max(m, Math.abs(a[i] - b[i]));
  }
  return m;
}

test('(U1) 元に戻す: ツールチップとメニューに「元に戻す: ピッチ」、戻したらステータス行に 1 回', async () => {
  await win.locator('#mock').focus();
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す（Ctrl+Z）');
  expect(await win.locator('#bUndo').isDisabled()).toBe(true);
  const p0 = await pitchOf('n015');
  await dragUp('n015', 30);
  await settle();
  expect(await pitchOf('n015')).toBeGreaterThan(p0 + 0.2);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: ピッチ（Ctrl+Z）');
  expect(await win.locator('#bUndo').isDisabled()).toBe(false);
  // 編集メニュー（タイトルバーのメニューバー）も同じ名前
  await expect.poll(() => win.evaluate(() => window.__app.menubar().model
    .find((m) => m.label === '編集').submenu[0].label)).toBe('元に戻す: ピッチ');
  await win.keyboard.press('Control+z');
  await settle();
  expect(await status()).toBe('元に戻した: ピッチ');
  expect(Math.abs((await pitchOf('n015')) - p0)).toBeLessThan(1e-6);
  expect(await win.evaluate(() => window.__app.redoTitle())).toBe('やり直す: ピッチ（Ctrl+Shift+Z）');
  await expect.poll(() => win.evaluate(() => window.__app.menubar().model
    .find((m) => m.label === '編集').submenu[1].label)).toBe('やり直す: ピッチ');
  await win.keyboard.press('Control+Shift+z');
  await settle();
  expect(await status()).toBe('やり直した: ピッチ');
  expect(await pitchOf('n015')).toBeGreaterThan(p0 + 0.2);
  await undoAll();
});

test('(U2) まだ当たっていない操作は Ctrl+Z で列から外す（画面からすぐ消え、エンジンには当たらない）', async () => {
  await win.locator('#mock').focus();
  const p15 = await pitchOf('n015');
  const n0 = (await edits()).length;
  await win.evaluate(() => { window.__app.S.busy = true; });   // 前の編集を当てている最中の体
  await dragUp('n015', 30);
  await dragUp('n014', 30);
  expect(await win.evaluate(() => window.__app.pending())).toEqual(['ピッチ', 'ピッチ']);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: ピッチ（Ctrl+Z）');
  const p14 = (await notes()).find((n) => n.id === 'n014');
  expect(await shown('n014')).toBeGreaterThan(p14.edited + 0.2);
  // 最後の操作（n014）だけ外す: すぐ元の高さに戻る。n015 は順番待ちのまま
  await win.keyboard.press('Control+z');
  expect(await win.evaluate(() => window.__app.pending())).toEqual(['ピッチ']);
  expect(Math.abs((await shown('n014')) - p14.edited)).toBeLessThan(1e-6);
  expect(await shown('n015')).toBeGreaterThan(p15 + 0.2);
  expect(await status()).toContain('元に戻した: ピッチ');
  // 鉛筆の線も、当たる前なら外せる
  await win.keyboard.press('2');
  const box = await blob('n010').boundingBox();
  await win.mouse.move(box.x + box.width * 0.2, box.y);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width * 0.8, box.y - 10, { steps: 6 });
  await win.mouse.up();
  expect(await win.evaluate(() => window.__app.pending())).toEqual(['ピッチ', '鉛筆']);
  await win.keyboard.press('Control+z');
  expect(await win.evaluate(() => window.__app.stroke())).toBeNull();
  await win.keyboard.press('1');
  await win.evaluate(() => { window.__app.S.busy = false; });
  await settle();
  const es = await edits();
  expect(es.length).toBe(n0 + 1);                            // n015 のピッチだけ当たった
  expect(es.some((e) => e.kind === 'pitch_shift' && e.target.note_id === 'n015')).toBe(true);
  expect(es.some((e) => e.kind === 'pitch_draw')).toBe(false);
  expect(Math.abs((await pitchOf('n014')) - p14.edited)).toBeLessThan(1e-6);
  await undoAll();
});

test('(U3) 1 曲 1 本の履歴: 別のトラックの操作を戻すと、そのトラックに切り替えてから戻す', async () => {
  const [t1, t2] = (await win.evaluate(() => window.__app.tracks())).map((t) => t.id);
  expect(await current()).toBe(t1);
  await win.locator('#mock').focus();
  const a0 = await pitchOf('n015');
  await dragUp('n015', 30);                                  // t1 の編集
  await settle();
  await win.evaluate((id) => window.__app.selectTrack(id), t2);
  await settle();
  await showRange(2.2, 3.5);
  const id2 = (await notes()).filter((n) => n.start > 2.3 && n.end < 3.4)[1].id;
  const b0 = await pitchOf(id2);
  await dragUp(id2, 30);                                     // t2 の編集
  await settle();
  expect((await hist()).undo).toMatchObject({ label: 'ピッチ', track: t2 });
  await win.evaluate((id) => window.__app.selectTrack(id), t1);
  await settle();
  // t1 を出したまま Ctrl+Z → t2 の操作: t2 に切り替えてから戻す
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  expect(await current()).toBe(t2);
  expect(await status()).toMatch(/^元に戻した: ピッチ（編集対象を .+ に切り替え）$/);
  expect(Math.abs((await pitchOf(id2)) - b0)).toBeLessThan(1e-6);
  // もう一度 → t1 の操作: t1 に戻って戻す
  await win.keyboard.press('Control+z');
  await settle();
  expect(await current()).toBe(t1);
  expect(Math.abs((await pitchOf('n015')) - a0)).toBeLessThan(1e-6);
  // やり直しも同じ順（t1 の操作は切り替えない）
  await win.keyboard.press('Control+Shift+z');
  await settle();
  expect(await current()).toBe(t1);
  expect(await pitchOf('n015')).toBeGreaterThan(a0 + 0.2);
  await win.keyboard.press('Control+y');
  await settle();
  expect(await current()).toBe(t2);
  await win.evaluate((id) => window.__app.selectTrack(id), t1);
  await settle();
  await undoAll();
  expect(await current()).toBe(t1);
  await showRange(2.2, 3.5);
});

test('(U4) トラックの名前・ガイド・ミュートを順に戻せる', async () => {
  const [t1, t2] = (await win.evaluate(() => window.__app.tracks())).map((t) => t.id);
  const tr = async (id) => (await win.evaluate(() => window.__app.tracks())).find((t) => t.id === id);
  const name0 = (await tr(t2)).name;
  // 右クリック → 名前を変える
  await win.locator(`#heads .th[data-id="${t2}"] .nm`).click({ button: 'right' });
  await win.locator('#menu [data-cmd="rename"]').click();
  const input = win.locator('#heads input.rn');
  await input.fill('ガイドの声');
  await input.press('Enter');
  await settle();
  expect((await tr(t2)).name).toBe('ガイドの声');
  expect((await hist()).undo.label).toBe('トラックの名前');
  // 単体版のミュートも履歴に入る
  await win.locator(`#heads .th[data-id="${t2}"] button[data-act="m"]`).click();
  await settle();
  expect((await tr(t2)).mute).toBe(true);
  await expect.poll(async () => (await hist()).undo.label).toBe('トラックのミュート');
  // ガイドを外す → 戻す
  await win.locator(`#heads .th[data-id="${t2}"] button[data-act="guide"]`).click();
  await settle();
  expect((await tr(t2)).guide).toBe(false);
  expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: ガイドの指定（Ctrl+Z）');
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  expect((await tr(t2)).guide).toBe(true);
  expect((await hist()).undo.label).toBe('トラックのミュート');
  // ガイドの指定を戻すと、操作したトラック（t2）が編集対象になる（何が戻ったか見える）
  expect(await current()).toBe(t2);
  await win.keyboard.press('Control+z');
  await settle();
  expect((await tr(t2)).mute).toBe(false);
  expect((await tr(t2)).name).toBe('ガイドの声');
  await win.keyboard.press('Control+z');
  await settle();
  expect((await tr(t2)).name).toBe(name0);
  expect((await tr(t2)).mute).toBe(false);
  await win.evaluate((id) => window.__app.selectTrack(id), t1);
  await settle();
  await showRange(2.2, 3.5);
});

test('(U5) 歌詞も戻せる', async () => {
  await win.evaluate((t) => window.__app.setLyrics(t, { start_sec: 2.3, end_sec: 3.4 }), M.text('C.part2'));
  await settle();
  expect(await win.evaluate(() => window.__app.lyricsEntries().length)).toBe(1);
  expect((await hist()).undo.label).toBe('歌詞');
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  expect(await status()).toBe('元に戻した: 歌詞');
  expect(await win.evaluate(() => window.__app.lyricsEntries().length)).toBe(0);
  expect(await win.evaluate(() => window.__app.phonemes().length)).toBe(0);
  await win.keyboard.press('Control+Shift+z');
  await settle();
  expect(await win.evaluate(() => window.__app.lyricsEntries()[0].text)).toBe(M.text('C.part2'));
  expect(await win.evaluate(() => window.__app.phonemes().length)).toBeGreaterThan(3);
  await undoAll();
});

test('(U6) 書き出しは順番待ちの編集が当たってから', async () => {
  await win.locator('#mock').focus();
  await win.evaluate(() => { window.__app.S.busy = true; });
  await dragUp('n015', 30);
  const pr = win.evaluate(() => window.__app.onMenu({ cmd: 'export' }));
  await expect.poll(status).toBe('前の編集を当ててから書き出します');
  await win.evaluate(() => { window.__app.S.busy = false; });
  const r = await pr;
  await settle();
  expect(await status()).toContain('書き出した');
  expect(r.replaced_spans_sec.length).toBeGreaterThan(0);     // 離した直後のピッチが書き出しに入った
  fs.rmSync(r.path, { force: true });
  await undoAll();
});

test('(U7) Claude Code（MCP）からの編集も同じ履歴に入り、画面の Ctrl+Z で戻る', async () => {
  const p0 = await pitchOf('n014');
  await win.evaluate(async () => {
    await window.api.call('shift_pitch', { cents: 60, note_id: 'n014', author: 'ai' });
    await window.__app.refresh();
  });
  expect(await pitchOf('n014')).toBeGreaterThan(p0 + 0.5);
  expect((await hist()).undo).toMatchObject({ label: 'ピッチ', author: 'ai' });
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  expect(Math.abs((await pitchOf('n014')) - p0)).toBeLessThan(1e-6);
  // MCP の undo / redo も同じ履歴
  const r = await win.evaluate(() => window.api.call('redo', {}));
  expect(r.redone.label).toBe('ピッチ');
  await win.evaluate(() => window.__app.refresh());
  expect(await pitchOf('n014')).toBeGreaterThan(p0 + 0.5);
  await undoAll();
});

test('(T1) 鍵盤とルーラーの上は、鉛筆・はさみでも矢印のカーソル', async () => {
  await win.locator('#mock').focus();
  for (const key of ['2', '3']) {
    await win.keyboard.press(key);
    const c = await win.evaluate(() => ({
      keys: getComputedStyle(document.querySelector('#roll rect[data-keys]')).cursor,
      scale: getComputedStyle(document.querySelector('#roll rect[data-scale]')).cursor,
      note: getComputedStyle(document.querySelector('#roll rect[data-note]')).cursor,
    }));
    expect(c.keys).toBe('default');
    expect(c.scale).toBe('default');
    expect(c.note).toContain('svg');                          // ノートの上はツールのカーソル
  }
  await win.keyboard.press('1');
});

test('(T2) 鉛筆の描き直し: 覆われた前の線を外したプレビュー = 離した後', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const box = await blob('n015').boundingBox();
  const y = box.y + box.height / 2;
  const draw = async (f0, f1, dy, beforeUp = null) => {
    await win.mouse.move(box.x + box.width * f0, y - dy);
    await win.mouse.down();
    await win.mouse.move(box.x + box.width * ((f0 + f1) / 2), y - dy - 8, { steps: 6 });
    await win.mouse.move(box.x + box.width * f1, y - dy, { steps: 6 });
    const out = beforeUp ? await beforeUp() : null;
    await win.mouse.up();
    await settle();
    return out;
  };
  await draw(0.35, 0.65, 30);                               // 前の線（高く）
  expect((await edits()).filter((e) => e.kind === 'pitch_draw')).toHaveLength(1);
  // 上から広く描き直す（前の線はすっぽり覆われる）: 描いている間の曲線 = 離した後
  const pv = await draw(0.2, 0.8, -10, () => win.evaluate(() => window.__app.editedCurve()));
  const done = await win.evaluate(() => window.__app.engineCurve());
  expect((await edits()).filter((e) => e.kind === 'pitch_draw')).toHaveLength(1);   // 前の線は外れた
  expect(maxDiff(pv, done)).toBeLessThan(TOL_ST);
  await win.keyboard.press('1');
  await undoAll();
});

test('別のエンジンの保存と競合したら読み直し、順番待ちの編集も当てない', async () => {
  await undoAll();
  await showRange(2.2, 3.5);
  await win.locator('#mock').focus();
  await win.keyboard.press('1');
  await dragUp('n015', 25);
  await settle();
  expect((await edits()).filter((e) => e.kind === 'pitch_shift')).toHaveLength(1);

  // 1 つ目の保存を project.lock で止め、編集の計算後に別のエンジンが保存した状態を置く。
  const dir = await win.evaluate(() => window.__app.S.projectDir);
  const jsonPath = path.join(dir, 'project.json');
  const script = 'from vocal_engine.project.store import FileLock\nimport sys\nl=FileLock(sys.argv[1])\nassert l.acquire(5)\nprint("locked", flush=True)\nsys.stdin.readline()\nl.release()';
  const cfg = JSON.parse(fs.readFileSync(path.join(REPO, '.mcp.json'), 'utf8')).mcpServers.gliss;
  const locker = spawn(cfg.command, ['-u', '-c', script, path.join(dir, 'project.lock')],
    { env: { ...process.env, PYTHONPATH: process.env.VOCAL_ENGINE_CWD || cfg.cwd } });
  try {
    await new Promise((resolve, reject) => {
      locker.stdout.once('data', (data) => data.toString().includes('locked') ? resolve() : reject(new Error(data.toString())));
      locker.once('error', reject);
      locker.once('exit', (code) => reject(new Error(`locker exited: ${code}`)));
    });
    await dragUp('n015', 20);
    await win.waitForFunction(() => window.__app.S.busy, null, { timeout: 10000 });
    await win.waitForTimeout(250);
    await dragUp('n014', 20);
    expect(await win.evaluate(() => window.__app.S.queued)).toBe(2);
    const raw = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
    for (const c of raw.changesets) c.undone = true;
    raw.updated_at = new Date().toISOString();
    fs.writeFileSync(jsonPath, JSON.stringify(raw, null, 2), 'utf8');
  } finally {
    if (locker.exitCode === null) {
      locker.stdin.end('\n');
      await new Promise((resolve) => locker.once('exit', resolve));
    }
  }

  await settle();
  await win.waitForFunction(() => window.__app.S.vd.edits.length === 0, null, { timeout: 10000 });
  expect(await status()).toContain('今の操作は当てていない');
  expect(await win.evaluate(() => window.__app.pending())).toEqual([]);
  expect(await win.evaluate(() => window.__app.S.local.pitch.size)).toBe(0);
  expect((await notes()).find((n) => n.id === 'n014').edited)
    .toBeCloseTo((await notes()).find((n) => n.id === 'n014').pitch, 4);
  await win.waitForTimeout(600);
  expect(await status()).toContain('今の操作は当てていない');
  expect(errors).toEqual([]);
});

test('準備待ちが期限切れなら、編集のプレビューを消して再試行を案内する', async () => {
  await undoAll();
  await showRange(2.2, 3.5);
  await win.locator('#mock').focus();
  await win.keyboard.press('1');
  const before = await pitchOf('n015');
  await app.evaluate(({ ipcMain }) => {
    const handlers = ipcMain._invokeHandlers;
    globalThis.__originalEngineCall = handlers.get('engine.call');
    handlers.set('engine.call', (event, name, args) => name === 'shift_pitch'
      ? { ok: false, preparing: true, conflict: false, error: '準備中' }
      : globalThis.__originalEngineCall(event, name, args));
  });
  try {
    await dragUp('n015', 25);
    await settle();
    expect(await status()).toBe('準備中。少し待ってからもう一度');
    expect(await pitchOf('n015')).toBeCloseTo(before, 4);
    expect(await win.evaluate(() => window.__app.S.local.pitch.size)).toBe(0);
  } finally {
    await app.evaluate(({ ipcMain }) => {
      ipcMain._invokeHandlers.set('engine.call', globalThis.__originalEngineCall);
      delete globalThis.__originalEngineCall;
    });
  }
  expect(errors).toEqual([]);
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
