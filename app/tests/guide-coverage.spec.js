// ガイドとの対応の表示（issue #53。案 C）。
//
//   (1) 「ガイドに合わせる」を開いている間だけ、対象ノートの確かな対応を細い灰色線、確かな対応の無い
//       ノートを灰色の破線の丸で描く。閉じると消える。1 対多は組の頭どうし・尻どうしの 2 本（範囲）。
//       色は既存の灰色（元の長さの線）だけ
//   (2) ホバーで「音程だけ対応／タイミングも対応（基準点・補間）／目標に届く」と、対応が無い理由
//   (3) 拡大率に応じて間引く（縮小すると線が減り、近すぎる線は描かない）
//   (4) 補間で動くノートも、スライダーのプレビュー = 離した後。取り消すと元どおり
//   (5) 選択範囲だけ: 選んだノートの対応だけを描く
//   (6) 確かな頭の組が 1 つも無いガイド（別の歌）: ステータス行に「このガイドとはタイミングを合わせられない」、
//       タイミング 100% でも動かない
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2', 'G');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const GUIDE = M.clip('C2');
const OTHER = M.clip('G');          // 別の歌（確かな頭の組が 1 つも無い）
const PROJECT = path.join(REPO, 'projects', '_test-guide-coverage');
const USERDATA = `${PROJECT}-userdata`;
const GRAY = '#a4a4aa';                                    // COLORS.WAS（元の長さの線と同じ灰色）
const TOL_SEC = 0.0005;

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

