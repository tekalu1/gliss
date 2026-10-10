// Electron アプリの通し確認。ダイアログは出さず、起動引数で素材を読ませる。
//
//   (1) 描画される（ノート数 > 0）
//   (2) blob 中央のドラッグ → shift_pitch → 再描画でピッチが変わる
//   (3) 境界のドラッグ → move_note / stretch
//   (4) 右クリック → オリジナルに戻す
//   (5) Ctrl+Z
//   (6) 「ガイドに合わせる」のスライダー
//   (7) 再生ボタンで再生ヘッドが進む
//   (8) 外部から project.json を書き換えると再描画される
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
// **プロジェクトを毎回作り直す。** 既定の置き場（素材のハッシュで決まる場所）を
// 共有すると、前回の実行で残った編集が「編集 1 件」の期待値を壊す。
const PROJECT = path.join(REPO, 'projects', '_test-app');
// 設定（最近使ったファイル・前回の表示範囲・ウィンドウの位置）も毎回まっさらにする。人が使った
// ときの表示範囲を引き継ぐと、狙うノートが画面の外に出てドラッグが外れる（人の設定も汚さない）。
const USERDATA = `${PROJECT}-userdata`;

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  // ELECTRON_RUN_AS_NODE が環境に残っていると Electron が素の Node として起動して
  // しまう（Playwright の --remote-debugging-port が弾かれる）ので、必ず外す。
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
});

test.afterAll(async () => {
  await app?.close();
});

const notes = () => win.evaluate(() => window.__app.notes());
const state = () => win.evaluate(() => ({
  sel: window.__app.S.sel,
  head: window.__app.S.head,
  busy: window.__app.S.busy,
  canUndo: window.__app.S.vd.history.can_undo,
  canRedo: window.__app.S.vd.history.can_redo,
  edits: window.__app.S.vd.edits.length,
  projectDir: window.__app.S.projectDir,
}));

async function settle() {
  // S.busy だけだと、離した後に計画の到着や前の編集を待っている間（まだ busy でない）を見落とす
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}

/** blob 本体（境界のつまみではない方）の矩形。 */
function blob(id) {
  return win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
}

test('(1) 起動して描画される', async () => {
  const ns = await notes();
  expect(ns.length).toBeGreaterThan(0);
  expect(await win.locator('#roll path').count()).toBeGreaterThan(1);
  expect(await win.locator('#roll rect[data-note]').count()).toBeGreaterThan(0);
  // 見た目（issue #1）: ノートごとの小さな波形。当たり判定の矩形は透明で枠を描かない、
  // ガイドは同じ形式でグレー、背景の大きな波形と息の斜線は無い
  const look = await win.evaluate(() => {
    const q = (s) => [...document.querySelectorAll(s)];
    return {
      blobs: q('#roll path[data-blob]').length,
      guides: q('#roll path[data-guide]').map((p) => p.getAttribute('fill')),
      noteRects: q('#roll rect[data-note]').map((r) => [r.getAttribute('fill'), r.getAttribute('stroke')]),
      hatch: q('#roll pattern, #roll [fill="url(#hatch)"]').length,
      pitched: window.__app.S.pitched.length,
    };
  });
  expect(look.blobs).toBe(look.pitched);
  expect(look.guides.length).toBeGreaterThan(0);
  expect(new Set(look.guides)).toEqual(new Set(['#5aa2ff']));
  for (const [fill, stroke] of look.noteRects) { expect(fill).toBe('transparent'); expect(stroke).toBeNull(); }
  expect(look.hatch).toBe(0);
  // 無音（C の中央 1.6〜2.2 秒は −52〜−56 dBFS）には何も描かない。息・子音など音のあるところだけ
  // ノートの区切り: 隣と接していても、blob の両端は細くなっている（枠の代わり）
  const shape = await win.evaluate(() => {
    const S = window.__app.S;
    const r = document.querySelector('#roll').getBoundingClientRect();
    const X = (t) => 44 + (t - S.view.t0) / S.view.span * (r.width - 44);
    const polys = (d) => d.split('Z').filter(Boolean).map((one) => [...one.matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)]
      .map((m) => [+m[1], +m[2]]));
    const silent = [];
    for (const p of document.querySelectorAll('#roll path[data-nopitch]')) {
      for (const pts of polys(p.getAttribute('d'))) {
        const xs = pts.map((q) => q[0]);
        if (Math.max(...xs) > X(1.7) && Math.min(...xs) < X(2.1)) silent.push(p.dataset.nopitch);
      }
    }
    // n005 の blob: 上の縁を行き、下の縁を戻る。端の太さ / いちばん太いところ
    const pts = polys(document.querySelector('#roll path[data-blob="n005"]').getAttribute('d'))[0];
    const n = pts.length / 2;
    const th = (k) => pts[pts.length - 1 - k][1] - pts[k][1];
    let mx = 0; for (let k = 0; k < n; k++) mx = Math.max(mx, th(k));
    return { silent, head: th(0) / mx, tail: th(n - 1) / mx };
  });
  expect(shape.silent).toEqual([]);
  expect(shape.head).toBeLessThan(0.35);
  expect(shape.tail).toBeLessThan(0.35);
  const st = await win.evaluate(() => window.__app.status());
  expect(st).toContain(path.basename(TAKE));
  fs.mkdirSync(path.join(APP, 'screenshots'), { recursive: true });
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot.png') });
});

