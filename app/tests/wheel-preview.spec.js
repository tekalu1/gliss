// ホイール操作を Studio One に合わせる・設定画面で変える／ノートをつかんでいる間のプレビュー音（issue #27）。
//
//   (M)  テストの起動は音を一切出さない（Chromium の --mute-audio・setAudioMuted・出力の音量 0）
//   (W1) 既定の割り当て: ホイール = 縦スクロール、Shift = 横スクロール（Windows の deltaX も）、Ctrl = 縦ズーム、
//        Ctrl+Shift = 横ズーム（どちらもポインタの位置が中心）、タッチパッドの横の量 = 横スクロール
//   (W2) トラックビュー: Ctrl+ホイール = トラックの高さ、Ctrl+Shift は何もしない（ページの拡大もしない）
//   (W3) 設定画面の「ホイール」: 一覧・修飾キー＋ホイールで割り当て（キーは受け付けない）・重なり・保存・効く・既定に戻す
//   (P1) ピッチのドラッグ: つかんだら今の高さで鳴らし、高さに追従（間引く）、離したら止める
//   (P2) 端のドラッグ・移動でもつかんだノートを鳴らす
//   (P3) 設定（つかんだノートを鳴らす）で切る → 鳴らさない（メニューのチェック・保存）
//   (P4) 再生中は鳴らさない・鳴らしている間に再生を始めたら止める
// 音は出さないので、プレビューは「鳴らそうとしたもの（どのノートを・どの区間・どの高さで）」の記録で確かめる。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const GUIDE = M.clip('C2');
const PROJECT = path.join(REPO, 'projects', '_test-wheel-preview');
const USERDATA = `${PROJECT}-userdata`;
const DOCS = path.join(APP, 'screenshots');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ...M.RMVPE_ENV, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  for (const d of [PROJECT, USERDATA]) fs.rmSync(d, { recursive: true, force: true });
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
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
}
async function showRange(a, b) {
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [a, b]);
}
const pview = () => win.evaluate(() => window.__app.pitchView());
const hview = () => win.evaluate(() => window.__app.view());
const plog = () => win.evaluate(() => window.__app.previewLog());
const pstate = () => win.evaluate(() => window.__app.previewState());
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
const edge = (id, which) => win.locator(`#roll rect[data-note="${id}"][data-edge="${which}"]`).first();
/** #roll の中の y（px）→ その高さの音程（draw.js の M と同じ式）。 */
const pitchAt = (y) => win.evaluate((yy) => {
  const S = window.__app.S; const r = document.querySelector('#roll').getBoundingClientRect();
  const top = 20; const bot = r.height - 46;
  return S.pv.top - (yy - top) / ((bot - top) / S.pv.span);
}, y);
/** #roll の中の x（px）→ その時刻（編集後の秒）。 */
const timeAt = (x) => win.evaluate((xx) => {
  const S = window.__app.S; const W = document.querySelector('#roll').getBoundingClientRect().width;
  return S.view.t0 + (xx - 44) / (W - 44) * S.view.span;
}, x);

async function wheelAt(x, y, dx, dy, mods = []) {
  await win.mouse.move(x, y);
  for (const k of mods) await win.keyboard.down(k);
  await win.mouse.wheel(dx, dy);
  for (const k of mods.slice().reverse()) await win.keyboard.up(k);
}
async function resetView() {
  await win.evaluate(() => {
    const a = window.__app;
    a.S.view = { t0: 2.2, span: 1.3 };
    a.S.pv = { top: a.S.midiHi + 2, span: 18 };
    a.render();
  });
}
/** いちばん長い音程ノート（画面に入っているもの）。 */
async function longNote() {
  const ns = await win.evaluate(() => {
    const S = window.__app.S;
    return S.pitched.filter((n) => n.kind === 'note' && n.pitch_editable)
      .map((n) => ({ id: n.id, len: n.edited_end_sec - n.edited_start_sec, s: n.edited_start_sec, e: n.edited_end_sec }))
      .filter((n) => n.s >= S.view.t0 && n.e <= S.view.t0 + S.view.span);
  });
  return ns.sort((a, b) => b.len - a.len)[0];
}
function stateJson() {
  return JSON.parse(fs.readFileSync(path.join(USERDATA, 'state.json'), 'utf8'));
}

