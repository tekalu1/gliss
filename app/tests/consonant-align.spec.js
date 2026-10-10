// 子音・息（音程の無い区間）の操作を、音程ノートと揃える（2026-10-10 承認。docs/user-guide.md）。第 2 段。
//
//   (A7) 選んである子音を押しても選択を保つ。複数を選んで動かすと、選んだ区間（音程ノートも子音も）がまとめて横に動く。
//        音高のドラッグは音程ノートだけ
//   (A8) 範囲選択（空白からドラッグ）・Ctrl+A に子音・息が入る。入っても、音程の操作は音程ノートだけに効く
//   (A9) つかんでいる間は子音・息も鳴る（つかんだノートを鳴らす）。選んだものを聞く（P）も子音・息を対象にする
//   (A10) 子音・息の上にマウスを置くと濃くなる（.hov）
//   (A12) AI に頼むは子音・息も対象。文には id と秒を入れ、「区間、子音・息を含む」と書く
//   (A11) 結合は同じ種類どうしだけ（子音どうし・息どうし・音程ノートどうし）。種類の違う組は理由を出して断る
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'A');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('A');          // 音程ノートに挟まれた無声（子音）と、息がある
const PROJECT = path.join(REPO, 'projects', '_test-consonant-align');
const USERDATA = `${PROJECT}-userdata`;

let app;
let win;
const errors = [];
let U = null;               // 子音 { id, prev, next, start, end }（前後が接した音程ノート）

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--project-dir', PROJECT, '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  U = await win.evaluate(() => {
    const ns = window.__app.S.notes;
    for (let i = 1; i < ns.length - 1; i++) {
      const [a, u, b] = [ns[i - 1], ns[i], ns[i + 1]];
      if (u.kind === 'unvoiced' && a.kind === 'note' && b.kind === 'note' && u.end_sec - u.start_sec > 0.08
        && Math.abs(u.start_sec - a.end_sec) < 1e-6 && Math.abs(b.start_sec - u.end_sec) < 1e-6) {
        return { id: u.id, prev: a.id, next: b.id, start: u.start_sec, end: u.end_sec };
      }
    }
    return null;
  });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}
const body = (id) => win.locator(`#roll rect[data-nop="${id}"]:not([data-nop-edge])`).first();
const vowel = (id) => win.locator(`#roll rect[data-note="${id}"]`).first();
const sel = () => win.evaluate(() => window.__app.S.sel.slice());
const startOf = (id) => win.evaluate((i) => window.__app.S.byId.get(i).edited_start_sec, id);
const pitchOf = (id) => win.evaluate((i) => {
  const n = window.__app.S.byId.get(i);
  return n.edited_pitch_midi ?? n.pitch_midi;
}, id);
async function view(r) {
  await win.evaluate((x) => {
    window.__app.S.view = { t0: Math.max(0, x.start - 0.5), span: (x.end - x.start) + 1.0 };
    window.__app.render();
  }, r);
}
async function select(ids) {
  await win.evaluate((s) => { window.__app.S.sel = s; window.__app.render(); }, ids);
}
async function drag(loc, dx, dy = 0) {
  const b = await loc.boundingBox();
  const x = b.x + b.width / 2; const y = b.y + b.height / 2;
  await win.mouse.move(x, y);
  await win.mouse.down();
  await win.mouse.move(x + dx, y + dy, { steps: 10 });
  await win.mouse.up();
  await settle();
}