test('(2) blob 中央のドラッグでピッチが変わる', async () => {
  const ns = await notes();
  const target = ns.find((n) => n.end - n.start > 0.15) || ns[0];
  const before = target.edited;
  const box = await blob(target.id).boundingBox();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - 40, { steps: 8 });
  // ドラッグ中（ツールチップが出ている）のスクリーンショット
  await expect(win.locator('#roll text').first()).toBeVisible();
  await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-drag.png') });
  await win.mouse.up();
  await settle();

  const after = (await notes()).find((n) => n.id === target.id);
  expect(after.edited).toBeGreaterThan(before + 0.3);   // 上に動かしたので高くなる
  expect(after.pitch).toBeCloseTo(target.pitch, 3);     // 編集前の値は動かない
  const st = await state();
  expect(st.edits).toBe(1);
  expect(st.canUndo).toBe(true);
});

test('(3) 境界のドラッグでタイミングが変わる', async () => {
  const ns = await notes();
  const target = ns.find((n) => n.end - n.start > 0.2 && n.cents === 0) || ns[1];
  const before = { s: target.editedStart, e: target.editedEnd };
  const handle = win.locator(`#roll rect[data-note="${target.id}"][data-edge="end"]`).first();
  const box = await handle.boundingBox();
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await win.mouse.down();
  await win.mouse.move(box.x + box.width / 2 + 24, box.y + box.height / 2, { steps: 8 });
  await win.mouse.up();
  await settle();

  const after = (await notes()).find((n) => n.id === target.id);
  expect(after.editedEnd - after.editedStart)
    .toBeGreaterThan((before.e - before.s) + 0.005);
  expect((await state()).edits).toBeGreaterThan(1);
});

test('(4) 右クリック → オリジナルに戻す', async () => {
  const editsBefore = (await state()).edits;
  expect(editsBefore).toBeGreaterThan(0);
  const ns = await notes();
  const edited = ns.find((n) => n.cents !== 0) || ns[0];
  const box = await blob(edited.id).boundingBox();
  await win.mouse.click(box.x + box.width / 2, box.y + box.height / 2, { button: 'right' });
  await expect(win.locator('#menu')).toBeVisible();
  await win.locator('#menu [data-cmd="reset-original"]').click();
  await settle();
  expect((await state()).edits).toBeLessThan(editsBefore);
});

test('(5) Ctrl+Z で戻る', async () => {
  const before = (await state()).edits;
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  const after = await state();
  expect(after.edits).toBeGreaterThan(before);   // 「戻す」を取り消したので編集が復活する
  expect(after.canRedo).toBe(true);
  // 元に戻せる状態に片付けておく
  await win.evaluate(async () => {
    for (let i = 0; i < 30; i += 1) {
      const r = await window.api.call('undo', {});
      if (!r || r.ok === false) break;
    }
    await window.__app.refresh();
  });
  expect((await state()).edits).toBe(0);
});

test('(6) ガイドに合わせるのスライダー', async () => {
  await win.locator('#bMacro').click();
  await expect(win.locator('#pop')).toBeVisible();
  // 開いた時点でエンジンが 100% の計画を返す。input（動かしている間）は計画に強度を
  // 掛けて描くだけ、change（離した）で同じ計画を確定する
  await win.waitForFunction(() => window.__app.plan()?.kind === 'guide', null, { timeout: 60000 });
  await expect(win.locator('#popPitchShape')).toBeChecked();
  const before = await win.evaluate(() => window.__app.shapes());
  const curveBefore = await win.evaluate(() => window.__app.editedCurve());
  await win.evaluate(() => {
    const el = document.querySelector('#popPitch');
    el.value = '70';
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const moved = await win.evaluate(() => window.__app.shapes());
  const curveMoved = await win.evaluate(() => window.__app.editedCurve());
  expect(moved.every((m, i) => Math.abs(m.pitch - before[i].pitch) < 0.01)).toBe(true);
  expect(curveMoved.some((v, i) => v != null && curveBefore[i] != null
    && Math.abs(v - curveBefore[i]) > 0.05)).toBe(true);
  expect((await state()).edits).toBe(0);            // まだエンジンは呼んでいない

  await win.evaluate(() => {
    document.querySelector('#popPitch').dispatchEvent(new Event('change', { bubbles: true }));
  });
  await settle();
  expect((await state()).edits).toBeGreaterThan(0);
  await win.keyboard.press('Escape');
});

test('(7) 再生ボタンで再生ヘッドが進む', async () => {
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 120000 });
  const h0 = (await state()).head;
  await win.waitForFunction((h) => window.__app.S.head > h + 0.2, h0, { timeout: 30000 });
  const clock = await win.locator('#clock').textContent();
  expect(clock).toMatch(/^\d+:\d\d\.\d{3}$/);
  await win.locator('#bPlay').click();
  await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 10000 });
});

test('(8) 外部から project.json を書き換えると再描画される', async () => {
  const { projectDir } = await state();
  const jsonPath = path.join(projectDir, 'project.json');
  const before = (await state()).edits;
  expect(before).toBeGreaterThan(0);

  // Claude Code が undo した体で、直接ファイルを書き換える
  const raw = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  for (const c of raw.changesets) c.undone = true;
  raw.updated_at = new Date().toISOString();
  fs.writeFileSync(jsonPath, JSON.stringify(raw, null, 2), 'utf8');

  await win.waitForFunction(() => window.__app.S.vd.edits.length === 0, null, { timeout: 60000 });
  expect(await win.evaluate(() => window.__app.status())).toContain('外部の変更');
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