// ---------------------------------------------------------------- 音を出さない
test('(M) テストの起動は音を一切出さない（--mute-audio・setAudioMuted・出力の音量 0）', async () => {
  expect(await win.evaluate(() => window.api.muted)).toBe(true);
  expect(await win.evaluate(() => window.__app.audioMuted())).toBe(true);
  expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.isAudioMuted())).toBe(true);
  expect(await app.evaluate(({ app: a }) => a.commandLine.hasSwitch('mute-audio'))).toBe(true);
});

// ---------------------------------------------------------------- ホイール
test('(W1) 既定: ホイール = 縦スクロール、Shift = 横スクロール、Ctrl = 縦ズーム、Ctrl+Shift = 横ズーム', async () => {
  await resetView();
  const r = await win.locator('#roll').boundingBox();
  const x = 600; const y = 200;
  // ホイール = 縦スクロール（下へ回す = 低い方へ）。横は動かない
  let p0 = await pview(); let h0 = await hview();
  await wheelAt(r.x + x, r.y + y, 0, 100);
  await expect.poll(async () => (await pview()).top).toBeLessThan(p0.top - 0.9);
  expect((await pview()).span).toBe(p0.span);
  expect(await hview()).toEqual(h0);
  // Shift+ホイール = 横スクロール（縦の量でも、Windows の横の量でも）。縦は動かない
  p0 = await pview(); h0 = await hview();
  await wheelAt(r.x + x, r.y + y, 0, 100, ['Shift']);
  await expect.poll(async () => (await hview()).t0).toBeGreaterThan(h0.t0 + 0.01);
  const h1 = await hview();
  await wheelAt(r.x + x, r.y + y, -100, 0, ['Shift']);
  await expect.poll(async () => (await hview()).t0).toBeLessThan(h1.t0 - 0.01);
  expect((await hview()).span).toBeCloseTo(h0.span, 6);
  expect(await pview()).toEqual(p0);
  // タッチパッドの横スクロール（修飾キー無しの deltaX）= 横スクロール
  h0 = await hview();
  await wheelAt(r.x + x, r.y + y, 60, 0);
  await expect.poll(async () => (await hview()).t0).toBeGreaterThan(h0.t0 + 0.005);
  expect(await pview()).toEqual(p0);
  // Ctrl+ホイール = 縦ズーム（ポインタの下の音程は動かない）。横は動かない
  h0 = await hview();
  const m0 = await pitchAt(y);
  await wheelAt(r.x + x, r.y + y, 0, -100, ['Control']);
  await expect.poll(async () => (await pview()).span).toBeLessThan(p0.span);
  expect(await pitchAt(y)).toBeCloseTo(m0, 3);
  expect(await hview()).toEqual(h0);
  // Ctrl+Shift+ホイール = 横ズーム（ポインタの下の時刻は動かない）。縦は動かない
  p0 = await pview();
  const t0 = await timeAt(x);
  await wheelAt(r.x + x, r.y + y, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => (await hview()).span).toBeLessThan(h0.span);
  expect(await timeAt(x)).toBeCloseTo(t0, 3);
  expect(await pview()).toEqual(p0);
  await wheelAt(r.x + x, r.y + y, 0, 100, ['Control', 'Shift']);
  await expect.poll(async () => (await hview()).span).toBeCloseTo(h0.span, 3);
});

test('(W2) トラックビュー: Ctrl+ホイール = トラックの高さ、Ctrl+Shift は上の横ズーム（#39。ページの拡大はしない）', async () => {
  const lanes = await win.locator('#lanes').boundingBox();
  const ts = () => win.evaluate(() => window.__app.tracksState());
  const zoom0 = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor());
  const h0 = (await ts()).trackH;
  await wheelAt(lanes.x + 300, lanes.y + 10, 0, -100, ['Control']);
  await expect.poll(async () => (await ts()).trackH).toBeGreaterThan(h0);
  await wheelAt(lanes.x + 300, lanes.y + 10, 0, 100, ['Control']);
  await expect.poll(async () => (await ts()).trackH).toBe(h0);
  const before = await ts();
  const ed0 = await hview();
  await wheelAt(lanes.x + 300, lanes.y + 10, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => { const r = (await ts()).range; return r[1] - r[0]; })
    .toBeLessThan(before.range[1] - before.range[0]);
  const after = await ts();
  expect(after.trackH).toBe(before.trackH);
  expect(await hview()).toEqual(ed0);                      // 下（エディター）は変わらない
  expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor())).toBe(zoom0);
  await win.evaluate(() => window.__app.setTracksView(null));   // 全体表示に戻す（後のテストのため）
});

