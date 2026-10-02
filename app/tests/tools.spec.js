// ツール（矢印・鉛筆・はさみ）と、ノートの変わり目のなだらかさ（REPORT-tools.md）。
//
//   (1) ツールの切り替え: 1 / 2 / 3・ヘッダーのアイコン・カーソル（svg の class）
//   (2) なだらかさ: ピッチのドラッグ中の曲線 = 離した後の曲線（つなぎ込み）
//   (3) なだらかさのポップアップ: スライダー中の曲線 = 離した後、0 で段差、ポップアップ 1 回 = 取り消し 1 回
//   (4) 鉛筆: 描いている間の曲線 = 離した後、描いた範囲が描いた値、範囲の外は変わらない、無声には描けない
//   (5) はさみ: クリックで分割（新しい境目は接続）、分割したノートを動かせる、境目のダブルクリックで結合
//   (6)〜(8) レビューで直したもの
//   (9) 前の編集の確定待ちの間に離したドラッグは捨てずに後から当たる・自分の編集を「外部の変更」と取り違えない
//   (10) 順番待ちと同じノートの再ドラッグ・Ctrl+Z、読むだけのツールの直前の外部の変更も読み直す
//
// 比べる許容誤差: 音程 1 セント。
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
const PROJECT = path.join(REPO, 'projects', '_test-tools');
// 設定（最近使ったファイル・前回の表示範囲・ウィンドウの位置）も毎回まっさらにする。人が使った
// ときの表示範囲を引き継ぐと、狙うノートが画面の外に出てドラッグが外れる（人の設定も汚さない）。
const USERDATA = `${PROJECT}-userdata`;
const TOL_ST = 0.01;
const DOCS = path.join(APP, 'screenshots');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
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
  // S.busy だけだと、離した後に計画の到着や前の編集を待っている間（まだ busy でない）を見落とす
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}
async function showRange(a, b) {
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [a, b]);
}
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
const preview = () => win.evaluate(() => window.__app.editedCurve());
const engine = () => win.evaluate(() => window.__app.engineCurve());
const edits = () => win.evaluate(() => window.__app.S.vd.edits);

/** 2 本の曲線（フレームごと）の最大の差（半音）。null は飛ばす。 */
function maxDiff(a, b, lo = 0, hi = Infinity) {
  let m = 0;
  for (let i = Math.max(0, lo); i < Math.min(a.length, b.length, hi); i++) {
    if (a[i] == null || b[i] == null) continue;
    m = Math.max(m, Math.abs(a[i] - b[i]));
  }
  return m;
}
async function frameOf(t) {
  const f = await win.evaluate(() => window.__app.f0Frame());
  return Math.round((t - f.t0) / f.hop);
}
async function undoAll() {
  await win.evaluate(async () => {
    while (window.__app.S.vd.history.can_undo) {
      await window.__app.onMenu({ cmd: 'undo' });
    }
  });
  await settle();
}

test('(1) ツールの切り替え: 1 / 2 / 3 とヘッダーのアイコン、カーソル', async () => {
  const root = win.locator('#mock');
  await root.focus();
  expect(await win.evaluate(() => window.__app.tool())).toBe('main');
  await win.keyboard.press('2');
  expect(await win.evaluate(() => window.__app.tool())).toBe('draw');
  expect(await win.locator('#roll').getAttribute('class')).toBe('tool-draw');
  expect(await win.locator('#bToolDraw').getAttribute('aria-pressed')).toBe('true');
  // 鉛筆のときはノートの端をつまむ要素を出さない（ツールの操作だけ）
  expect(await win.locator('#roll rect[data-edge]').count()).toBe(0);
  await win.keyboard.press('3');
  expect(await win.evaluate(() => window.__app.tool())).toBe('cut');
  const cur = await win.evaluate(() => getComputedStyle(
    document.querySelector('#roll rect[data-note]')).cursor);
  expect(cur).toContain('svg');                          // はさみのカーソル
  await win.locator('#bToolMain').click();
  expect(await win.evaluate(() => window.__app.tool())).toBe('main');
  expect(await win.locator('#bToolMain').getAttribute('aria-pressed')).toBe('true');
  await win.keyboard.press('1');
  expect(await win.evaluate(() => window.__app.tool())).toBe('main');
  expect(await win.locator('#roll rect[data-edge]').count()).toBeGreaterThan(0);
});

