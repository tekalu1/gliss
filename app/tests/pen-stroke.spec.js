// ペン（鉛筆）の線が、動かしたマウスの軌跡どおりにフレームへ入ること。音声は合成（素材は要らない）。
//
//   (P1) 斜めの線: 各フレームの値が、軌跡をそのフレームの時刻で読んだ値と同じ（フレームに丸めない）。
//        横のずれ（値から逆算した時刻とフレームの時刻の差）が 1 ms 未満。左へ描いても同じ
//   (P2) サイン・ジグザグを速く動かしても、軌跡からのずれが 1 セント未満（結合した中間の点も使う）
//   (P3) 点がまばら（25 px おき）でも、点と点の間は直線でつながる
//   (P4) 離した位置（pointerup）も線の終わりに入る
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SR = 44100;
const NOTE = [0.3, 2.3, 220];       // 2 秒の長い音（A3）
// ピアノロールの座標（draw.js: KEYS_W・SCALE_H・LANE_H）
const KW = 44; const ST = 20; const LH = 46;

function writeTone(file) {
  const n = Math.round(SR * 2.6);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) {
    const t = i / SR;
    let v = 0;
    if (t >= NOTE[0] && t < NOTE[1]) {
      const env = Math.min(1, (t - NOTE[0]) / 0.03, (NOTE[1] - t) / 0.03);
      for (let k = 1; k <= 6; k++) v += Math.sin(2 * Math.PI * NOTE[2] * k * t) / k;
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
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-pen-stroke-'));
  const wav = path.join(dir, 'tone.wav');
  writeTone(wav);
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
  await win.waitForFunction(() => window.__app.notes().length >= 1, null, { timeout: 120000 });
  await win.evaluate(() => window.__app.setTool('draw'));
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

const settle = () => win.waitForFunction(() => window.__app.idle() && !window.__app.S.stroke, null, { timeout: 120000 }).catch(() => {});

const geom = () => win.evaluate(() => {
  const r = document.querySelector('#roll').getBoundingClientRect();
  const S = window.__app.S;
  return { left: r.left, top: r.top, W: r.width, H: r.height, t0: S.view.t0, span: S.view.span, ptop: S.pv.top, pspan: S.pv.span };
});
const rowHOf = (g) => (g.H - LH - ST) / g.pspan;
const toClient = (g, t, m) => [g.left + KW + (t - g.t0) / g.span * (g.W - KW), g.top + ST + (g.ptop - m) * rowHOf(g)];
const fromClient = (g, x, y) => [g.t0 + (x - g.left - KW) / (g.W - KW) * g.span, g.ptop - (y - g.top - ST) / rowHOf(g)];

async function setView(tspan, pspan, centerT, centerM) {
  await win.evaluate(([a, s, ps, cm]) => {
    const S = window.__app.S;
    S.view = { t0: a, span: s };
    S.pv = { top: cm + ps / 2, span: ps };
    window.__app.render();
  }, [centerT - tspan / 2, tspan, pspan, centerM]);
  await win.waitForTimeout(150);
}

/** 点列（t 昇順）を t で読む（範囲の外は null）。 */
function readAt(pts, t) {
  if (t <= pts[0][0] || t >= pts[pts.length - 1][0]) return null;
  let i = 0;
  while (i + 1 < pts.length && pts[i + 1][0] < t) i++;
  const [ta, ma] = pts[i]; const [tb, mb] = pts[i + 1];
  return tb === ta ? ma : ma + (mb - ma) * (t - ta) / (tb - ta);
}

const shapes = (P) => ({
  diag: (u) => P - 1.5 + 3 * u,
  zigzag: (u) => { const k = (u * 6) % 1; return P + (k < 0.5 ? -1 + 4 * k : 3 - 4 * k); },
  sine: (u) => P + 1.2 * Math.sin(2 * Math.PI * 2 * u),
});

/** 軌跡を送って、描いている最中の線（strokeData）を返す。離したあとは取り消して元に戻す。 */
async function draw({ shape, tspan, pspan, pxStep, reverse = false, upExtraPx = 0 }) {
  const P = 57;                                    // A3（220 Hz）
  const a = 0.8; const b = 1.8;
  const ta = Math.max(a, (a + b) / 2 - tspan * 0.35); const tb = Math.min(b, (a + b) / 2 + tspan * 0.35);
  await setView(tspan, pspan, (ta + tb) / 2, P);
  const g = await geom();
  const [x0] = toClient(g, ta, 0); const [x1] = toClient(g, tb, 0);
  const n = Math.max(2, Math.ceil((x1 - x0) / pxStep));
  const sent = [];
  for (let k = 0; k <= n; k++) {
    const u = k / n;
    sent.push(toClient(g, ta + (tb - ta) * u, shapes(P)[shape](u)));
  }
  if (reverse) sent.reverse();
  await win.mouse.move(sent[0][0], sent[0][1]);
  await win.mouse.down();
  for (let k = 1; k < sent.length; k++) await win.mouse.move(sent[k][0], sent[k][1]);
  let up = null;
  if (upExtraPx) {
    // 最後の pointermove の後で、さらに動いてから離した（pointerup だけが届く）
    const last = sent[sent.length - 1];
    up = [last[0] + (reverse ? -upExtraPx : upExtraPx), last[1]];
    await win.evaluate(([x, y]) => {
      document.querySelector('#roll').dispatchEvent(new PointerEvent('pointerup', { clientX: x, clientY: y, bubbles: true, pointerId: 1 }));
    }, up);
  }
  const sd = await win.evaluate(() => window.__app.stroke());
  await win.mouse.up();
  await settle();
  await win.evaluate(() => window.__app.undo());
  await win.waitForFunction(() => window.__app.idle() && !(window.__app.S.vd.edits || []).some((e) => e.kind === 'pitch_draw'),
    null, { timeout: 60000 }).catch(() => {});
  const sentTM = sent.map(([x, y]) => fromClient(g, x, y)).sort((p, q) => p[0] - q[0]);
  return { sd, sentTM, g, up };
}

const maxAbsCents = (sd, sentTM) => {
  let worst = 0; let count = 0;
  for (const [t, m] of sd.pts) {
    const exp = readAt(sentTM, t);
    if (exp == null) continue;
    worst = Math.max(worst, Math.abs(m - exp) * 100);
    count++;
  }
  return { worst, count };
};

test('P1 斜めの線: 値から逆算した時刻がフレームの時刻と同じ（向きに関わらず）', async () => {
  for (const reverse of [false, true]) {
    const { sd, sentTM, g } = await draw({ shape: 'diag', tspan: 0.6, pspan: 12, pxStep: 3, reverse });
    expect(sd.pts.length).toBeGreaterThan(20);
    // diag は m が t に比例する（P−1.5 → P+1.5）。値から逆算した時刻とフレームの時刻の差（ms）
    const [tA, mA] = sentTM[0]; const [tB, mB] = sentTM[sentTM.length - 1];
    let worstMs = 0;
    for (const [t, m] of sd.pts) {
      if (t <= tA || t >= tB) continue;
      worstMs = Math.max(worstMs, Math.abs(tA + (m - mA) / (mB - mA) * (tB - tA) - t) * 1000);
    }
    expect(worstMs, `reverse=${reverse}`).toBeLessThan(1);
    expect(maxAbsCents(sd, sentTM).worst, `reverse=${reverse}`).toBeLessThan(1);
    expect(g.span).toBeCloseTo(0.6, 3);
  }
});

test('P2 サイン・ジグザグを速く動かしてもずれない', async () => {
  for (const c of [
    { shape: 'sine', tspan: 0.6, pspan: 6, pxStep: 6 },
    { shape: 'zigzag', tspan: 2.0, pspan: 12, pxStep: 4 },
    { shape: 'sine', tspan: 2.0, pspan: 12, pxStep: 4 },
  ]) {
    const { sd, sentTM } = await draw(c);
    const r = maxAbsCents(sd, sentTM);
    expect(r.count, JSON.stringify(c)).toBeGreaterThan(20);
    expect(r.worst, JSON.stringify(c)).toBeLessThan(1);
  }
});

test('P3 点がまばらでも点と点の間は直線', async () => {
  for (const pxStep of [25, 50]) {
    const { sd, sentTM } = await draw({ shape: 'sine', tspan: 2.0, pspan: 12, pxStep });
    const r = maxAbsCents(sd, sentTM);
    expect(r.count, `pxStep=${pxStep}`).toBeGreaterThan(20);
    expect(r.worst, `pxStep=${pxStep}`).toBeLessThan(1);
  }
});

test('P4 離した位置も線の終わりに入る', async () => {
  const { sd, sentTM, g, up } = await draw({ shape: 'diag', tspan: 2.0, pspan: 12, pxStep: 6, upExtraPx: 60 });
  const [tUp] = fromClient(g, up[0], up[1]);
  const lastT = sd.pts[sd.pts.length - 1][0];
  expect(lastT).toBeGreaterThan(sentTM[sentTM.length - 1][0] + 0.01);
  // 線は離した位置までのフレーム（その時刻以下で最後のフレーム）で終わる
  expect(lastT).toBeLessThanOrEqual(tUp + 1e-6);
  expect(tUp - lastT).toBeLessThan(0.0101);
});

test('エラーが出ていない', () => {
  expect(errors).toEqual([]);
});