test('(W3) 設定画面の「ホイール」: 一覧・修飾キー＋ホイールで割り当て・重なり・保存・効く・既定に戻す', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+,');
  await expect(win.locator('#keys')).toBeVisible();
  await expect(win.locator('#keys .kh')).toContainText('ショートカット（キー・ホイール）');
  const groups = await win.locator('#keys .kg').allTextContents();
  expect(groups[groups.length - 1]).toBe('ホイール');
  const row = (id) => win.locator(`#keys .kr[data-id="${id}"]`);
  await expect(row('wheel-zoom-v').locator('.k')).toHaveText('Ctrl+ホイール');
  await expect(row('wheel-zoom-h').locator('.k')).toHaveText('Ctrl+Shift+ホイール');
  await expect(row('wheel-scroll-v').locator('.k')).toHaveText('ホイール');
  await expect(row('wheel-scroll-h').locator('.k')).toHaveText('Shift+ホイール');
  // 検索でも出る
  await win.locator('#keys .ks').fill('ホイール');
  // ホイールの 4 行と、設定画面を開くコマンド（ショートカット（キー・ホイール）…）
  await expect(win.locator('#keys .kr')).toHaveCount(5);
  await win.locator('#keys .ks').fill('');
  await row('wheel-zoom-h').scrollIntoViewIfNeeded();
  await win.screenshot({ path: path.join(DOCS, 'screenshot-keys-wheel.png') });
  // 横ズームを Alt+ホイールに: キーは受け付けない
  await row('wheel-zoom-h').dblclick();
  await expect(row('wheel-zoom-h').locator('.k')).toHaveText('ホイールを回してください（Esc でやめる）');
  await win.keyboard.press('q');
  await expect(row('wheel-zoom-h').locator('.k')).toHaveText('ホイールを回してください（Esc でやめる）');
  const box = await win.locator('#keys').boundingBox();
  await wheelAt(box.x + box.width / 2, box.y + box.height / 2, 0, -100, ['Alt']);
  await expect(row('wheel-zoom-h').locator('.k')).toHaveText('Alt+ホイール');
  await expect(row('wheel-zoom-h').locator('.st')).toHaveText('既定: Ctrl+Shift+ホイール');
  // 縦スクロールに Ctrl+ホイールを: 縦ズームと重なる → 置き換える
  await row('wheel-scroll-v').dblclick();
  await wheelAt(box.x + box.width / 2, box.y + box.height / 2, 0, 100, ['Control']);
  await expect(win.locator('#keys .kc')).toContainText('Ctrl+ホイール は「縦ズーム（音程の幅・トラックの高さ）」に割り当て済み');
  await win.locator('#keys .kc button[data-b="rep"]').click();
  await expect(row('wheel-scroll-v').locator('.k')).toHaveText('Ctrl+ホイール');
  await expect(row('wheel-zoom-v').locator('.k')).toHaveText('—');
  await expect(win.locator('#keys .kn')).toHaveText('3 件を変更');
  // 保存（ユーザー設定）
  await expect.poll(() => stateJson().keys).toEqual({
    'wheel-zoom-h': ['Alt+Wheel'], 'wheel-scroll-v': ['Ctrl+Wheel'], 'wheel-zoom-v': [],
  });
  await win.keyboard.press('Escape');
  await expect(win.locator('#keys')).toBeHidden();
  // 効く: Alt+ホイール = 横ズーム、Ctrl+ホイール = 縦スクロール、ホイールだけ = 何もしない
  await resetView();
  const r = await win.locator('#roll').boundingBox();
  const p0 = await pview(); const h0 = await hview();
  await wheelAt(r.x + 600, r.y + 200, 0, -100, ['Alt']);
  await expect.poll(async () => (await hview()).span).toBeLessThan(h0.span);
  expect(await pview()).toEqual(p0);
  const h1 = await hview();
  await wheelAt(r.x + 600, r.y + 200, 0, 100, ['Control']);
  await expect.poll(async () => (await pview()).top).toBeLessThan(p0.top - 0.9);
  expect((await pview()).span).toBe(p0.span);
  const p1 = await pview();
  await wheelAt(r.x + 600, r.y + 200, 0, 100);
  await win.waitForTimeout(150);
  expect(await pview()).toEqual(p1);
  expect(await hview()).toEqual(h1);
  // すべて既定に戻す
  await win.keyboard.press('Control+,');
  await win.locator('#keys button[data-b="all"]').click();
  await expect(row('wheel-zoom-v').locator('.k')).toHaveText('Ctrl+ホイール');
  await expect(win.locator('#keys .kn')).toHaveText('');
  await expect.poll(() => stateJson().keys).toEqual({});
  await win.keyboard.press('Escape');
  const w = await win.evaluate(() => window.__app.wheels());
  expect(w.map((x) => [x.id, x.keys])).toEqual([
    ['wheel-zoom-v', ['Ctrl+Wheel']], ['wheel-zoom-h', ['Ctrl+Shift+Wheel']],
    ['wheel-scroll-v', ['Wheel']], ['wheel-scroll-h', ['Shift+Wheel']],
  ]);
});