test('(2) なだらかさ: ピッチのドラッグ中の曲線 = 離した後（つなぎ込み）', async () => {
  const trs = await win.evaluate(() => window.__app.transitions());
  const tr = trs.find((t) => t.a === 'n014' && t.b === 'n015');
  expect(tr).toBeTruthy();
  expect(tr.hl + tr.hr).toBeGreaterThan(0.02);            // 自動で幅がある
  const box = await blob('n014').boundingBox();
  const x = box.x + box.width / 2; const y = box.y + box.height / 2;
  await win.mouse.move(x, y);
  await win.mouse.down();
  await win.mouse.move(x, y - 40, { steps: 8 });
  const pv = await preview();
  await win.mouse.up();
  await settle();
  const done = await engine();
  const k = await frameOf(tr.tb);
  expect(maxDiff(pv, done)).toBeLessThan(TOL_ST);
  // 境目をまたいで段差になっていない（1 フレームの差が移動量よりずっと小さい）
  const shift = done[k - 8] - (await win.evaluate(() => window.__app.origCurve()))[k - 8];
  expect(Math.abs(shift)).toBeGreaterThan(0.5);
  let jump = 0;
  for (let i = k - 6; i < k + 6; i++) {
    if (done[i] == null || done[i + 1] == null) continue;
    const o = await win.evaluate((j) => window.__app.origCurve()[j + 1] - window.__app.origCurve()[j], i);
    jump = Math.max(jump, Math.abs((done[i + 1] - done[i]) - o));
  }
  expect(jump).toBeLessThan(Math.abs(shift) * 0.5);
});

