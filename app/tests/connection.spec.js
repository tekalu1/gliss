// 接続 / 切り離し（リップルなしの編集）と「ドラッグ中の見た目 = 離した後の結果」。
//
//   (1) 接続された右端のドラッグ: 隣の頭が一緒に動き、それ以外のノートは動かない
//   (2) Alt+ドラッグ: 接続を切って自分だけ縮む（隙間ができる）。Ctrl+Z で接続ごと戻る
//   (3) 吸着: 切り離された端を隣にぶつかるまで伸ばすと接続になる
//   (4) 切り離された端（息の手前）: 自分だけ伸び縮みし、隣は動かない
//   (5) ノート本体の横ドラッグ: 両隣（接続）が伸び縮みして吸収、その外は動かない
//   (6) ガイドに合わせる: 対応するガイドノートだけ通常の濃さ、100% のプレビュー = 確定後、
//       100% でガイドの中心・境界に重なる
//   (7) 確定の途中でもう一度離しても、最後の値で当たる
//   (8) 押している間に OS のマウスが割り込み、離したことが届かなくても、最後に見せた位置で当たる
//       （端のドラッグ・テンポのドラッグ。離した後はマウスについて行かない）
//
// 比べる許容誤差: 時刻 0.5 ms（view data は 0.1 ms に丸めてある）、音程 1 セント。
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
const PROJECT = path.join(REPO, 'projects', '_test-connection');
// 設定（最近使ったファイル・前回の表示範囲・ウィンドウの位置）も毎回まっさらにする。人が使った
// ときの表示範囲を引き継ぐと、狙うノートが画面の外に出てドラッグが外れる（人の設定も汚さない）。
const USERDATA = `${PROJECT}-userdata`;
const TOL_SEC = 0.0005;
const TOL_ST = 0.01;

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
  win.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await showRange(0.3, 1.7);
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
const shapes = () => win.evaluate(() => window.__app.shapes());
const byId = (xs) => new Map(xs.map((x) => [x.id, x]));
const edits = () => win.evaluate(() => window.__app.S.vd.edits);
const conns = async () => byId(await win.evaluate(() => window.__app.connections()));

/** ドラッグ中（離す直前）と離した後の形が同じこと。 */
function expectSame(preview, done, label) {
  const d = byId(done);
  for (const p of preview) {
    const q = d.get(p.id);
    expect(Math.abs(q.s - p.s), `${label} ${p.id} 頭`).toBeLessThan(TOL_SEC);
    expect(Math.abs(q.e - p.e), `${label} ${p.id} 尻`).toBeLessThan(TOL_SEC);
    expect(Math.abs(q.pitch - p.pitch), `${label} ${p.id} 音程`).toBeLessThan(TOL_ST);
  }
}

/** 指定したノート以外は動いていないこと。 */
function expectOthersStill(before, after, allowed, label) {
  const a = byId(after);
  for (const b of before) {
    if (allowed.includes(b.id)) continue;
    const q = a.get(b.id);
    expect(Math.abs(q.s - b.s), `${label}: ${b.id} の頭が動いた`).toBeLessThan(1e-4);
    expect(Math.abs(q.e - b.e), `${label}: ${b.id} の尻が動いた`).toBeLessThan(1e-4);
  }
}

/** ハンドルをつまんで dx px 動かし、離す直前の形を返す。 */
async function drag(locator, dx, { alt = false, shot = null, dy = 0 } = {}) {
  const box = await locator.boundingBox();
  const x = box.x + box.width / 2; const y = box.y + box.height / 2;
  await win.mouse.move(x, y);
  if (alt) await win.keyboard.down('Alt');
  await win.mouse.down();
  await win.mouse.move(x + dx, y + dy, { steps: 10 });
  // 計画が届いてから（エンジンが x の範囲を返してから）形を取る
  await win.waitForFunction(() => window.__app.plan() !== null, null, { timeout: 30000 });
  await win.mouse.move(x + dx + 0.01, y + dy);
  const preview = await shapes();
  const plan = await win.evaluate(() => window.__app.plan());
  if (shot) await win.screenshot({ path: path.join(APP, 'screenshots', shot) });
  await win.mouse.up();
  if (alt) await win.keyboard.up('Alt');
  await settle();
  return { preview, plan };
}
const edge = (id, which) => win.locator(`#roll rect[data-note="${id}"][data-edge="${which}"]`).first();
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();

