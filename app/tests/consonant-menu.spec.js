// 子音・息（音程の無い区間）を、音程ノートと同じメニューと操作で扱う（2026-10-10 承認。docs/user-guide.md）。
//
//   (M1) 「ここで分ける」: Alt+X（再生位置で）とメニューの両方で、子音・息も分けられる。Ctrl+Z で戻る
//   (M2) 無効の項目は、ホバーで理由を出す（title）。すべてのメニューに効く
//   (M3) 体・端の右クリックで、音程ノートと同じノートのメニュー（音程の要る項目は無効で、理由は「子音・息には音程がありません」）
//   (M4) G のポップアップは、対象が無ければ「全体」と出す
//   (M5) Del・メニューの「無音にする」が子音・息にも効く
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
const PROJECT = path.join(REPO, 'projects', '_test-consonant-menu');
const USERDATA = `${PROJECT}-userdata`;

let app;
let win;
const errors = [];
let U = null;               // 子音 { id, start, end }
let B = null;               // 息 { id, start, end }

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
    const n = window.__app.S.notes.find((x) => x.kind === 'unvoiced' && x.end_sec - x.start_sec > 0.08);
    return n ? { id: n.id, start: n.start_sec, end: n.end_sec } : null;
  });
  B = await win.evaluate(() => {
    const n = window.__app.S.notes.find((x) => x.kind === 'breath' && x.end_sec - x.start_sec > 0.08);
    return n ? { id: n.id, start: n.start_sec, end: n.end_sec } : null;
  });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}
const items = () => win.evaluate(() => window.__app.menuItems());
const all = () => win.evaluate(() => window.__app.S.notes.map((n) => ({ id: n.id, kind: n.kind, s: n.start_sec, e: n.end_sec, muted: !!n.muted })));
async function view(r) {
  await win.evaluate((x) => {
    window.__app.S.view = { t0: Math.max(0, x.start - 0.5), span: (x.end - x.start) + 1.0 };
    window.__app.render();
  }, r);
}
async function select(ids) {
  await win.evaluate((s) => { window.__app.S.sel = s; window.__app.render(); }, ids);
}
async function escape() {
  await win.keyboard.press('Escape');
  await expect(win.locator('#menu')).toBeHidden();
}

for (const [name, get] of [['子音', () => U], ['息', () => B]]) {
  test(`(M1) ${name}: Alt+X（再生位置）で分けられる。Ctrl+Z で戻る`, async () => {
    const r = get();
    expect(r).not.toBeNull();
    await view(r);
    const n0 = (await all()).length;
    const mid = (r.start + r.end) / 2;
    await win.evaluate((t) => { window.__app.S.head = window.__app.S.off + t; window.__app.render(); }, mid);
    await select([r.id]);
    await win.keyboard.press('Alt+x');
    await settle();
    const after = await all();
    expect(after.length).toBe(n0 + 1);
    const left = after.find((n) => n.id === r.id);
    const right = after.find((n) => n.id.startsWith(`${r.id}@`));
    expect(right?.kind).toBe(left.kind);
    expect(Math.abs(left.e - mid)).toBeLessThan(0.002);
    expect(Math.abs(right.s - mid)).toBeLessThan(0.002);
    expect(await win.evaluate(() => window.__app.undoTitle())).toBe('元に戻す: 分割（Ctrl+Z）');
    await win.keyboard.press('Control+z');
    await settle();
    expect((await all()).length).toBe(n0);
  });

  test(`(M1) ${name}: メニューの「ここで分ける」で分けられる（はさみのツールで右クリック）`, async () => {
    const r = get();
    await view(r);
    await select([]);
    await win.evaluate(() => window.__app.setTool('cut'));
    const n0 = (await all()).length;
    await win.locator(`#roll rect[data-note="${r.id}"]`).first().click({ button: 'right' });
    const split = (await items()).find((x) => x.cmd === 'split');
    expect(split).toMatchObject({ label: 'ここで分ける', key: 'Alt+X', disabled: false });
    await win.locator('#menu [data-cmd="split"]').click();
    await settle();
    const after = await all();
    expect(after.length).toBe(n0 + 1);
    expect(after.find((n) => n.id.startsWith(`${r.id}@`))?.kind).toBe(after.find((n) => n.id === r.id).kind);
    await win.keyboard.press('Control+z');
    await settle();
    expect((await all()).length).toBe(n0);
    await win.evaluate(() => window.__app.setTool('main'));
  });
}

test('(M2) 無効の項目はホバーで理由が出る（ノート・空白・ルーラー・歌詞の段）', async () => {
  const r = U;
  await view(r);
  // 子音（はさみのツール）: 半音・なだらかさは音程が無いので無効。有効の項目には理由を出さない
  await win.evaluate(() => window.__app.setTool('cut'));
  await win.locator(`#roll rect[data-note="${r.id}"]`).first().click({ button: 'right' });
  let it = await items();
  const by = (c) => it.find((x) => x.cmd === c);
  expect(by('semitone')).toMatchObject({ disabled: true, title: '子音・息には音程がありません' });
  expect(by('transition')).toMatchObject({ disabled: true, title: '子音・息には音程がありません' });
  expect(by('split')).toMatchObject({ disabled: false, title: '' });
  await expect(win.locator('#menu [data-cmd="semitone"]')).toHaveAttribute('title', '子音・息には音程がありません');
  await escape();
  await win.evaluate(() => window.__app.setTool('main'));
  // ルーラー: ループが無い
  const rb = await win.locator('#roll').boundingBox();
  await win.mouse.click(rb.x + rb.width / 2, rb.y + 8, { button: 'right' });
  it = await items();
  expect(it.find((x) => x.id === 'clear-loop')).toMatchObject({ disabled: true, title: 'ループが無い' });
  await escape();
  // 歌詞の段（歌詞なし）: 消す歌詞が無い
  await win.mouse.click(rb.x + rb.width / 2, rb.y + rb.height - 30, { button: 'right' });
  it = await items();
  expect(it.find((x) => x.id === 'clear-lyrics')).toMatchObject({ disabled: true, title: '消す歌詞が無い' });
  await escape();
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