test('(3) なだらかさのポップアップ: スライダー中 = 離した後、0 で段差、取り消し 1 回', async () => {
  await blob('n014').click();
  await blob('n014').click({ button: 'right' });
  await win.locator('#menu [data-cmd="transition"]').click();
  await expect(win.locator('#popTr')).toBeVisible();
  expect(await win.locator('#popTrScope').textContent()).toBe('選択 1 ノート');
  expect(await win.locator('#popTrVV').textContent()).toBe('自動');
  const set = (v, release) => win.evaluate(([val, rel]) => {
    const el = document.querySelector('#popTrV');
    el.value = String(val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    if (rel) el.dispatchEvent(new Event('change', { bubbles: true }));
  }, [v, release]);
  await set(95, false);
  const pv = await preview();
  await win.evaluate(() => {                     // 図のために境目が隠れない位置へ
    const el = document.querySelector('#popTr');
    el.style.left = '110px'; el.style.top = '120px';
  });
  await win.screenshot({ path: path.join(DOCS, 'screenshot-transition.png') });
  await set(95, true);
  await settle();
  await win.waitForFunction(() => (window.__app.S.vd.edits || []).some((e) => e.kind === 'transition'));
  expect(maxDiff(pv, await engine())).toBeLessThan(TOL_ST);
  // 0 = 段差
  await set(0, false);
  const pv0 = await preview();
  await set(0, true);
  await settle();
  await win.waitForFunction(() => (window.__app.S.vd.edits || [])
    .some((e) => e.kind === 'transition' && e.params.value === 0));
  const d0 = await engine();
  expect(maxDiff(pv0, d0)).toBeLessThan(TOL_ST);
  const tr = (await win.evaluate(() => window.__app.transitions())).find((t) => t.a === 'n014' && t.b === 'n015');
  const k = await frameOf(tr.tb);
  const orig = await win.evaluate(() => window.__app.origCurve());
  const offL = d0[k - 1] - orig[k - 1]; const offR = d0[k] - orig[k];
  expect(Math.abs(offL - offR)).toBeGreaterThan(0.3);       // 段差（n014 だけ上げてある）
  // ポップアップ 1 回 = 取り消し 1 回（2 回離しても changeset は 1 つ）
  await win.keyboard.press('Escape');
  await expect(win.locator('#popTr')).toBeHidden();
  const live = await win.evaluate(() => window.__app.S.vd.history.changesets.filter((c) => !c.undone).length);
  expect(live).toBe(2);                                     // ピッチ + なだらかさ
  await win.evaluate(() => window.__app.onMenu({ cmd: 'undo' }));
  await settle();
  expect((await edits()).filter((e) => e.kind === 'transition')).toHaveLength(0);
  expect((await edits()).filter((e) => e.kind === 'pitch_shift')).toHaveLength(1);
  await undoAll();
});

test('(4) 鉛筆: 描いている間 = 離した後、描いた値、範囲外は不変、無声には描けない', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const before = await engine();
  const box = await blob('n015').boundingBox();
  const y = box.y + box.height / 2 - 20;
  const x0 = box.x + box.width * 0.2; const x1 = box.x + box.width * 0.8;
  await win.mouse.move(x0, y);
  await win.mouse.down();
  await win.mouse.move((x0 + x1) / 2, y - 12, { steps: 6 });
  await win.mouse.move(x1, y, { steps: 6 });
  const pv = await preview();
  const sd = await win.evaluate(() => window.__app.stroke());
  expect(sd.v0).toBeGreaterThanOrEqual(0);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-pencil.png') });
  await win.mouse.up();
  await settle();
  const done = await engine();
  expect(maxDiff(pv, done)).toBeLessThan(TOL_ST);          // 描いている間 = 離した後
  const e = (await edits()).find((x) => x.kind === 'pitch_draw');
  expect(e).toBeTruthy();
  // 描いた範囲は描いた値
  const vals = new Map(sd.pts.map(([t, m]) => [Math.round(t * 100), m]));
  let checked = 0;
  for (let i = sd.v0; i <= sd.v1; i++) {
    if (done[i] == null || !vals.has(i)) continue;
    expect(Math.abs(done[i] - vals.get(i))).toBeLessThan(TOL_ST);
    checked++;
  }
  expect(checked).toBeGreaterThan(3);
  // 範囲（＋両端 40 ms）の外は変わらない
  const lo = sd.v0 - 5; const hi = sd.v1 + 5;
  expect(maxDiff(before, done, 0, lo)).toBeLessThan(1e-6);
  expect(maxDiff(before, done, hi)).toBeLessThan(1e-6);
  // 無声（息の区間）だけに描いても編集にならない
  const n = await win.evaluate(() => window.__app.S.notes.find((q) => q.kind !== 'note'
    && q.start_sec > 1.5 && q.end_sec < 2.3 && q.end_sec - q.start_sec > 0.2));
  expect(n).toBeTruthy();
  {
    await showRange(1.4, 2.4);
    const ux0 = await win.evaluate((t) => {
      const r = document.querySelector('#roll').getBoundingClientRect();
      const S = window.__app.S; const KW = 44;
      return r.left + KW + (t - S.view.t0) / S.view.span * (r.width - KW);
    }, n.start_sec + 0.05);
    const ux1 = ux0 + 40;
    await win.mouse.move(ux0, y);
    await win.mouse.down();
    await win.mouse.move(ux1, y, { steps: 5 });
    await win.mouse.up();
    await settle();
    expect(await win.evaluate(() => window.__app.status())).toContain('無声');
    expect((await edits()).filter((x) => x.kind === 'pitch_draw')).toHaveLength(1);
    await showRange(2.2, 3.5);
  }
  await undoAll();
  await win.keyboard.press('1');
});