test('(1) 接続された右端: 隣の頭が一緒に動き、他は動かない', async () => {
  const c = await conns();
  expect(c.get('n005').next).toBe(true);                 // 既定: 隙間なし = 接続
  expect(c.get('n007').next).toBe(false);                // 息を挟む = 切り離し
  const before = await shapes();
  fs.mkdirSync(path.join(APP, 'screenshots'), { recursive: true });
  const { preview, plan } = await drag(edge('n005', 'end'), 30,
    { shot: 'screenshot-connected-drag.png' });
  expect(plan.kind).toBe('edge');
  const after = await shapes();
  expectSame(preview, after, '接続の右端');
  const a = byId(after); const b = byId(before);
  expect(a.get('n005').e).toBeGreaterThan(b.get('n005').e + 0.01);
  expect(Math.abs(a.get('n006').s - a.get('n005').e)).toBeLessThan(TOL_SEC);   // 境目を共有
  expect(Math.abs(a.get('n006').e - b.get('n006').e)).toBeLessThan(1e-4);     // 隣の尻は動かない
  expectOthersStill(before, after, ['n005', 'n006'], '接続の右端');
});

test('(2) Alt+ドラッグで切り離して自分だけ縮む。Ctrl+Z で接続ごと戻る', async () => {
  const before = await shapes();
  const { preview } = await drag(edge('n005', 'end'), -40, { alt: true });
  const after = await shapes();
  expectSame(preview, after, 'Alt');
  const a = byId(after); const b = byId(before);
  expect(a.get('n005').e).toBeLessThan(b.get('n005').e - 0.01);
  expect(Math.abs(a.get('n006').s - b.get('n006').s)).toBeLessThan(1e-4);     // 隣は動かない
  expect(a.get('n006').s - a.get('n005').e).toBeGreaterThan(0.01);            // 隙間ができた
  expect((await conns()).get('n005').next).toBe(false);
  expect((await edits()).some((e) => e.kind === 'connection')).toBe(true);
  expectOthersStill(before, after, ['n005'], 'Alt');
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-detached.png') });

  // Ctrl+Z で位置も接続も戻る
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  const undone = byId(await shapes());
  expect(Math.abs(undone.get('n005').e - b.get('n005').e)).toBeLessThan(1e-4);
  expect((await conns()).get('n005').next).toBe(true);
  await win.keyboard.press('Control+y');
  await settle();
  expect((await conns()).get('n005').next).toBe(false);
});

test('(3) 切り離された端を隣にぶつかるまで伸ばすと接続になる', async () => {
  const before = await shapes();
  const b = byId(before);
  const gapPx = await win.evaluate(([e, s]) => {
    const r = document.querySelector('#roll').getBoundingClientRect();
    return (s - e) / window.__app.S.view.span * (r.width - 44);
  }, [b.get('n005').e, b.get('n006').s]);
  const { preview, plan } = await drag(edge('n005', 'end'), gapPx + 30);   // 行き過ぎても止まる
  expect(plan.snap).not.toBeNull();
  const after = await shapes();
  expectSame(preview, after, '吸着');
  const a = byId(after);
  expect(Math.abs(a.get('n005').e - a.get('n006').s)).toBeLessThan(TOL_SEC);
  expect(Math.abs(a.get('n006').s - b.get('n006').s)).toBeLessThan(1e-4);
  expect((await conns()).get('n005').next).toBe(true);
  expectOthersStill(before, after, ['n005'], '吸着');
});

test('(4) 切り離された端（息の手前）は自分だけ伸び縮みする', async () => {
  const before = await shapes();
  const { preview } = await drag(edge('n007', 'end'), -25);
  const after = await shapes();
  expectSame(preview, after, '切り離しの右端');
  const a = byId(after); const b = byId(before);
  expect(a.get('n007').e).toBeLessThan(b.get('n007').e - 0.01);
  expectOthersStill(before, after, ['n007'], '切り離しの右端');
});

