// ノートにマウスを載せたときの強調（2026-10-11 承認。モックの案 A）。素材は要らない（合成の歌）。
//
//   (H1) ホバーしたテイクのノートだけ、帯の色の縁 1.6px が付く（ほかのノートは縁なし）。離すと消える
//   (H2) ガイドは、ホバーでも選択でも見た目が変わらない（縁・太さ・塗り）。対のガイドの印と「ガイドより…」の札は無い
//   (H3) 手で直した（白）帯は、ホバーで縁が白の 1.6px
//   (H4) プラグインのモード（ARA）でも同じ（renderer/ara.js は同じ draw.js・interact.js を使う）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-hover-spec-'));
const TAKE_WAV = path.join(ROOT, 'take.wav');
const GUIDE_WAV = path.join(ROOT, 'guide.wav');
const TAKE_RGB = 'rgb(230, 210, 74)';          // palette.js の TAKE（#e6d24a）
const GUIDE_C = '#5aa2ff';

/** 倍音のある合成の歌（ビブラート付き）。notes: [開始秒, 終わり秒, MIDI]。 */
function writeSong(file, notes, sec = 5, sr = 44100) {
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
      f = 440 * 2 ** ((m - 69) / 12 + Math.sin(2 * Math.PI * 5.5 * (t - a)) * 0.003);
    }
    ph += 2 * Math.PI * f / sr;
    let x = 0;
    for (let k = 1; k <= 6; k++) x += Math.sin(k * ph) / k;
    buf.writeInt16LE(Math.round(env * x * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let app;
let win;
const errors = [];

async function launch(args, extraEnv = {}) {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_AUTO_LYRICS: '0',
    VOCAL_ENGINE_WORK_DIR: path.join(ROOT, 'work'), VOCAL_ENGINE_PROJECTS: path.join(ROOT, 'projects'),
    VOCAL_ENGINE_LOG_DIR: path.join(ROOT, 'log'), GLISS_BRIDGE: path.join(ROOT, 'bridge.json'), ...extraEnv };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_F0_ESTIMATOR;
  app = await electron.launch({ args: [APP, '--user-data-dir', path.join(ROOT, `userdata-${args.length}`), '--mute', ...args], env });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
}

async function close() {
  await app?.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 1 }); }).catch(() => {});
  await app?.close().catch(() => {});
  app = null;
}

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  writeSong(TAKE_WAV, [[0.4, 1.4, 64], [1.8, 2.8, 67], [3.2, 4.4, 69]]);
  writeSong(GUIDE_WAV, [[0.45, 1.45, 64.3], [1.9, 2.8, 66.6], [3.2, 4.4, 69.2]]);
});

test.afterAll(async () => {
  await close();
  fs.rmSync(ROOT, { recursive: true, force: true });
});

const roll = () => win.evaluate(() => {
  const q = (s) => [...document.querySelectorAll(s)];
  const style = (p) => { const c = getComputedStyle(p); return { stroke: c.stroke, sw: c.strokeWidth, so: +c.strokeOpacity, fo: +c.fillOpacity }; };
  return {
    blobs: Object.fromEntries(q('#roll path[data-blob]').map((p) => [p.dataset.blob, style(p)])),
    guides: q('#roll path[data-guide]').map((p) => [p.getAttribute('fill'), p.getAttribute('fill-opacity'), p.getAttribute('stroke'),
      p.getAttribute('stroke-width'), p.getAttribute('stroke-opacity')].join(' ')),
    pairs: q('#roll [data-pair]').length,
    tips: q('#roll [data-guide-diff]').length,
    noteHover: window.__app.S.noteHover,
  };
});

const noteBox = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first().boundingBox();

async function hoverNote(id) {
  const box = await noteBox(id);
  await win.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 3 });
  await win.waitForFunction((i) => window.__app.S.noteHover === i, id, { timeout: 10000 });
}

async function leave() {
  await win.mouse.move(2, 2);
  await win.waitForFunction(() => window.__app.S.noteHover == null, null, { timeout: 10000 });
}