test('(5) はさみ: クリックで分割、分割したノートを動かせる、境目のダブルクリックで結合', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('3');
  const before = await win.evaluate(() => window.__app.allNotes());
  const box = await blob('n015').boundingBox();
  const cx = box.x + box.width * 0.5; const cy = box.y + box.height / 2;
  await win.mouse.move(cx, cy);
  await win.mouse.move(cx + 1, cy);
  const hov = await win.evaluate(() => window.__app.cutHover());
  expect(hov?.id).toBe('n015');
  await win.mouse.click(cx + 1, cy);
  await settle();
  const after = await win.evaluate(() => window.__app.allNotes());
  expect(after.length).toBe(before.length + 1);
  const right = after.find((n) => n.id.startsWith('n015@'));
  expect(right).toBeTruthy();
  expect(Math.abs(right.start - hov.src)).toBeLessThan(1e-6);
  const conn = await win.evaluate(() => window.__app.connections());
  expect(conn.find((c) => c.id === 'n015').next).toBe(true);   // 新しい境目は接続
  await win.screenshot({ path: path.join(DOCS, 'screenshot-cut.png') });
  // 分割した右側を矢印で上へ（ふつうのノートとして動く）
  await win.keyboard.press('1');
  const rb = await blob(right.id).boundingBox();
  await win.mouse.move(rb.x + rb.width / 2, rb.y + rb.height / 2);
  await win.mouse.down();
  await win.mouse.move(rb.x + rb.width / 2, rb.y + rb.height / 2 - 30, { steps: 6 });
  const pv = await preview();
  await win.mouse.up();
  await settle();
  expect(maxDiff(pv, await engine())).toBeLessThan(TOL_ST);
  const sh = (await edits()).find((e) => e.kind === 'pitch_shift');
  expect(sh.target.note_id).toBe(right.id);
  // 境目をダブルクリックで結合
  await win.keyboard.press('3');
  const j = win.locator(`#roll rect[data-join="n015|${right.id}"]`);
  await expect(j).toHaveCount(1);
  const jb = await j.boundingBox();
  await win.mouse.click(jb.x + jb.width / 2, jb.y + jb.height / 2);
  await win.mouse.click(jb.x + jb.width / 2, jb.y + jb.height / 2);
  await settle();
  const merged = await win.evaluate(() => window.__app.allNotes());
  expect(merged.length).toBe(before.length);
  expect(merged.find((n) => n.id.startsWith('n015@'))).toBeFalsy();
  await win.keyboard.press('1');
  expect(errors).toEqual([]);
});

// ---------------------------------------------------------------- レビューで直したもの（2026-09-23）
test('(6) 鉛筆: 前の編集を当てている間に離した線は、終わってから当たる・計画待ちの間は結合しない', async () => {
  await win.locator('#mock').focus();
  await win.keyboard.press('2');
  const box = await blob('n015').boundingBox();
  const y = box.y + box.height / 2 - 20;
  await win.mouse.move(box.x + box.width * 0.2, y);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width * 0.7, y, { steps: 6 });
  await win.evaluate(() => { window.__app.S.busy = true; });   // Ctrl+Z などが走っている間に離した
  await win.mouse.up();
  // 捨てない: 線は出したまま、前のが終わるのを待っていると分かる
  expect(await win.evaluate(() => window.__app.stroke())).not.toBeNull();
  expect(await win.evaluate(() => window.__app.status())).toContain('続けて当てる');
  expect((await edits()).filter((e) => e.kind === 'pitch_draw')).toHaveLength(0);
  const drawn = await preview();
  await win.evaluate(() => { window.__app.S.busy = false; });
  await settle();
  expect((await edits()).filter((e) => e.kind === 'pitch_draw')).toHaveLength(1);
  expect(await win.evaluate(() => window.__app.stroke())).toBeNull();
  expect(maxDiff(drawn, await engine())).toBeLessThan(TOL_ST);   // 待っている間の線 = 当たった後
  await undoAll();
  // はさみ: 計画の確定待ち（pendingPlan）の間の境目のダブルクリックは受け付けない
  await win.keyboard.press('3');
  const cx = box.x + box.width * 0.5;
  await win.mouse.move(cx, box.y + box.height / 2);
  await win.mouse.click(cx + 1, box.y + box.height / 2);
  await settle();
  const right = (await win.evaluate(() => window.__app.allNotes())).find((n) => n.id.startsWith('n015@'));
  expect(right).toBeTruthy();
  const j = win.locator(`#roll rect[data-join="n015|${right.id}"]`);
  const jb = await j.boundingBox();
  await win.evaluate(() => { window.__app.S.pendingPlan = true; });
  await win.mouse.click(jb.x + jb.width / 2, jb.y + jb.height / 2);
  await win.mouse.click(jb.x + jb.width / 2, jb.y + jb.height / 2);
  expect(await win.evaluate(() => window.__app.status())).toContain('結合しない');   // 断ったことは出す
  await win.evaluate(() => { window.__app.S.pendingPlan = false; });
  await settle();
  expect((await win.evaluate(() => window.__app.allNotes())).find((n) => n.id === right.id)).toBeTruthy();
  await undoAll();
  await win.keyboard.press('1');
});