test('(5) ノート本体の横ドラッグ: 両隣が吸収し、その外は動かない', async () => {
  await showRange(2.1, 3.6);
  const before = await shapes();
  await win.keyboard.press('Escape');                 // 選択を外す（左上は再生ボタン）
  expect(await win.evaluate(() => ({ selected: window.__app.S.sel.length,
    playing: window.__app.S.playing }))).toEqual({ selected: 0, playing: false });
  const { preview, plan } = await drag(blob('n012'), 20);
  expect(plan.kind).toBe('move');
  const after = await shapes();
  expectSame(preview, after, '移動');
  const a = byId(after); const b = byId(before);
  const d = a.get('n012').s - b.get('n012').s;
  expect(d).toBeGreaterThan(0.005);
  expect(Math.abs((a.get('n012').e - b.get('n012').e) - d)).toBeLessThan(TOL_SEC);   // 長さそのまま
  expect(Math.abs(a.get('n011').e - a.get('n012').s)).toBeLessThan(TOL_SEC);
  expect(Math.abs(a.get('n013').s - a.get('n012').e)).toBeLessThan(TOL_SEC);
  expectOthersStill(before, after, ['n011', 'n012', 'n013'], '移動');
  await win.evaluate(async () => {
    for (let i = 0; i < 30; i += 1) {
      const r = await window.api.call('undo', {});
      if (!r || r.ok === false) break;
    }
    await window.__app.refresh();
  });
  expect((await edits()).length).toBe(0);
});