test('(A7) 選んである子音を押しても選択を保つ。まとめて横に動く（母音も子音も）。Ctrl+Z で戻る', async () => {
  expect(U).not.toBeNull();
  await view(U);
  await select([U.id, U.prev]);
  const [u0, p0, n0] = [await startOf(U.id), await startOf(U.prev), await startOf(U.next)];
  // 押しただけ（動かさない）: 選択は 2 つのまま
  const b = await body(U.id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  expect(await sel()).toEqual([U.id, U.prev]);
  await win.mouse.up();
  await settle();
  expect(await sel()).toEqual([U.id, U.prev]);
  await drag(body(U.id), 30);
  const [u1, p1, n1] = [await startOf(U.id), await startOf(U.prev), await startOf(U.next)];
  expect(u1 - u0).toBeGreaterThan(0.01);
  expect(Math.abs((p1 - p0) - (u1 - u0))).toBeLessThan(0.002);   // 選んだ母音も同じだけ動く
  expect(Math.abs(n1 - n0)).toBeLessThan(0.1);                  // 選んでいない隣は、境目の分だけ（縮む側）
  expect(await sel()).toEqual([U.id, U.prev]);
  await win.keyboard.press('Control+z');
  await settle();
  expect(Math.abs((await startOf(U.id)) - u0)).toBeLessThan(1e-6);
  expect(Math.abs((await startOf(U.prev)) - p0)).toBeLessThan(1e-6);
});

test('(A7) 選んでいない子音を押すと、そのひとつだけを選び直す。Shift で足す・外す', async () => {
  await select([U.prev]);
  await body(U.id).click();
  expect(await sel()).toEqual([U.id]);
  await select([U.prev]);
  await body(U.id).click({ modifiers: ['Shift'] });
  expect(await sel()).toEqual([U.prev, U.id]);
  await body(U.id).click({ modifiers: ['Shift'] });
  expect(await sel()).toEqual([U.prev]);
});

test('(A7) 母音を縦にドラッグすると、一緒に選んだ子音は動かない（音高は母音だけ）', async () => {
  await select([U.id, U.prev]);
  const [u0, p0, pitch0] = [await startOf(U.id), await startOf(U.prev), await pitchOf(U.prev)];
  await drag(vowel(U.prev), 0, -24);
  expect(Math.abs((await pitchOf(U.prev)) - pitch0)).toBeGreaterThan(0.3);
  expect(Math.abs((await startOf(U.id)) - u0)).toBeLessThan(1e-6);
  expect(Math.abs((await startOf(U.prev)) - p0)).toBeLessThan(1e-6);
  await win.keyboard.press('Control+z');
  await settle();
  expect(Math.abs((await pitchOf(U.prev)) - pitch0)).toBeLessThan(1e-6);
});

test('(A7) 子音を縦にドラッグしても動かない。理由を出す', async () => {
  await select([U.id]);
  const u0 = await startOf(U.id);
  await drag(body(U.id), 0, -30);
  expect(Math.abs((await startOf(U.id)) - u0)).toBeLessThan(1e-6);
  await expect(win.locator('#status')).toContainText('子音・息は音程が無いので');
});

test('(A8) Ctrl+A は音程ノートも子音・息も選ぶ。半音に合わせる（Q）は音程ノートだけに効く', async () => {
  await select([]);
  await win.keyboard.press('Control+a');
  const r = await win.evaluate(() => ({
    sel: window.__app.S.sel.length, blocks: window.__app.S.blocks.length,
    kinds: [...new Set(window.__app.S.sel.map((i) => window.__app.S.byId.get(i).kind))].sort(),
  }));
  expect(r.sel).toBe(r.blocks);
  expect(r.kinds).toContain('unvoiced');
  expect(r.kinds).toContain('note');
  expect(await win.evaluate(() => window.__app.noPitch().filter((n) => n.kind !== 'silence').length)).toBeGreaterThan(0);
  // 半音に合わせるの対象は音程ノートだけ（子音・息の id を shift_pitch に渡さない = エラーが出ない）
  const u0 = await startOf(U.id);
  await win.keyboard.press('q');
  await settle();
  expect(Math.abs((await startOf(U.id)) - u0)).toBeLessThan(1e-6);
  expect(await win.evaluate(() => window.__app.status())).not.toMatch(/失敗|エラー/);
  await win.keyboard.press('Escape');
  expect(await sel()).toEqual([]);
});

test('(A8) 空白からの範囲選択で、範囲にかかった子音・息も選ぶ（帯の高さに範囲が届かなければ選ばない）', async () => {
  await select([]);
  await view(U);
  const b = await body(U.id).boundingBox();
  const roll = await win.locator('#roll').boundingBox();
  // 子音の帯をまたぐ範囲（帯の上下を含む高さ）。空白から始める
  const y0 = b.y - 6; const y1 = b.y + b.height + 6;
  await win.mouse.move(b.x - 2, y0 - 10);
  await win.mouse.down();
  await win.mouse.move(b.x + b.width + 2, y1, { steps: 8 });
  await win.mouse.up();
  expect(await sel()).toContain(U.id);
  // 子音の帯より上だけの範囲には入らない
  await select([]);
  await win.mouse.move(b.x - 2, roll.y + 20);
  await win.mouse.down();
  await win.mouse.move(b.x + b.width + 2, roll.y + 24, { steps: 8 });
  await win.mouse.up();
  expect(await sel()).not.toContain(U.id);
  await select([]);
});

test('(A9) 子音を押している間は鳴る。離すと止まる', async () => {
  await select([]);
  await view(U);
  await win.evaluate(() => window.__app.setPreviewEnabled(true, { save: false }));
  const b = await body(U.id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  await win.waitForFunction((id) => window.__app.previewState().note === id, U.id);
  await win.mouse.up();
  await win.waitForFunction(() => window.__app.previewState().note == null);
});

test('(A9) 子音だけを選んで P（押している間）で試聴する。何も選んでいなければ使えない理由が出る', async () => {
  await select([U.id]);
  await win.locator('#mock').focus();
  await win.keyboard.down('p');
  await win.waitForFunction((id) => window.__app.previewState().note === id, U.id);
  await win.keyboard.up('p');
  await win.waitForFunction(() => window.__app.previewState().note == null);
  await select([]);
  await win.keyboard.press('p');
  expect(await win.evaluate(() => window.__app.status())).toContain('今は使えない');
});

test('(A10) 子音の上にマウスを置くと濃くなる（母音と同じ .hov）。外すと戻る', async () => {
  await select([]);
  await view(U);
  const b = await body(U.id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.waitForFunction((id) => window.__app.S.noteHover === id, U.id);
  await expect(win.locator('#roll .npg.hov')).toHaveCount(1);
  await win.mouse.move(b.x + b.width / 2, 2);
  await win.waitForFunction(() => window.__app.S.noteHover == null);
  await expect(win.locator('#roll .npg.hov')).toHaveCount(0);
});

const all = () => win.evaluate(() => window.__app.S.notes.map((n) => ({ id: n.id, kind: n.kind })));
const items = () => win.evaluate(() => window.__app.menuItems());

test('(A11) 分けた子音の 2 つを選ぶと Ctrl+J で 1 つに結合できる。Ctrl+Z で分けた状態に戻る', async () => {
  await view(U);
  const mid = (U.start + U.end) / 2;
  await win.evaluate((t) => { window.__app.S.head = window.__app.S.off + t; window.__app.render(); }, mid);
  await select([U.id]);
  const n0 = (await all()).length;
  await win.keyboard.press('Alt+x');
  await settle();
  const split = await all();
  expect(split.length).toBe(n0 + 1);
  const right = split.find((n) => n.id.startsWith(`${U.id}@`));
  expect(right?.kind).toBe('unvoiced');
  await select([U.id, right.id]);
  await win.keyboard.press('Control+j');
  await settle();
  expect((await all()).length).toBe(n0);
  await win.keyboard.press('Control+z');
  await settle();
  expect((await all()).length).toBe(n0 + 1);
  // 分けたままにしない
  await win.keyboard.press('Control+z');
  await settle();
  expect((await all()).length).toBe(n0);
});

test('(A11) 音程ノートと子音は結合できない。キーはステータスに理由を出し、メニューは無効＋理由', async () => {
  await view(U);
  await select([U.prev, U.id]);
  const n0 = (await all()).length;
  await win.keyboard.press('Control+j');
  await settle();
  expect((await all()).length).toBe(n0);
  await expect(win.locator('#status')).toContainText('同じ種類');
  // 複数選択の右クリック
  await body(U.id).click({ button: 'right' });
  const m = (await items()).find((x) => x.cmd === 'merge');
  expect(m).toMatchObject({ disabled: true });
  expect(m.title).toContain('同じ種類');
  await win.keyboard.press('Escape');
  // 境目（子音の終わりと次の音程ノート）のメニュー
  await select([]);
  await win.locator(`#roll rect[data-nop="${U.id}"][data-nop-edge="end"]`).first().click({ button: 'right' });
  const b = (await items()).find((x) => x.cmd === 'merge');
  expect(b).toMatchObject({ disabled: true });
  expect(b.title).toContain('同じ種類');
  await win.keyboard.press('Escape');
});

test('(A12) 子音を選んで「AI に頼む」: 有効。文に子音の id・秒・「区間、子音・息を含む」が入る', async () => {
  await view(U);
  await select([U.prev, U.id]);
  await body(U.id).click({ button: 'right' });
  const a = (await items()).find((x) => x.cmd === 'ask-ai');
  expect(a).toMatchObject({ disabled: false, title: '' });
  await win.keyboard.press('Escape');
  const t = await win.evaluate(() => window.__app.askText());
  expect(t.text).toContain(`${U.prev}〜${U.id}（`);
  expect(t.text).toContain('2 区間、子音・息を含む）');
  expect(t.note).toBe('2 区間をコピー');
  await select([U.id]);
  expect((await win.evaluate(() => window.__app.askText())).text).toContain(`${U.id}（`);
  await select([]);
});

test('エラーなし', () => {
  expect(errors).toEqual([]);
});