test('(7) はさみ: 切れない端へは吸着しない・Alt を押す／離すだけで切る線が変わる', async () => {
  await showRange(0.0, 4.0);                 // 1 px が 3 ms ほど: 端から 8 px 以内 = 20 ms より内側がある
  await win.locator('#mock').focus();
  await win.keyboard.press('3');
  const n = await win.evaluate(() => window.__app.S.byId.get('n015'));
  const X = (t) => win.evaluate((tt) => {
    const r = document.querySelector('#roll').getBoundingClientRect();
    const S = window.__app.S; const KW = 44;
    return r.left + KW + (tt - S.view.t0) / S.view.span * (r.width - KW);
  }, t);
  const box = await blob('n015').boundingBox();
  const cy = box.y + box.height / 2;
  // 頭から 8 ms の音素境界（切れない位置）の近く、頭から 25 ms（切れる位置）では、その境界へ吸着せずに切る線が出る
  const addB = (id, t) => win.evaluate(([i, tt]) => {
    window.__app.S.bounds.push({ id: i, sec: tt, edited_sec: tt });
  }, [id, t]);
  const dropB = (id) => win.evaluate((i) => {
    const b = window.__app.S.bounds; b.splice(b.findIndex((x) => x.id === i), 1);
  }, id);
  await addB('fake0', +(n.start_sec + 0.008).toFixed(4));
  const x25 = await X(n.start_sec + 0.025);
  await win.mouse.move(x25, cy);
  await win.mouse.move(x25 + 0.5, cy);
  const h = await win.evaluate(() => window.__app.cutHover());
  await dropB('fake0');
  expect(h?.id).toBe('n015');
  expect(h.src).toBeGreaterThanOrEqual(n.start_sec + 0.02 - 1e-6);
  // Alt: 吸着先（仮の音素境界）を足して、Alt の押し・離しだけで線が吸着なし／ありに変わる
  const tb = +(n.start_sec + 0.12).toFixed(4);
  await addB('fake', tb);
  const xn = (await X(tb)) + 5;
  await win.mouse.move(xn, cy);
  await win.mouse.move(xn + 0.5, cy);
  expect((await win.evaluate(() => window.__app.cutHover())).src).toBeCloseTo(tb, 4);
  await win.keyboard.down('Alt');
  const free = await win.evaluate(() => window.__app.cutHover());
  expect(Math.abs(free.src - tb)).toBeGreaterThan(0.005);
  await win.keyboard.up('Alt');
  expect((await win.evaluate(() => window.__app.cutHover())).src).toBeCloseTo(tb, 4);
  await dropB('fake');
  await win.mouse.move(5, 5);
  await win.keyboard.press('1');
  await showRange(2.2, 3.5);
});