test('(6) ガイドに合わせる: 対応だけ濃く、100% のプレビュー = 確定後 = ガイドの位置', async () => {
  await showRange(0.2, 3.7);
  // 1 ノートだけ選ぶと、対応するガイドノートだけ通常の濃さになる
  await blob('n005').click();
  await win.locator('#bMacro').click();
  await win.waitForFunction(() => window.__app.plan()?.kind === 'guide', null, { timeout: 60000 });
  await expect(win.locator('#popPitchShape')).toBeChecked();
  const sourceCurve = await win.evaluate(() => window.__app.editedCurve());
  const halfHz = await win.evaluate(() => {
    const el = document.querySelector('#popPitch');
    el.value = '50'; el.dispatchEvent(new Event('input', { bubbles: true }));
    const S = window.__app.S;
    const row = S.plan.data.pitch_curve.find(([, h0, h1, w]) => w > 0.99 && Math.abs(h1 - h0) > 20);
    if (!row) return null;
    const [t, h0, h1, w] = row;
    const i = Math.round((t - S.vd.f0.t0_sec) / S.vd.f0.hop_sec);
    const actual = window.__app.editedCurve()[i];
    const baseline = S.vd.f0.take_edited_midi[i];
    const targetHz = h0 + 0.5 * w * (h1 - h0);
    return { actual, expected: baseline + 12 * Math.log2(targetHz / h0),
      geometric: baseline + 0.5 * w * 12 * Math.log2(h1 / h0) };
  });
  expect(halfHz).not.toBeNull();
  expect(Math.abs(halfHz.actual - halfHz.expected)).toBeLessThan(0.003);
  expect(Math.abs(halfHz.actual - halfHz.geometric)).toBeGreaterThan(0.02);
  await win.evaluate(() => {
    const el = document.querySelector('#popPitch');
    el.value = '70'; el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const shapeCurve = await win.evaluate(() => window.__app.editedCurve());
  const shapeColor = (await shapes()).find((n) => n.id === 'n005').line;
  expect(shapeCurve.some((v, i) => v != null && sourceCurve[i] != null && Math.abs(v - sourceCurve[i]) > 0.05)).toBe(true);
  await win.screenshot({ path: path.join(REPO, 'scratchpad', 'issue46-guide-shape.png') });
  await win.evaluate(() => document.querySelector('#popPitch').dispatchEvent(new Event('change', { bubbles: true })));
  await settle();
  const committedCurve = await win.evaluate(() => window.__app.editedCurve());
  expect((await shapes()).find((n) => n.id === 'n005').line).toBe(shapeColor);
  for (let i = 0; i < shapeCurve.length; i++) {
    if (shapeCurve[i] != null && committedCurve[i] != null)
      expect(Math.abs(shapeCurve[i] - committedCurve[i]), `形のプレビュー ${i}`).toBeLessThan(0.005);
  }
  await win.locator('#popPitchShape').uncheck();
  await settle();
  expect(await win.evaluate(() => window.__app.S.vd.edits.some((e) => e.kind === 'pitch_shift'))).toBe(true);
  expect(await win.evaluate(() => window.__app.S.vd.edits.some((e) => e.kind === 'pitch_draw'))).toBe(false);
  const afterToggleBase = await win.evaluate(() => window.__app.editedCurve());
  await win.evaluate(() => {
    const el = document.querySelector('#popPitch');
    el.value = '85'; el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const afterTogglePreview = await win.evaluate(() => window.__app.editedCurve());
  expect(afterTogglePreview.some((v, i) => v != null && afterToggleBase[i] != null
    && Math.abs(v - afterToggleBase[i]) > 0.05)).toBe(true);
  await win.evaluate(() => document.querySelector('#popPitch').dispatchEvent(new Event('change', { bubbles: true })));
  await settle();
  const afterToggleCommit = await win.evaluate(() => window.__app.editedCurve());
  for (let i = 0; i < afterTogglePreview.length; i++) {
    if (afterTogglePreview[i] != null && afterToggleCommit[i] != null)
      expect(Math.abs(afterTogglePreview[i] - afterToggleCommit[i]), `OFF 後のスライダー ${i}`).toBeLessThan(0.005);
  }
  await win.locator('#popPitchShape').check();
  await settle();
  expect(await win.evaluate(() => window.__app.S.vd.edits.some((e) => e.kind === 'pitch_draw'))).toBe(true);
  expect(await win.evaluate(() => window.__app.S.vd.edits.some((e) => e.kind === 'pitch_shift'))).toBe(false);
  const backOnBase = await win.evaluate(() => window.__app.editedCurve());
  await win.evaluate(() => {
    const el = document.querySelector('#popPitch');
    el.value = '55'; el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const backOnPreview = await win.evaluate(() => window.__app.editedCurve());
  expect(backOnPreview.some((v, i) => v != null && backOnBase[i] != null
    && Math.abs(v - backOnBase[i]) > 0.05)).toBe(true);
  await win.evaluate(() => document.querySelector('#popPitch').dispatchEvent(new Event('change', { bubbles: true })));
  await settle();
  const backOnCommit = await win.evaluate(() => window.__app.editedCurve());
  for (let i = 0; i < backOnPreview.length; i++) {
    if (backOnPreview[i] != null && backOnCommit[i] != null)
      expect(Math.abs(backOnPreview[i] - backOnCommit[i]), `ON 後のスライダー ${i}`).toBeLessThan(0.005);
  }
  await win.keyboard.press('Escape');
  await win.keyboard.press('Control+z');
  await settle();
  await win.locator('#bMacro').click();
  await expect(win.locator('#popPitchShape')).toBeChecked();
  // 計画が届いてから（スクリーンショットに対応の線・ステータス行が出る）
  await win.waitForFunction(() => window.__app.S.plan?.data?.kind === 'guide', null, { timeout: 60000 });
  await win.evaluate(() => {
    for (const id of ['#popPitch', '#popTime']) {
      const el = document.querySelector(id);
      el.value = '100';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-guide-select.png') });
  await win.keyboard.press('Escape');
  await win.locator('#bMacro').click();
  await win.locator('#popPitchShape').uncheck();
  await win.waitForFunction(() => window.__app.S.plan?.data?.params?.match_pitch_shape === false, null, { timeout: 60000 });
  const one = await win.evaluate(() => window.__app.plan());
  const ops = await win.evaluate(() => window.__app.guideOpacity());
  const lit = ops.filter((o) => o.op > 0.1).map((o) => o.id);
  expect(lit.length).toBeGreaterThan(0);
  expect(new Set(lit)).toEqual(new Set(one.guides));
  expect(ops.some((o) => o.op < 0.1)).toBe(true);        // それ以外は薄い
  await win.evaluate(() => {                              // 100% のプレビュー（離さない）
    for (const id of ['#popPitch', '#popTime']) {
      const el = document.querySelector(id);
      el.value = '100';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  expect((await edits()).length).toBe(0);
  await win.keyboard.press('Escape');
  await win.keyboard.press('Escape');                     // 選択も外す（全体が対象）

  const before = await shapes();
  await win.locator('#bMacro').click();
  await expect(win.locator('#popPitchShape')).toBeChecked();
  // 計画が届いてから（スクリーンショットに対応の線・ステータス行が出る）
  await win.waitForFunction(() => window.__app.S.plan?.data?.kind === 'guide', null, { timeout: 60000 });
  await win.evaluate(() => {
    for (const id of ['#popPitch', '#popTime']) {
      const el = document.querySelector(id);
      el.value = '100';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-guide-plan.png') });
  await win.keyboard.press('Escape');
  await win.locator('#bMacro').click();
  await win.locator('#popPitchShape').uncheck();
  await win.waitForFunction(() => window.__app.S.plan?.data?.params?.match_pitch_shape === false, null, { timeout: 60000 });
  const plan = await win.evaluate(() => window.__app.plan());
  await win.evaluate(() => {
    for (const id of ['#popPitch', '#popTime']) {
      const el = document.querySelector(id);
      el.value = '100';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  const preview = await shapes();
  // プレビュー（計画の式）での発音の頭の行き先 = ガイドの頭 + 全体のずれ
  const rch = plan.timing.filter((e) => e.reached);
  const pv = await win.evaluate((ts) => ts.map((t) => window.__app.planWarp(t)),
    rch.map((e) => e.cur_sec));
  rch.forEach((e, i) => {
    expect(Math.abs(pv[i] - e.target_sec), `プレビュー ${e.take}`).toBeLessThan(0.001);
  });
  await win.evaluate(() => {
    document.querySelector('#popTime').dispatchEvent(new Event('change', { bubbles: true }));
  });
  await settle();
  const after = await shapes();
  expectSame(preview, after, 'ガイド 100%');

  // 音程: 対応するノートは計画どおり（= ガイドの中心）へ
  const b = byId(before); const a = byId(after);
  for (const [id, cents] of Object.entries(plan.pitchPlan)) {
    expect(Math.abs(a.get(id).pitch - (b.get(id).pitch + cents / 100))).toBeLessThan(TOL_ST);
  }
  // タイミング: 発音の頭が「ガイドの頭 + 全体のずれ」（画面に描いているガイドの位置）へ（issue #12）。
  // プレビュー（計画の式）でも確定後でも同じ位置
  const reached = plan.timing.filter((e) => e.reached);
  expect(reached.length).toBeGreaterThan(0);
  const got = await win.evaluate((ts) => ts.map((t) => window.__app.toEdited(t)),
    reached.map((e) => e.take_sec));
  reached.forEach((e, i) => {
    expect(Math.abs(got[i] - e.target_sec), `${e.take} ${e.take_sec}`).toBeLessThan(0.001);
  });
  // 画面のガイドは「ガイドの時刻 + 全体のずれ」に描く（C ↔ C2 は所ごとのずれが無い）
  const vd = await win.evaluate(() => ({
    g: window.__app.S.vd.guide_notes, basis: window.__app.S.vd.guide_basis,
  }));
  expect(vd.basis.basis).toBe('guide_time');
  for (const x of vd.g) {
    expect(Math.abs(x.start_sec - x.guide_start_sec - vd.basis.offset_ms / 1000), x.id)
      .toBeLessThan(0.001);
  }
  await win.keyboard.press('Escape');
});

test('(7) 確定の途中でもう一度離しても、最後の値で当たる', async () => {
  await win.evaluate(async () => {
    for (let i = 0; i < 30; i += 1) {
      const r = await window.api.call('undo', {});
      if (!r || r.ok === false) break;
    }
    await window.__app.refresh();
  });
  await win.locator('#bMacro').click();
  await win.waitForFunction(() => window.__app.plan()?.kind === 'guide', null, { timeout: 60000 });
  const set = (v) => win.evaluate((val) => {
    const el = document.querySelector('#popPitch');
    el.value = String(val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, v);
  await set(50);
  await set(80);                                          // 50% の確定が終わる前に離す
  const preview = await shapes();
  await win.waitForFunction(() => {
    const p = window.__app.plan();
    return p && p.pitch0 === 0.8 && !window.__app.S.busy;
  }, null, { timeout: 60000 });
  expectSame(preview, await shapes(), '続けて離す');
  const cs = await win.evaluate(() => window.__app.S.vd.history.changesets.filter((c) => !c.undone));
  expect(cs.length).toBe(1);                              // ポップアップの変更は 1 つだけ残る
  await win.keyboard.press('Escape');
});

/** ボタンを押していない OS のマウスの動き（Playwright の CDP ではなく、実マウスと同じ経路）。 */
const osMouseMove = (x, y) => app.evaluate(({ BrowserWindow }, [px, py]) => {
  BrowserWindow.getAllWindows()[0].webContents.sendInputEvent({ type: 'mouseMove', x: px, y: py });
}, [Math.round(x), Math.round(y)]);

test('(8) 押している間に OS のマウスが割り込んで離したことが届かなくても、最後に見せた位置で当たる', async () => {
  // 押している間に実マウス（ボタンを押していない）が動くと、Chromium はポインタのキャプチャを外し、
  // ボタンを押していない move を送ってくる（pointerup は来ないことがある）。以前はそれをドラッグの続きとして扱い、
  // 端がマウスについて行って、見た目と違う位置で当たっていた（(1) が全体実行でときどき落ちた原因）
  await win.evaluate(async () => {
    for (let i = 0; i < 30; i += 1) {
      const r = await window.api.call('undo', {});
      if (!r || r.ok === false) break;
    }
    await window.__app.refresh();
  });
  await showRange(0.3, 1.7);
  const before = await shapes();
  const box = await edge('n005', 'end').boundingBox();
  const x = box.x + box.width / 2; const y = box.y + box.height / 2;
  await win.mouse.move(x, y);
  await win.mouse.down();
  await win.mouse.move(x + 30, y, { steps: 10 });
  await win.waitForFunction(() => window.__app.plan() !== null, null, { timeout: 30000 });
  await win.mouse.move(x + 30.01, y);
  const preview = await shapes();
  await osMouseMove(x + 200, y);
  await win.waitForFunction(() => !window.__app.S.drag, null, { timeout: 10000 });   // そこで離したことにする
  await osMouseMove(x + 300, y);                          // 離した後に動かしても、もうついて行かない
  await settle();
  const after = await shapes();
  expectSame(preview, after, '割り込み');
  const a = byId(after); const b = byId(before);
  expect(a.get('n005').e).toBeGreaterThan(b.get('n005').e + 0.01);
  expectOthersStill(before, after, ['n005', 'n006'], '割り込み');
  await win.mouse.up();                                   // 遅れて届いた pointerup は何もしない
  await settle();
  expectSame(preview, await shapes(), '遅れた pointerup');

  // テンポ（BPM）のドラッグも同じ。キャプチャが外れた後の move は #tBpm に来ないので、以前はドラッグが残り、
  // 見せていた値は当たらず、あとでボタンを押さずに上を通っただけでテンポが変わった
  const tb = await win.locator('#tBpm').boundingBox();
  const bx = tb.x + tb.width / 2; const by = tb.y + tb.height / 2;
  await win.mouse.move(bx, by);
  await win.mouse.down();
  for (let k = 1; k <= 5; k++) await win.mouse.move(bx, by - k * 8);
  const shown = (await win.evaluate(() => window.__app.tempoText())).bpm;
  expect(shown).not.toBe('—');
  await osMouseMove(bx + 300, by);
  await settle();
  const committed = () => win.evaluate(() => window.__app.S.session?.tempo?.bpm ?? null);   // 当たった値
  await expect.poll(committed).toBe(Number(shown));
  await win.mouse.up();
  await osMouseMove(bx, by + 4);                          // ボタンを押さずに上を通る
  await osMouseMove(bx, by - 20);
  await settle();
  expect((await win.evaluate(() => window.__app.tempoText())).bpm).toBe(shown);
  expect(await committed()).toBe(Number(shown));
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
