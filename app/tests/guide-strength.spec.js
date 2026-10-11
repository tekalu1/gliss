// 「ガイドに合わせる」の割合を後から変える（音程。2026-10-11 承認）。素材・解析モデルの重みは要らない（合成の歌）。
//
//   (R1) ノートごとに当てた割合が、開き直したポップアップに出る。違う割合のノートを一緒に選ぶと
//        「混在」と「ノートごとに lo〜hi%」、つまみは平均。動かしていない線の形は 0%
//   (R2) ポップアップで割合を変える → 1 回の Ctrl+Z で前の割合（混在）に戻る。履歴の項目は 1 つだけ増える
//   (R3) アプリを閉じて開き直しても、割合が出る（プロジェクトに残る）
import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/** 倍音のある合成の歌。notes: [開始秒, 終わり秒, MIDI]、vib: ビブラートの深さ（半音）。 */
function writeSong(file, notes, vib, sec = 5, sr = 22050) {
  const n = sr * sec;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  let ph = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const note = notes.find(([a, b]) => t >= a && t < b);
    let env = 0;
    let f = 220;
    if (note) {
      const [a, b, m] = note;
      env = 0.25 * Math.min(1, (t - a) / 0.04, (b - t) / 0.04);
      f = 440 * 2 ** ((m - 69 + vib * Math.sin(2 * Math.PI * 5.5 * (t - a))) / 12);
    }
    ph += 2 * Math.PI * f / sr;
    let x = 0;
    for (let k = 1; k <= 6; k++) x += Math.sin(k * ph) / k;
    buf.writeInt16LE(Math.round(env * x * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let ROOT;
let TAKE;
let GUIDE;
let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

async function launch() {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_AUTO_LYRICS: '0',
    VOCAL_ENGINE_WORK_DIR: path.join(ROOT, 'work'), VOCAL_ENGINE_PROJECTS: path.join(ROOT, 'projects'),
    VOCAL_ENGINE_LOG_DIR: path.join(ROOT, 'log'), GLISS_BRIDGE: path.join(ROOT, 'bridge.json') };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_F0_ESTIMATOR;
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', path.join(ROOT, 'project'),
      '--user-data-dir', path.join(ROOT, 'userdata'), '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready() && !!window.__app.S.vd?.guide, null, { timeout: 240000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 800));
  await win.evaluate(() => { window.__app.S.view = { t0: 0, span: 5 }; window.__app.render(); });
  await settle();
}

async function close() {
  const pid = app?.process().pid;
  await Promise.race([app?.close(), new Promise((r) => setTimeout(r, 15000))]).catch(() => {});
  try { if (pid) execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* 終わっている */ }
  app = null;
}

test.beforeAll(async () => {
  ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-strength-'));
  TAKE = path.join(ROOT, 'take.wav');
  GUIDE = path.join(ROOT, 'guide.wav');
  writeSong(TAKE, [[0.4, 1.4, 64], [1.8, 2.8, 67], [3.2, 4.4, 69]], 0.05);
  // ガイドは中心がずれていて（+50・+60・−40 セント）、揺れが深い
  writeSong(GUIDE, [[0.4, 1.4, 64.5], [1.8, 2.8, 67.6], [3.2, 4.4, 68.6]], 0.4);
  await launch();
});
test.afterAll(async () => {
  await close();
  if (ROOT) fs.rmSync(ROOT, { recursive: true, force: true });
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
}
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();

/** ガイドに対応するノート（音程を合わせる計画に載るもの）。 */
let ids = [];

async function select(...xs) {
  await win.keyboard.press('Escape');
  await win.keyboard.press('Escape');
  for (const [k, id] of xs.entries()) await blob(id).click(k ? { modifiers: ['Shift'] } : {});
  expect(await win.evaluate(() => [...window.__app.S.sel])).toEqual(xs);
}

/** G でポップアップを開き、計画（ノートごとの今の割合）が届いてからの表示。 */
async function openPop() {
  await win.keyboard.press('g');
  await win.waitForFunction(() => !document.querySelector('#pop').hidden
    && !!window.__app.S.plan?.data?.strength, null, { timeout: 60000 });
  return win.evaluate(() => {
    const q = (s) => document.querySelector(s);
    return { pitch: q('#popPitchV').textContent, pitchSub: q('#popPitchS').hidden ? null : q('#popPitchS').textContent,
      pitchValue: +q('#popPitch').value, shape: q('#popShapeV').textContent, mixed: q('#popPitchV').classList.contains('mix') };
  });
}

/** つまみを動かして離す（離したら当たる）。 */
async function setSlider(sel, v) {
  await win.evaluate(([s, val]) => {
    const el = document.querySelector(s);
    el.value = String(val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, [sel, v]);
  await settle();
}

const live = () => win.evaluate(() => window.__app.S.vd.history.changesets.filter((c) => !c.undone).length);

test('(R1) ノートごとの割合が開き直すと出る・違えば混在', async () => {
  await win.keyboard.press('Escape');
  const all = await openPop();
  expect(all.pitch).toBe('0%');                           // まだ何も当てていない
  ids = await win.evaluate(() => Object.keys(window.__app.plan().pitchPlan));
  expect(ids.length).toBeGreaterThanOrEqual(2);
  await win.keyboard.press('Escape');

  await select(ids[0]);
  await openPop();
  await setSlider('#popPitch', 30);
  await win.keyboard.press('Escape');
  await select(ids[1]);
  await openPop();
  await setSlider('#popPitch', 70);
  await win.keyboard.press('Escape');

  await select(ids[0]);
  expect(await openPop()).toMatchObject({ pitch: '30%', pitchSub: null, pitchValue: 30, shape: '0%', mixed: false });
  await win.keyboard.press('Escape');
  await select(ids[0], ids[1]);
  expect(await openPop()).toMatchObject({ pitch: '混在', pitchSub: 'ノートごとに 30〜70%', pitchValue: 50, shape: '0%', mixed: true });
  await win.keyboard.press('Escape');
});

test('(R2) ポップアップの変更は 1 回の Ctrl+Z で戻る', async () => {
  await select(ids[0], ids[1]);
  await openPop();
  const n0 = await live();
  await setSlider('#popPitch', 45);
  await setSlider('#popPitch', 55);                       // 同じポップアップの中で動かし直しても 1 つ
  expect(await live()).toBe(n0 + 1);
  await win.keyboard.press('Escape');
  expect(await openPop()).toMatchObject({ pitch: '55%', pitchSub: null, mixed: false });
  await win.keyboard.press('Escape');
  await win.keyboard.press('Escape');
  await win.keyboard.press('Control+z');
  await settle();
  expect(await live()).toBe(n0);
  await select(ids[0], ids[1]);
  expect(await openPop()).toMatchObject({ pitch: '混在', pitchSub: 'ノートごとに 30〜70%', pitchValue: 50 });
  await win.keyboard.press('Escape');
});

test('(R3) 閉じて開き直しても割合が出る', async () => {
  await close();
  await launch();
  await select(ids[0], ids[1]);
  expect(await openPop()).toMatchObject({ pitch: '混在', pitchSub: 'ノートごとに 30〜70%', pitchValue: 50 });
  await win.keyboard.press('Escape');
});

test('実行時のコンソールエラーが無い', () => {
  expect(errors).toEqual([]);
});