// ---------------------------------------------------------------- プレビュー音
test('(P1) ピッチのドラッグ: つかんだら鳴らし、高さに追従（間引く）、離したら止める', async () => {
  await resetView();
  await settle();
  await win.evaluate(() => window.__app.clearPreviewLog());
  expect(await win.evaluate(() => window.__app.previewEnabled())).toBe(true);
  const n = await longNote();
  const b = await blob(n.id).boundingBox();
  const cx = b.x + b.width / 2; const cy = b.y + b.height / 2;
  await win.mouse.move(cx, cy);
  await win.mouse.down();
  // つかんだ: いまの高さ（0 セント）で、ノートの区間を鳴らす
  await expect.poll(async () => (await plog()).length).toBe(1);
  const first = (await plog())[0];
  expect(first.note).toBe(n.id);
  expect(first.cents).toBe(0);
  expect(first.range[0]).toBeCloseTo(n.s, 4);
  expect(first.range[1]).toBeCloseTo(n.e, 4);
  await expect.poll(async () => (await pstate()).sounding?.cents).toBe(0);
  const st = await pstate();
  expect(st.note).toBe(n.id);
  expect(st.sounding.duration).toBeCloseTo(n.e - n.s, 2);
  // 上へドラッグ: 細かく 20 回動かしても、作り直しは間引かれる。最後は今の高さ
  for (let k = 1; k <= 20; k++) await win.mouse.move(cx, cy - k * 3);
  const want = await win.evaluate((id) => Math.round((window.__app.S.local.pitch.get(id) || 0) * 1000) / 10, n.id);
  expect(want).toBeGreaterThan(50);
  await expect.poll(async () => (await pstate()).sounding?.cents, { timeout: 30000 }).toBe(want);
  const log = await plog();
  expect(log.length).toBeGreaterThan(1);
  expect(log.length).toBeLessThan(21);
  expect(log.every((x) => x.note === n.id)).toBe(true);
  expect(log[log.length - 1].cents).toBe(want);
  // 離したら止める
  await win.mouse.up();
  await expect.poll(async () => (await pstate()).sounding).toBeNull();
  expect((await pstate()).note).toBeNull();
  await settle();
  // 当たった高さ（shift_pitch）は、最後に鳴らした高さと同じ
  const cents = await win.evaluate((id) => window.__app.notes().find((x) => x.id === id).cents, n.id);
  expect(Math.abs(cents - want)).toBeLessThan(1.5);
  await win.keyboard.press('Control+z');
  await settle();
});