test('(8) なだらかさのポップアップ: 矢印キーで 1 ずつ・混在の表示・開き直しても前の編集を取り消さない', async () => {
  await win.locator('#mock').focus();
  const openOn = (ids) => win.evaluate((sel) => {
    window.__app.S.sel = sel; window.__app.render(); window.__app.openTr(200, 150);
  }, ids);
  const change = (v) => win.evaluate((val) => {
    const el = document.querySelector('#popTrV');
    el.value = String(val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, v);
  // 矢印キー: 50 → 53（「自動」への吸着はポインタのときだけ）
  await openOn(['n014']);
  await win.locator('#popTrV').focus();
  for (let i = 0; i < 3; i++) await win.keyboard.press('ArrowRight');
  await settle();
  expect(await win.locator('#popTrV').inputValue()).toBe('53');
  expect(await win.locator('#popTrVV').textContent()).toBe('53');
  await win.waitForFunction(() => (window.__app.S.vd.edits || [])
    .some((e) => e.kind === 'transition' && Math.abs(e.params.value - 0.53) < 1e-9));
  await win.keyboard.press('Escape');
  // 値のばらばらな境目（n013｜n014 と n014｜n015 は 0.53、n015｜n016 は自動）→「混在」
  await openOn(['n014', 'n015']);
  expect(await win.locator('#popTrVV').textContent()).toBe('混在');
  await win.keyboard.press('Escape');
  await undoAll();
  // 当てている途中で閉じて別のノートで開き直しても、前のポップアップの編集は残る
  await win.evaluate(async () => {
    const A = window.__app;
    const fire = (val) => {
      const el = document.querySelector('#popTrV');
      el.value = String(val);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    A.S.sel = ['n011']; A.openTr(200, 150); fire(20);
    document.querySelector('#mock').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    A.S.sel = ['n015']; A.openTr(200, 150); fire(80);
  });
  await win.waitForFunction(() => (window.__app.S.vd.edits || [])
    .filter((e) => e.kind === 'transition').length >= 4, null, { timeout: 60000 });
  await settle();
  const tr = (await edits()).filter((e) => e.kind === 'transition');
  expect(tr.filter((e) => Math.abs(e.params.value - 0.2) < 1e-9).length).toBe(2);
  expect(tr.filter((e) => Math.abs(e.params.value - 0.8) < 1e-9).length).toBe(2);
  await win.keyboard.press('Escape');
  await undoAll();
  // 1 / 2 / 3 でツールを替えたら右クリックのメニューは閉じる
  const b = await blob('n014').boundingBox();
  await win.mouse.click(b.x + b.width / 2, b.y + b.height / 2, { button: 'right' });
  await expect(win.locator('#menu')).toBeVisible();
  await win.keyboard.press('2');
  await expect(win.locator('#menu')).toBeHidden();
  await win.keyboard.press('1');
});

// ---------------------------------------------------------------- 確定待ちの間のドラッグ（2026-09-23）
test('(9) 確定待ちの間に離したドラッグは後から当たる・自分の編集を外部の変更と取り違えない', async () => {
  await undoAll();
  await showRange(2.2, 3.5);
  await win.locator('#mock').focus();
  await win.keyboard.press('1');
  const pitchOf = async (id) => (await win.evaluate(() => window.__app.notes())).find((n) => n.id === id).edited;
  const dragUp = async (id, dy) => {
    const b = await blob(id).boundingBox();
    await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await win.mouse.down();
    await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - dy, { steps: 6 });
    await win.mouse.up();
  };

  // (a) 前の編集（Ctrl+Z など）を当てている間に掴んで離した: 黙って捨てず、終わってから当たる
  const p15 = await pitchOf('n015');
  await win.evaluate(() => { window.__app.S.busy = true; });
  await dragUp('n015', 30);
  expect(await win.evaluate(() => window.__app.S.queued)).toBe(1);
  expect(await win.evaluate(() => window.__app.status())).toContain('続けて当てる');
  expect((await edits()).filter((e) => e.kind === 'pitch_shift')).toHaveLength(0);
  await win.evaluate(() => { window.__app.S.busy = false; });
  await settle();
  expect(await pitchOf('n015')).toBeGreaterThan(p15 + 0.2);

  // (b) 端のドラッグ（計画の確定待ち）の直後に、待たずに別のノートのピッチを動かす: 両方当たる
  const changesets = () => win.evaluate(() => window.__app.S.vd.history.changesets
    .filter((c) => !c.undone).length);
  const n0 = await changesets();
  const p14 = await pitchOf('n014');
  const h = win.locator('#roll rect[data-note="n014"][data-edge="end"]').first();
  const hb = await h.boundingBox();
  await win.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
  await win.mouse.down();
  await win.mouse.move(hb.x + hb.width / 2 - 12, hb.y + hb.height / 2, { steps: 4 });
  await win.mouse.up();
  expect(await win.evaluate(() => window.__app.idle())).toBe(false);   // まだ確定していない
  await dragUp('n014', 30);
  await settle();
  expect(await changesets()).toBe(n0 + 2);                // 端（計画）とピッチの 2 操作
  const es = await edits();
  expect(es.some((e) => e.kind === 'pitch_shift' && e.target.note_id === 'n014')).toBe(true);
  expect(await pitchOf('n014')).toBeGreaterThan(p14 + 0.2);

  // (c) 自分の apply_plan を「外部の変更」として読み直さない
  //    （読み直すと取り消しのまとまりが消え、ドラッグ中ならピッチの差分が消えていた）
  const hb2 = await h.boundingBox();
  await win.mouse.move(hb2.x + hb2.width / 2, hb2.y + hb2.height / 2);
  await win.mouse.down();
  await win.mouse.move(hb2.x + hb2.width / 2 + 8, hb2.y + hb2.height / 2, { steps: 4 });
  await win.mouse.up();
  await settle();                           // 最後に当てたのが apply_plan（以前は監視に「外部の変更」と見なされた）
  // 取り消しの履歴はエンジンにあるので読み直しでは消えないが、読み直しそのものが起きないことを見る
  const hist = await win.evaluate(() => window.__app.hist());
  expect(hist.undo.label).toBe('ノートの長さ');
  await win.waitForTimeout(600);            // main の監視は 120 ms 待ってから比べる
  expect(await win.evaluate(() => window.__app.hist())).toEqual(hist);
  expect(await win.evaluate(() => window.__app.status())).not.toContain('外部の変更');
  await undoAll();
  expect(errors).toEqual([]);
});

test('(10) 確定待ちの間: 同じノートの続けてのドラッグ = 見た目、Ctrl+Z は追い越さない、外部の変更は読み落とさない', async () => {
  await undoAll();
  await showRange(2.2, 3.5);
  await win.locator('#mock').focus();
  await win.keyboard.press('1');
  const pitchOf = async (id) => (await win.evaluate(() => window.__app.notes())).find((n) => n.id === id).edited;
  const shown = async (id) => (await win.evaluate(() => window.__app.shapes())).find((n) => n.id === id).pitch;
  const dragUp = async (id, dy) => {
    const b = await blob(id).boundingBox();
    await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await win.mouse.down();
    await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - dy, { steps: 6 });
    await win.mouse.up();
  };
  // 同じノートを 2 回続けて上げる（1 回目がまだ当たっていない間に 2 回目）: 当たった結果 = 離したときの見た目
  const p0 = await pitchOf('n015');
  await win.evaluate(() => { window.__app.S.busy = true; });
  await dragUp('n015', 20);
  await dragUp('n015', 20);
  const seen = await shown('n015');
  expect(seen).toBeGreaterThan(p0 + 0.3);
  await win.evaluate(() => { window.__app.S.busy = false; });
  await settle();
  expect(Math.abs((await pitchOf('n015')) - seen)).toBeLessThan(TOL_ST);

  // 確定待ちの間に Ctrl+Z: 黙って捨てず、追い越さず、その時点の最後の操作（いま離したドラッグ）を取り消す
  const p1 = await pitchOf('n015');
  await win.evaluate(() => { window.__app.S.busy = true; });
  await dragUp('n014', 30);
  await win.keyboard.press('Control+z');
  await win.evaluate(() => { window.__app.S.busy = false; });
  await settle();
  expect(await pitchOf('n015')).toBeCloseTo(p1, 4);          // 前の操作は残る
  const p14 = await win.evaluate(() => window.__app.notes().find((n) => n.id === 'n014'));
  expect(p14.edited).toBeCloseTo(p14.pitch, 4);             // n014 のドラッグは取り消された

  // 外部の変更の直後（監視の 120 ms の間）に、読むだけのツールを呼んでも読み落とさない
  const dir = await win.evaluate(() => window.__app.S.projectDir);
  const jsonPath = path.join(dir, 'project.json');
  const raw = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  for (const c of raw.changesets) c.undone = true;
  raw.updated_at = new Date().toISOString();
  fs.writeFileSync(jsonPath, JSON.stringify(raw, null, 2), 'utf8');
  await win.evaluate(() => window.api.call('list_notes', {}));
  await win.waitForFunction(() => window.__app.S.vd.edits.length === 0, null, { timeout: 10000 });
  expect(await win.evaluate(() => window.__app.status())).toContain('外部の変更');
  expect(errors).toEqual([]);
});