async function launch(guide, dir) {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(dir, { recursive: true, force: true });
  fs.rmSync(`${dir}-userdata`, { recursive: true, force: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', guide, '--project-dir', dir,
      '--user-data-dir', `${dir}-userdata`, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
}

test.beforeAll(async () => {
  await launch(GUIDE, PROJECT);
  await showRange(0.2, 3.6);
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}
async function showRange(a, b) {
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [a, b]);
}
const shapes = () => win.evaluate(() => window.__app.shapes());
const byId = (xs) => new Map(xs.map((x) => [x.id, x]));
const corr = () => win.evaluate(() => window.__app.guideCorr());
const plan = () => win.evaluate(() => window.__app.plan());
const ends2 = (d) => { const m = /^M([-\d.]+) [-\d.]+L([-\d.]+)/.exec(d); return [+m[1], +m[2]]; };

async function openGuide() {
  await win.locator('#bMacro').click();
  await win.waitForFunction(() => window.__app.S.plan?.data?.kind === 'guide'
    && window.__app.S.plan.data.params?.match_pitch_shape === true, null, { timeout: 120000 });
  await win.evaluate(() => window.__app.render());
}
async function closeGuide() {
  await win.keyboard.press('Escape');
  await win.waitForFunction(() => document.querySelector('#pop').hidden, null, { timeout: 30000 });
}
async function setTime(v, commit) {
  await win.evaluate(([val, done]) => {
    const el = document.querySelector('#popTime');
    el.value = String(val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    if (done) el.dispatchEvent(new Event('change', { bubbles: true }));
  }, [v, commit]);
}

test('(1) 開いている間だけ、確かな対応を灰色線、対応の無いノートを破線の丸で描く', async () => {
  await win.keyboard.press('Escape');                     // 選択を外す（全体が対象）
  expect((await corr()).lines).toHaveLength(0);
  await openGuide();
  const pl = await plan();
  const rows = pl.notes;
  expect(rows.length).toBeGreaterThan(5);
  const c = await corr();
  const confirmed = pl.pairs.filter((p) => p.confirmed);
  const multi = confirmed.filter((p) => p.take.length > 1 || p.guide.length > 1);
  // 確かな組ごとに線がある（1 対 1 は頭どうしの 1 本、1 対多は頭と尻の 2 本。組の尻と次の組の頭が
  // 同じ所なら 1 本にまとめる）。確かでない組には引かない
  const keys = (ps) => [...new Set(ps.map((p) => `${p.take[0]}|${p.guide[0]}`))].sort();
  expect([...new Set(c.lines.map((l) => l.key))].sort()).toEqual(keys(confirmed));
  expect(c.lines.length).toBeLessThanOrEqual(confirmed.length + multi.length);
  expect(multi.length).toBeGreaterThan(0);
  expect(c.lines.filter((l) => l.end)).toHaveLength(multi.length);
  for (const l of c.lines) {
    expect(l.stroke).toBe(GRAY);
    expect(l.width).toBe(1);
  }
  const unconfirmed = rows.filter((r) => !r.confirmed).map((r) => r.note).sort();
  expect(c.marks.map((m) => m.id).sort()).toEqual(unconfirmed);
  for (const m of c.marks) {
    expect(m.stroke).toBe(GRAY);
    expect(m.dash).toBeTruthy();
  }
  // ステータス行: 確かな対応とタイミングの数（基準点・補間）
  const st = await win.evaluate(() => window.__app.status());
  expect(st).toContain('ガイドとの対応');
  expect(st).toContain(`タイミング ${rows.filter((r) => r.timing).length}/${rows.length}`);
  expect(pl.info.timing_interp_notes).toBeGreaterThan(0);
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-guide-coverage.png') });
  await closeGuide();
  const after = await corr();
  expect(after.lines).toHaveLength(0);
  expect(after.marks).toHaveLength(0);
  // 閉じたらステータス行の数も消す（次に開いたとき、前の対象の数が残って見えない）
  expect(await win.evaluate(() => window.__app.status())).not.toContain('ガイドとの対応');
});

test('(2) ホバーで対応の種類と理由を出す', async () => {
  await openGuide();
  const rows = (await plan()).notes;
  const pick = [
    rows.find((r) => r.confirmed && r.timing === 'interp'),
    rows.find((r) => r.confirmed && r.timing === 'anchor'),
    rows.find((r) => !r.confirmed),
  ].filter(Boolean);
  expect(pick.length).toBeGreaterThanOrEqual(2);
  for (const r of pick) {
    await win.locator(`#roll rect[data-note="${r.note}"]:not([data-edge]):not([data-fade])`).hover();
    await win.waitForFunction((id) => document.querySelector(`#roll [data-corr-tip="${id}"]`), r.note);
    const lines = await win.evaluate((id) => window.__app.corrText(id), r.note);
    expect((await corr()).tip).toBe(lines.join(''));
    if (r.timing === 'interp' && r.pitch) expect(lines[0]).toContain('タイミングも対応（補間）');
    if (r.timing === 'anchor' && r.pitch) expect(lines[0]).toContain('タイミングも対応（基準点）');
    if (r.timing) expect(lines[0]).toContain(r.reached ? '目標に届く' : '目標に届かない');
    if (!r.confirmed) expect(lines.join('')).toContain(r.reason);
    if (!r.timing) expect(lines.join('')).toContain(r.timing_reason);
    if (r.guide.length) expect(lines.join('')).toContain(r.guide[0]);
  }
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-guide-coverage-tip.png') });
  await win.mouse.move(5, 5);
  await closeGuide();
});

test('(3) 拡大率に応じて間引く', async () => {
  await openGuide();
  const near = (await corr()).lines.length;
  await showRange(0, 80);                                   // 1 秒が十数 px まで縮める
  const far = (await corr()).lines;
  expect(far.length).toBeLessThan(near);
  expect(far.length).toBeGreaterThan(0);
  const ends = far.map((l) => ends2(l.d)).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  for (let i = 1; i < ends.length; i++) {
    const [a0, a1] = ends[i - 1]; const [b0, b1] = ends[i];
    expect(b0 - a0 >= 6 - 0.11 || Math.abs(b1 - a1) >= 6 - 0.11, `${ends[i - 1]} / ${ends[i]}`).toBe(true);
  }
  const marks = (await corr()).marks.map((m) => m.cx).sort((a, b) => a - b);
  for (let i = 1; i < marks.length; i++) expect(marks[i] - marks[i - 1]).toBeGreaterThanOrEqual(12);
  await showRange(0.2, 3.6);
  await closeGuide();
});

test('(4) 補間で動くノートも、プレビュー = 離した後。取り消すと元どおり', async () => {
  const before = await shapes();
  await openGuide();
  const rows = (await plan()).notes;
  const interp = rows.filter((r) => r.timing === 'interp').map((r) => r.note);
  expect(interp.length).toBeGreaterThan(0);
  await setTime(100, false);
  const preview = await shapes();
  const b = byId(before);
  // 補間のノートも動く（基準点の無いノートの頭）
  expect(interp.some((id) => Math.abs(byId(preview).get(id).s - b.get(id).s) > 0.001)).toBe(true);
  await setTime(100, true);
  await settle();
  const done = byId(await shapes());
  for (const p of preview) {
    const q = done.get(p.id);
    expect(Math.abs(q.s - p.s), `${p.id} の頭`).toBeLessThan(TOL_SEC);
    expect(Math.abs(q.e - p.e), `${p.id} の尻`).toBeLessThan(TOL_SEC);
  }
  await closeGuide();
  await win.keyboard.press('Control+z');
  await settle();
  const back = byId(await shapes());
  for (const p of before) {
    expect(Math.abs(back.get(p.id).s - p.s), `${p.id}`).toBeLessThan(TOL_SEC);
    expect(Math.abs(back.get(p.id).e - p.e), `${p.id}`).toBeLessThan(TOL_SEC);
  }
});

test('(5) 選択範囲のノートだけを描く', async () => {
  const all = await win.evaluate(() => window.__app.allNotes().map((n) => n.id));
  const sel = all.slice(0, 3);
  await win.evaluate((ids) => { window.__app.S.sel = ids; window.__app.render(); }, sel);
  await openGuide();
  await expect(win.locator('#popScope')).toHaveText('選択 3 ノート');
  const pl = await plan();
  expect(pl.notes.map((r) => r.note).sort()).toEqual([...sel].sort());
  const c = await corr();
  for (const m of c.marks) expect(sel).toContain(m.id);
  for (const l of c.lines) {
    const pr = pl.pairs.find((p) => `${p.take[0]}|${p.guide[0]}` === l.key);
    expect(pr && pr.take.some((id) => sel.includes(id))).toBe(true);
  }
  await closeGuide();
  await win.keyboard.press('Escape');
  expect(errors).toEqual([]);
});

test('(5b) ポップアップの数は音程ノートだけ数える（子音・息だけを選んだときは「全体」）', async () => {
  const ids = await win.evaluate(() => ({
    pitched: window.__app.allNotes().map((n) => n.id),
    block: window.__app.S.notes.find((n) => n.kind === 'unvoiced' || n.kind === 'breath')?.id,
  }));
  test.skip(!ids.block, '素材に子音・息が無い');
  const scope = async (sel) => {
    await win.evaluate((s) => { window.__app.S.sel = s; window.__app.render(); }, sel);
    await openGuide();
    const text = await win.locator('#popScope').textContent();
    await closeGuide();
    return text;
  };
  expect(await scope([ids.pitched[0], ids.pitched[1], ids.block])).toBe('選択 2 ノート');
  expect(await scope([ids.block])).toBe('全体');
});

test('(6) 確かな頭の組が無いガイド: 「タイミングを合わせられない」と出し、タイミングは動かない', async () => {
  await app.close();
  await launch(OTHER, `${PROJECT}-other`);
  await showRange(0.2, 3.6);
  await win.keyboard.press('Escape');
  const before = await shapes();
  await openGuide();
  await expect.poll(() => win.evaluate(() => window.__app.status()))
    .toContain('このガイドとはタイミングを合わせられない');
  const pl = await plan();
  expect(pl.info.timing_possible).toBe(false);
  expect(pl.notes.every((r) => r.timing === null)).toBe(true);
  expect((await corr()).lines).toHaveLength(0);
  await setTime(100, false);
  const preview = byId(await shapes());
  for (const p of before) expect(Math.abs(preview.get(p.id).s - p.s)).toBeLessThan(TOL_SEC);
  await win.evaluate(() => window.__app.render());
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-guide-coverage-none.png') });
  await setTime(0, false);
  await closeGuide();
  expect(errors).toEqual([]);
});