test('(P2) 端のドラッグ・移動でもつかんだノートを鳴らす', async () => {
  await resetView();
  await settle();
  const n = await longNote();
  // 端
  await win.evaluate(() => window.__app.clearPreviewLog());
  const eb = await edge(n.id, 'end').boundingBox();
  await win.mouse.move(eb.x + eb.width / 2, eb.y + eb.height / 2);
  await win.mouse.down();
  await expect.poll(async () => (await plog()).map((x) => x.note)).toEqual([n.id]);
  await expect.poll(async () => (await pstate()).sounding?.cents).toBe(0);
  await win.mouse.move(eb.x + eb.width / 2 - 6, eb.y + eb.height / 2);
  await win.mouse.up();
  await expect.poll(async () => (await pstate()).sounding).toBeNull();
  await settle();
  // 移動（横へ）
  await win.evaluate(() => window.__app.clearPreviewLog());
  const b = await blob(n.id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  await win.mouse.move(b.x + b.width / 2 + 8, b.y + b.height / 2);
  await expect.poll(async () => (await plog()).map((x) => x.note)).toEqual([n.id]);
  await win.mouse.up();
  await expect.poll(async () => (await pstate()).note).toBeNull();
  await settle();
  // 取り消して元に
  for (let i = 0; i < 2; i++) {
    const h = await win.evaluate(() => window.__app.hist());
    if (!h?.undo) break;
    await win.keyboard.press('Control+z');
    await settle();
  }
});

test('(P3) 設定で切ると鳴らさない（メニューのチェック・ユーザー設定に保存）', async () => {
  await resetView();
  await settle();
  const menuItem = async () => (await win.evaluate(() => window.__app.appMenuTemplate()))
    .find((m) => m.label === '編集').submenu.find((x) => x.cmd === 'preview-notes');
  expect(await menuItem()).toMatchObject({ label: 'つかんだノートを鳴らす', checked: true });
  await win.evaluate(() => window.__app.onMenu({ cmd: 'preview-notes' }));
  expect(await win.evaluate(() => window.__app.previewEnabled())).toBe(false);
  expect((await menuItem()).checked).toBe(false);
  await expect.poll(() => stateJson().preview).toBe(false);
  await win.evaluate(() => window.__app.clearPreviewLog());
  const n = await longNote();
  const b = await blob(n.id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  for (let k = 1; k <= 5; k++) await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - k * 3);
  await win.waitForTimeout(300);
  expect(await plog()).toEqual([]);
  expect((await pstate()).sounding).toBeNull();
  await win.mouse.up();
  await settle();
  await win.keyboard.press('Control+z');
  await settle();
  // 設定画面にも出る（キーを割り当てられる）
  await win.keyboard.press('Control+,');
  await expect(win.locator('#keys .kr[data-id="preview-notes"] .n')).toHaveText('つかんだノートを鳴らす');
  await win.keyboard.press('Escape');
  await win.evaluate(() => window.__app.onMenu({ cmd: 'preview-notes' }));
  expect(await win.evaluate(() => window.__app.previewEnabled())).toBe(true);
  await expect.poll(() => stateJson().preview).toBe(true);
});

test('(P4) 再生中は鳴らさない・鳴らしている間に再生を始めたら止める', async () => {
  await resetView();
  await settle();
  const n = await longNote();
  const b = await blob(n.id).boundingBox();
  // 鳴らしている間に再生（Space）→ 止める
  await win.evaluate(() => window.__app.clearPreviewLog());
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  await expect.poll(async () => (await pstate()).sounding?.cents).toBe(0);
  await win.evaluate(() => window.__app.runCommand('play'));
  await expect.poll(async () => (await pstate()).sounding).toBeNull();
  await win.mouse.up();
  await expect.poll(() => win.evaluate(() => window.__app.S.playing)).toBe(true);
  // 再生中につかんでも鳴らさない
  await win.evaluate(() => window.__app.clearPreviewLog());
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  await win.waitForTimeout(300);
  expect(await plog()).toEqual([]);
  await win.mouse.up();
  await win.evaluate(() => window.__app.runCommand('play'));      // 止める
  await expect.poll(() => win.evaluate(() => window.__app.S.playing)).toBe(false);
  await settle();
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