/** ホバーの縁の確かめ（H1・H2）。ガイドが 1 つ以上あること。 */
async function checkHover() {
  await win.evaluate(() => { const A = window.__app; A.S.sel = []; A.S.noteHover = null; A.S.view = { t0: 0, span: A.totalSec() }; A.render(); });
  const ids = await win.evaluate(() => window.__app.S.pitched.filter((n) => n.end_sec - n.start_sec > 0.3).map((n) => n.id));
  expect(ids.length).toBeGreaterThanOrEqual(2);
  const base = await roll();
  expect(base.guides.length).toBeGreaterThan(0);
  expect(new Set(base.guides)).toEqual(new Set([`${GUIDE_C} 0.16 ${GUIDE_C} 1 0.9`]));
  for (const b of Object.values(base.blobs)) expect(b.stroke).toBe('none');

  for (const id of ids) {
    await hoverNote(id);
    const hov = await roll();
    // ホバーしたノートだけ、テイクの帯の色の縁 1.6px
    expect(hov.blobs[id]).toMatchObject({ stroke: TAKE_RGB, sw: '1.6px', so: 1 });
    expect(hov.blobs[id].fo).toBeGreaterThan(0.3);       // 塗りは今までどおり濃くなる
    for (const [k, b] of Object.entries(hov.blobs)) if (k !== id) expect(b.stroke, k).toBe('none');
    // ガイドは変わらない。対の印も差の札も無い
    expect(hov.guides).toEqual(base.guides);
    expect(hov.pairs).toBe(0);
    expect(hov.tips).toBe(0);
    await leave();
    const after = await roll();
    for (const b of Object.values(after.blobs)) expect(b.stroke).toBe('none');
    expect(after.guides).toEqual(base.guides);
  }

  // 選択だけ（ホバーなし）でも、ガイドは変わらず、対の印も札も無い。縁は付かない（ホバーのときだけ）
  await win.evaluate((i) => { window.__app.S.sel = [i]; window.__app.render(); }, ids[0]);
  const sel = await roll();
  expect(sel.guides).toEqual(base.guides);
  expect(sel.pairs).toBe(0);
  expect(sel.tips).toBe(0);
  expect(sel.blobs[ids[0]].stroke).toBe('none');
  // 選んだノートをホバーしても札は出ない
  await hoverNote(ids[0]);
  const both = await roll();
  expect(both.guides).toEqual(base.guides);
  expect(both.tips).toBe(0);
  expect(both.blobs[ids[0]]).toMatchObject({ stroke: TAKE_RGB, sw: '1.6px' });
  await leave();
  await win.evaluate(() => { window.__app.S.sel = []; window.__app.render(); });
  return ids;
}

test('(H1)(H2) ホバーしたテイクのノートだけ帯の色の縁が付き、ガイドはホバー・選択で変わらない', async () => {
  await launch(['--take', TAKE_WAV, '--guide', GUIDE_WAV, '--project-dir', path.join(ROOT, 'project')]);
  await win.waitForFunction(() => window.__app?.ready() && window.__app.S.pitched.length > 0
    && (window.__app.S.vd?.guide_notes?.length || 0) > 0, null, { timeout: 240000 });
  const ids = await checkHover();

  // (H3) 手で直した（白）帯: ホバーで縁が白の 1.6px
  // （ホバーで描き直した後、同じ turn の中で DOM の印だけを白の帯にして、CSS の優先を見る）
  await hoverNote(ids[0]);
  const man = await win.evaluate((i) => {
    const el = document.querySelector(`#roll path[data-blob="${i}"]`);
    el.setAttribute('data-man', '1'); el.setAttribute('fill', '#ffffff'); el.style.setProperty('--bc', '#ffffff');
    const c = getComputedStyle(el);
    return { stroke: c.stroke, sw: c.strokeWidth, so: +c.strokeOpacity };
  }, ids[0]);
  expect(man).toMatchObject({ stroke: 'rgb(255, 255, 255)', sw: '1.6px', so: 1 });
  await leave();
  expect(errors).toEqual([]);
  await close();
});

test('(H4) プラグインのモード（ARA）でも同じ', async () => {
  await launch([], { GLISS_TEST_ARA: '1' });
  await win.waitForFunction(() => !!window.__app && window.api?.mode === 'ara', null, { timeout: 120000 });
  await win.evaluate(async ([a, b]) => {
    const r = await window.api.call('new_project', { take_path: a, author: 'human' });
    if (r.ok === false) throw new Error(r.error);
    const s = await window.api.call('add_track', { path: b, guide: true, author: 'human' });
    if (s.ok === false) throw new Error(s.error);
  }, [TAKE_WAV, GUIDE_WAV]);
  await win.evaluate(() => window.api.__araEmit('session-changed', { dir: '' }));
  await win.waitForFunction(() => window.__app.S.pitched.length > 0 && window.__app.idle() && !window.__app.S.opening,
    null, { timeout: 240000 });
  const ids = await win.evaluate(() => window.__app.S.pitched.filter((n) => n.end_sec - n.start_sec > 0.3).map((n) => n.id));
  expect(ids.length).toBeGreaterThanOrEqual(2);
  await win.evaluate(() => { const A = window.__app; A.S.view = { t0: 0, span: A.totalSec() }; A.render(); });
  const guides = (await roll()).guides;
  expect(guides.length).toBeGreaterThan(0);                // ガイドが無いと、ガイドが変わらないことを確かめたことにならない
  for (const id of ids.slice(0, 2)) {
    await hoverNote(id);
    const hov = await roll();
    expect(hov.blobs[id]).toMatchObject({ sw: '1.6px', so: 1 });
    expect(hov.blobs[id].stroke).not.toBe('none');
    for (const [k, b] of Object.entries(hov.blobs)) if (k !== id) expect(b.stroke, k).toBe('none');
    expect(hov.pairs).toBe(0);
    expect(hov.tips).toBe(0);
    expect(hov.guides).toEqual(guides);
    await leave();
  }
  expect(errors).toEqual([]);
});
