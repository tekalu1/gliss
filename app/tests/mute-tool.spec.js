// ミュートツール（キー 4）と「無音を戻す」、無音のノートの見分け。音声は合成（素材は要らない）。
//
//   (M1) ツールの切り替え: 4・ヘッダーのアイコン・カーソル・ツールチップ・押した印、トラックビューは今までどおり
//   (M2) クリックで無音⇔戻す（トグル）。ピッチの編集は残る。無音のノートは点線の輪郭・灰色の線
//   (M3) なぞると、押したノートと同じ向きにそろえて 1 回の編集（Ctrl+Z 1 回で全部戻る）
//   (M4) 無音を戻す（コマンド・右クリック）: 無音のノートを選んだときだけ。無音だけが戻る
//   (M5) トラックビューの編集中トラックに無音の区間が出る
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SHOTS = process.env.GLISS_SCREENSHOT_DIR || path.join(os.tmpdir(), 'gliss-mute-tool-shots');
const SR = 44100;
// 長い音 4 つ（間は 0.25 秒の無音）。音の高さはばらばら（ピアノロールの縦にずれて並ぶ）
const NOTES = [[0.4, 1.0, 220], [1.25, 1.85, 247], [2.1, 2.7, 277], [2.95, 3.55, 330]];

function writeTones(file) {
  const n = Math.round(SR * 4.0);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) {
    const t = i / SR;
    const nt = NOTES.find(([a, b]) => t >= a && t < b);
    let v = 0;
    if (nt) {
      const env = Math.min(1, (t - nt[0]) / 0.03, (nt[1] - t) / 0.03);
      for (let k = 1; k <= 6; k++) v += Math.sin(2 * Math.PI * nt[2] * k * t) / k;
      v *= 0.25 * env;
    }
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let app;
let win;
let dir;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-mute-spec-'));
  const wav = path.join(dir, 'tones.wav');
  writeTones(wav);
  fs.mkdirSync(SHOTS, { recursive: true });
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_EDITOR_MUTE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_WORK_DIR: path.join(dir, 'work'),
    VOCAL_ENGINE_PROJECTS: path.join(dir, 'projects') };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({
    args: [APP, wav, '--project-dir', path.join(dir, 'project'), '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await win.waitForFunction(() => window.__app.notes().length >= 4, null, { timeout: 120000 });
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

const settle = () => win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
const notes = () => win.evaluate(() => window.__app.notes());
const muted = async () => (await notes()).filter((n) => n.muted).map((n) => n.id);
const hist = () => win.evaluate(() => window.__app.hist());
const rect = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
async function center(id) {
  const b = await rect(id).boundingBox();
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}
async function undoAll() {
  await win.evaluate(async () => {
    while (window.__app.S.vd.history.can_undo) await window.__app.onMenu({ cmd: 'undo' });
  });
  await settle();
}
const shot = (name) => win.screenshot({ path: path.join(SHOTS, `${name}.png`) });

test('(M1) ツールの切り替え: 4・アイコン・カーソル・ツールチップ。トラックビューは今までどおり', async () => {
  await win.locator('#mock').focus();
  expect(await win.evaluate(() => window.__app.tool())).toBe('main');
  await win.keyboard.press('4');
  expect(await win.evaluate(() => window.__app.tool())).toBe('mute');
  expect(await win.locator('#roll').getAttribute('class')).toBe('tool-mute');
  expect(await win.locator('#bToolMute').getAttribute('aria-pressed')).toBe('true');
  expect(await win.locator('#bToolMain').getAttribute('aria-pressed')).toBe('false');
  // 端のつまみは出さない（ツールの操作だけ）・カーソルはスピーカー×の絵
  expect(await win.locator('#roll rect[data-edge]').count()).toBe(0);
  const cur = await win.evaluate(() => getComputedStyle(document.querySelector('#roll rect[data-note]')).cursor);
  expect(cur).toContain('svg');
  expect(await win.locator('#bToolMute').getAttribute('title')).toContain('ミュート（4）');
  // 歌詞レーン・タイムスケール・鍵盤は、ほかのツールと同じカーソル
  const lane = await win.evaluate(() => getComputedStyle(document.querySelector('#roll rect[data-lane]')).cursor);
  expect(lane).toBe('text');
  const keys = await win.evaluate(() => getComputedStyle(document.querySelector('#roll rect[data-keys]')).cursor);
  expect(keys).toBe('default');
  // アイコンのクリックでも切り替わる。1 でメインに戻る
  await win.locator('#bToolCut').click();
  expect(await win.evaluate(() => window.__app.tool())).toBe('cut');
  await win.locator('#bToolMute').click();
  expect(await win.evaluate(() => window.__app.tool())).toBe('mute');
  // ショートカットの一覧にも出る（設定で変えられる）
  const cmd = await win.evaluate(() => window.__app.commands().find((c) => c.id === 'tool-mute'));
  expect(cmd).toMatchObject({ label: 'ミュート', keys: ['4'] });
  // トラックビューはツールに関係なくメインと同じ（編集対象のトラックの行を押しても何も無音にしない）
  const tv = win.locator('#tv');
  if (await tv.isVisible()) {
    const r = await win.locator('#tv .th, #heads .th').first().boundingBox().catch(() => null);
    if (r) await win.mouse.click(r.x + r.width / 2, r.y + r.height / 2);
  }
  expect(await muted()).toEqual([]);
  await win.keyboard.press('1');
  expect(await win.evaluate(() => window.__app.tool())).toBe('main');
  expect(await win.locator('#roll rect[data-edge]').count()).toBeGreaterThan(0);
});

test('(M2) クリックで無音⇔戻す。ピッチの編集は残り、無音のノートは点線・灰色の線', async () => {
  const ns = await notes();
  expect(ns.length).toBeGreaterThanOrEqual(4);
  const [a, b] = ns;
  // ピッチを編集しておく（無音を戻しても残ること）
  await win.evaluate(async (id) => { await window.api.call('shift_pitch', { note_id: id, cents: 60, author: 'human' }); await window.__app.refresh(); }, a.id);
  await settle();
  const edited = (await notes())[0].edited;
  await win.keyboard.press('4');
  await win.mouse.click(...Object.values(await center(a.id)));
  await settle();
  expect(await muted()).toEqual([a.id]);
  expect((await hist()).undo.label).toBe('無音にする');
  // 見た目: 無音のノートは .muted・点線の輪郭、ピッチの線は灰色（--fg3）。ほかは黄色のまま（乗っている間は濃くなるので外す）
  await win.mouse.move(5, 5);
  const look = await win.evaluate((id) => {
    const g = document.querySelectorAll('#roll .nb');
    const blob = document.querySelector('#roll .nb.muted .blob');
    const cs = blob && getComputedStyle(blob);
    return {
      total: g.length, mutedCount: document.querySelectorAll('#roll .nb.muted').length,
      dash: cs?.strokeDasharray, fillOp: cs && +cs.fillOpacity,
      lines: [...document.querySelectorAll('#roll [data-line]')].map((p) => p.dataset.line),
    };
  }, a.id);
  expect(look.mutedCount).toBe(1);
  expect(look.dash).not.toBe('none');
  expect(look.fillOp).toBeLessThan(0.1);
  expect(look.lines).toContain('#5c5c62');
  expect(look.lines.some((c) => c !== '#5c5c62')).toBe(true);
  await shot('roll-muted');
  // もう一度クリックで戻す（ピッチの編集は残る）
  await win.mouse.click(...Object.values(await center(a.id)));
  await settle();
  expect(await muted()).toEqual([]);
  expect((await hist()).undo.label).toBe('無音を戻す');
  expect((await notes())[0].edited).toBeCloseTo(edited, 3);
  // 取り消すと、また無音
  await win.keyboard.press('Control+z');
  await settle();
  expect(await muted()).toEqual([a.id]);
  // 空白のクリックは何も無音にしない（ふつうの範囲選択）
  const r = await win.locator('#roll').boundingBox();
  await win.mouse.click(r.x + r.width - 40, r.y + 60);
  await settle();
  expect(await muted()).toEqual([a.id]);
  void b;
  await undoAll();
  expect(await muted()).toEqual([]);
});

test('(M3) なぞると押したノートと同じ向きにそろえて、Ctrl+Z 1 回で戻る', async () => {
  const ns = await notes();
  const hBefore = (await hist()).undo?.label ?? null;
  await win.keyboard.press('4');
  const pts = [];
  for (const n of ns.slice(0, 3)) pts.push(await center(n.id));
  // 1 つ目から 3 つ目まで、間の無音の所も通ってなぞる
  await win.mouse.move(pts[0].x, pts[0].y);
  await win.mouse.down();
  for (let i = 1; i < pts.length; i++) await win.mouse.move(pts[i].x, pts[i].y, { steps: 20 });
  // 離す前から、なぞったノートは無音の見かけ
  expect((await win.evaluate(() => [...document.querySelectorAll('#roll .nb.muted')].length))).toBe(3);
  await shot('roll-drag');
  await win.mouse.up();
  await settle();
  expect(await muted()).toEqual(ns.slice(0, 3).map((n) => n.id));
  expect((await hist()).undo.label).toBe('無音にする');
  // 無音のノートから始めてなぞると、通ったノートを全部「戻す」にそろえる（無音でなかったものは無音にしない）
  const c4 = await center(ns[3].id);
  await win.mouse.move(pts[2].x, pts[2].y);
  await win.mouse.down();
  await win.mouse.move(c4.x, c4.y, { steps: 20 });       // 3 つ目（無音）→ 4 つ目（無音でない）
  await win.mouse.up();
  await settle();
  expect(await muted()).toEqual(ns.slice(0, 2).map((n) => n.id));
  expect((await hist()).undo.label).toBe('無音を戻す');
  // 1 回の Ctrl+Z で、そのなぞった分だけ戻る
  await win.keyboard.press('Control+z');
  await settle();
  expect(await muted()).toEqual(ns.slice(0, 3).map((n) => n.id));
  // もう 1 回で、最初のなぞり全部が戻る
  await win.keyboard.press('Control+z');
  await settle();
  expect(await muted()).toEqual([]);
  expect((await hist()).undo?.label ?? null).toBe(hBefore);
});

test('(M4) 無音を戻す: コマンド・右クリック。無音のノートを選んだときだけ', async () => {
  const ns = await notes();
  await win.keyboard.press('1');
  const enabled = () => win.evaluate(() => {
    const m = window.__app.appMenuTemplate();
    const flat = (items) => items.flatMap((x) => [x, ...(x.submenu ? flat(x.submenu) : [])]);
    const it = flat(m).find((x) => x.label === '無音を戻す');
    return it ? it.enabled !== false : null;
  });
  // 何も選んでいない・無音でないノートだけ選んでいる: 押せない
  await rect(ns[0].id).click();
  expect(await enabled()).toBe(false);
  await rect(ns[0].id).click({ button: 'right' });
  await expect(win.locator('#menu [data-cmd="unmute"]')).toHaveCount(0);
  await win.keyboard.press('Escape');
  // 無音にする（Del）→ 選んだまま、無音を戻すが押せる
  await rect(ns[0].id).click();
  await win.keyboard.press('Delete');
  await settle();
  expect(await muted()).toEqual([ns[0].id]);
  expect(await enabled()).toBe(true);
  // ピッチを動かしておく（戻しても残る）
  await win.evaluate(async (id) => { await window.api.call('shift_pitch', { note_id: id, cents: 40, author: 'human' }); await window.__app.refresh(); }, ns[0].id);
  await settle();
  // 右クリックのメニューに出る
  await rect(ns[0].id).click({ button: 'right' });
  await shot('menu-unmute');
  await win.locator('#menu [data-cmd="unmute"]').click();
  await settle();
  expect(await muted()).toEqual([]);
  expect((await hist()).undo.label).toBe('無音を戻す');
  const after = (await notes())[0];
  expect(after.edited - after.pitch).toBeGreaterThan(0.3);   // ピッチの編集は残る
  // 複数選んで一部だけ無音: 無音のものだけ戻す
  await win.evaluate(async (ids) => { await window.api.call('mute_notes', { note_ids: ids, author: 'human' }); await window.__app.refresh(); }, [ns[1].id, ns[2].id]);
  await settle();
  await win.evaluate((ids) => { window.__app.S.sel = ids; window.__app.render(); }, [ns[0].id, ns[1].id]);
  await win.evaluate(() => window.__app.runCommand('unmute'));
  await settle();
  expect(await muted()).toEqual([ns[2].id]);
  await undoAll();
});

test('(M5) トラックビューの編集中トラックに無音の区間が出る', async () => {
  const ns = await notes();
  expect(await win.locator('#tv [data-muted-span]').count()).toBe(0);
  await win.evaluate(async (ids) => { await window.api.call('mute_notes', { note_ids: ids, author: 'human' }); await window.__app.refresh(); }, [ns[1].id]);
  await settle();
  await win.evaluate(() => window.__app.renderTracks());
  const spans = await win.locator('#tv [data-muted-span]').count();
  expect(spans).toBeGreaterThanOrEqual(1);
  await shot('trackview-muted');
  await undoAll();
  await win.evaluate(() => window.__app.renderTracks());
  expect(await win.locator('#tv [data-muted-span]').count()).toBe(0);
});

test('(M9) 画面のエラーが無い', () => {
  expect(errors).toEqual([]);
});
