// v3 の見た目と表示の操作（issue #15・#21・#19。提案 proposal/v3.html §3・§6・§9）。
//
//   #15 ノートを平均の音程の水平な帯に
//     (1) 帯（blob）はノートの平均の音程（band_midi）に水平。ガイドも同じ形でグレー
//     (2) 端のつかみ: 内側 8 px（短いノートは幅の 1/3）・外側 4 px（隣との隙間の半分まで）、縦は中心 ±10 px
//     (3) 端に乗ると明るい縦線とカーソル ↔。離れると消える
//     (4) 上下のドラッグで帯と線が一緒に動く。ドラッグ中の帯の高さ = 離した後
//   #21 子音の色
//     (1) 歌詞が無いときは区別しない
//     (2) 歌詞がある区間だけ、帯の中の子音を同じ色相のまま彩度を落とす。選択・ホバーの濃さは同じ比
//   #19 縦ズームとホイール（割り当ては #27 で Studio One に合わせた。設定で変えるのは wheel-preview.spec.js）
//     (1) Ctrl+ホイール = 縦ズーム（ポインタの下の音程が中心、6〜36 半音）
//     (2) ホイール = 縦スクロール
//     (3) Shift+ホイール = 横スクロール、Ctrl+Shift+ホイール = 横ズーム
//     (4) トラックビュー: Ctrl+ホイール = 全トラックの高さ（28〜96 px）、Ctrl+Shift+ホイールは何もしない
//     (5) 表示 > ズームを戻す
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
const PROJECT = path.join(REPO, 'projects', '_test-view-v3');
const USERDATA = `${PROJECT}-userdata`;
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
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
  fs.mkdirSync(DOCS, { recursive: true });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
}

/** path の d → 閉じた区間ごとの点列（上の縁を行き、下の縁を戻る）。 */
function centersOf(sel) {
  return win.evaluate((s) => [...document.querySelectorAll(s)].map((p) => {
    const polys = p.getAttribute('d').split('Z').filter(Boolean)
      .map((one) => [...one.matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)].map((m) => [+m[1], +m[2]]));
    return polys.map((pts) => {
      const n = pts.length / 2;
      const cs = [];
      for (let k = 0; k < n; k++) cs.push((pts[k][1] + pts[pts.length - 1 - k][1]) / 2);
      return cs;
    });
  }), sel);
}

// ---------------------------------------------------------------- #15
test('#15 (1) 帯はノートの平均の音程に水平。ガイドも同じ形', async () => {
  const bands = await win.evaluate(() => window.__app.bands());
  expect(bands.length).toBeGreaterThan(5);
  for (const b of bands) expect(b.band).not.toBeNull();
  // 平均（音量の重み・無声を除く）は、解析の中心（中央値に近い）とは一致しないノートがある
  expect(bands.some((b) => Math.abs(b.band - b.edited) > 0.05)).toBe(true);
  // テイク: 帯の中心の高さは 1 本の水平線（丸めの 0.1 px まで）で、band_midi の高さ
  const info = await win.evaluate(() => window.__app.edgeInfo('n005'));
  const take = await centersOf('#roll path[data-blob="n005"]');
  for (const cs of take[0][0]) expect(Math.abs(cs - info.yc)).toBeLessThan(0.11);
  // ガイド: 同じ形（水平）でグレー
  const guides = await centersOf('#roll path[data-guide]');
  expect(guides.length).toBeGreaterThan(3);
  for (const g of guides) {
    for (const cs of g) expect(Math.max(...cs) - Math.min(...cs)).toBeLessThan(0.11);
  }
  const gb = await win.evaluate(() => window.__app.guideBands());
  expect(gb.filter((g) => g.pitch != null).every((g) => g.band != null)).toBe(true);
  // ピッチの揺れは細い線で重ねる（黄色の曲線は帯と別に描いている）
  expect(await win.locator('#roll path[stroke="#e6d24a"]').count()).toBeGreaterThan(0);
});

test('#15 (2) 端のつかみ: 内側 8 px・外側 4 px（隣との隙間の半分まで）・縦 ±10 px', async () => {
  const r = await win.evaluate(() => {
    const S = window.__app.S;
    return S.pitched.map((n, k) => ({ k, id: n.id, ...window.__app.edgeInfo(n.id) }))
      .filter((e) => e.start && e.end);
  });
  expect(r.length).toBeGreaterThan(3);
  const tol = 0.15;
  let outer0 = 0;
  let outer4 = 0;
  for (let i = 0; i < r.length; i++) {
    const e = r[i];
    const inner = Math.min(8, (e.x1 - e.x0) / 3);
    for (const [bx, x] of [[e.start, e.x0], [e.end, e.x1]]) {
      expect(Math.abs(bx.y - (e.yc - 10))).toBeLessThan(tol);
      expect(bx.h).toBe(20);
      // 内側はちょうど inner
      if (bx === e.start) expect(Math.abs(bx.x + bx.w - (x + inner))).toBeLessThan(tol);
      else expect(Math.abs(bx.x - (x - inner))).toBeLessThan(tol);
    }
    const out = e.x0 - e.start.x;
    expect(out).toBeGreaterThan(-tol);
    expect(out).toBeLessThan(4 + tol);
    if (Math.abs(out) < tol) outer0 += 1;
    if (Math.abs(out - 4) < tol) outer4 += 1;
    // 外側は隣との隙間の半分まで（隣のつかみと重ならない）
    const prev = r.find((q) => q.k === e.k - 1);
    if (prev) expect(e.start.x).toBeGreaterThanOrEqual(prev.end.x + prev.end.w - tol);
  }
  expect(outer0).toBeGreaterThan(0);       // 子音・息の端と重なる部分は譲る
  expect(outer4).toBeGreaterThan(0);       // 音程なしの帯が接していても外側 4 px が残る
  const noPitchGrab = await win.evaluate(() => {
    const roll = document.querySelector('#roll');
    const origin = roll.getBoundingClientRect();
    const np = [...roll.querySelectorAll('rect[data-nop-edge]')];
    for (const n of window.__app.S.pitched) {
      const e = window.__app.edgeInfo(n.id);
      for (const [which, x, box, opposite, dir] of [
        ['start', e.x0, e.start, 'end', -1], ['end', e.x1, e.end, 'start', 1],
      ]) {
        if (!box || (which === 'start' ? x - box.x : box.x + box.w - x) < 3.8) continue;
        const band = np.find((p) => {
          if (p.dataset.nopEdge !== opposite) return false;
          const px = +p.getAttribute('x'); const width = +p.getAttribute('width');
          return Math.abs((opposite === 'end' ? px + width + 4 : px - 4) - x) < 0.2 && width >= 3.8;
        });
        if (!band) continue;
        const y = +band.getAttribute('y') + +band.getAttribute('height') / 2;
        const hit = document.elementFromPoint(origin.x + x + dir * 6, origin.y + y);
        return { expected: band.dataset.nop, hit: hit?.getAttribute('data-nop'), edge: hit?.getAttribute('data-nop-edge') };
      }
    }
    return null;
  });
  expect(noPitchGrab).not.toBeNull();
  expect(noPitchGrab.hit).toBe(noPitchGrab.expected);
  expect(noPitchGrab.edge).toBeTruthy();
  // #44 以降、この区間のノート間には音程のない帯が入る。表示から一時的に除いて空き端を検査する。
  const free = await win.evaluate(() => {
    const S = window.__app.S;
    const notes = S.notes;
    try {
      S.notes = notes.filter((n) => n.kind === 'note');
      window.__app.render();
      const e = window.__app.edgeInfo('n002');
      return e.x0 - e.start.x;
    } finally {
      S.notes = notes;
      window.__app.render();
    }
  });
  expect(free).toBeCloseTo(4, 1);          // 隙間のある端は外側 4 px
});

test('#15 (3) 端に乗ると明るい縦線とカーソル ↔', async () => {
  const e = await win.evaluate(() => window.__app.edgeInfo('n005'));
  const r = await win.locator('#roll').boundingBox();
  expect(e.hot).toEqual([]);
  // 帯の中心から 8 px 下（帯の外でも ±10 px ならつかめる）・端の 3 px 内側
  await win.mouse.move(r.x + e.x1 - 3, r.y + e.yc + 8);
  await expect.poll(() => win.evaluate(() => window.__app.edgeHover())).toEqual({ id: 'n005', which: 'end' });
  const hot = (await win.evaluate(() => window.__app.edgeInfo('n005'))).hot;
  expect(hot.length).toBe(1);
  expect(Math.abs(hot[0].x - e.x1)).toBeLessThan(0.11);
  expect(hot[0].y1).toBeLessThan(e.yc - 7);
  expect(hot[0].y2).toBeGreaterThan(e.yc + 7);
  const cursor = await win.evaluate(() => getComputedStyle(document.elementFromPoint(
    ...(() => { const b = document.querySelector('#roll rect[data-note="n005"][data-edge="end"]').getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; })())).cursor);
  expect(['col-resize', 'ew-resize']).toContain(cursor);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-band-edge.png') });
  // 縦に 10 px より離れると端ではない（帯の中央 = ノートの移動・ピッチ）
  await win.mouse.move(r.x + e.x1 - 3, r.y + e.yc + 13);
  await expect.poll(() => win.evaluate(() => window.__app.edgeHover())).toBeNull();
  expect((await win.evaluate(() => window.__app.edgeInfo('n005'))).hot).toEqual([]);
});

test('#15 (4) 上下のドラッグで帯と線が一緒に動く。ドラッグ中 = 離した後', async () => {
  const e = await win.evaluate(() => window.__app.edgeInfo('n005'));
  const r = await win.locator('#roll').boundingBox();
  const before = (await win.evaluate(() => window.__app.shapes())).find((s) => s.id === 'n005');
  const cx = r.x + (e.x0 + e.x1) / 2;
  await win.mouse.move(cx, r.y + e.yc);
  await win.mouse.down();
  await win.mouse.move(cx, r.y + e.yc - 30, { steps: 6 });
  const during = (await win.evaluate(() => window.__app.shapes())).find((s) => s.id === 'n005');
  const dur = await win.evaluate(() => window.__app.edgeInfo('n005'));
  const cs = await centersOf('#roll path[data-blob="n005"]');
  for (const c of cs[0][0]) expect(Math.abs(c - dur.yc)).toBeLessThan(0.11);      // 帯も動いている
  expect(during.band - before.band).toBeGreaterThan(0.5);
  // 線もノートの中は同じ量だけ上がる
  const curves = await win.evaluate(() => ({ now: window.__app.editedCurve(), eng: window.__app.engineCurve() }));
  const moved = curves.now.map((v, i) => (v == null ? null : v - curves.eng[i])).filter((v) => v != null && Math.abs(v) > 1e-6);
  expect(moved.length).toBeGreaterThan(5);
  expect(Math.max(...moved)).toBeCloseTo(during.band - before.band, 3);
  await win.mouse.up();
  await settle();
  const after = (await win.evaluate(() => window.__app.shapes())).find((s) => s.id === 'n005');
  expect(Math.abs(after.band - during.band)).toBeLessThan(0.01);
  await win.keyboard.press('Control+z');
  await settle();
});

// ---------------------------------------------------------------- #21
/** 子音の path（data-cons）と、子音の音素の横の範囲（px）。 */
function consLook() {
  return win.evaluate(() => {
    const S = window.__app.S;
    const W = document.querySelector('#roll').getBoundingClientRect().width;
    const X = (t) => 44 + (t - S.view.t0) / S.view.span * (W - 44);
    const xsOf = (d) => [...d.matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)].map((m) => +m[1]);
    const ranges = window.__app.phonemes().filter((p) => p.label === 'consonant').map((p) => [X(p.start), X(p.end)]);
    return {
      ranges,
      cons: [...document.querySelectorAll('#roll [data-cons]')].map((el) => ({
        id: el.dataset.cons, fill: el.getAttribute('fill'), blob: el.classList.contains('blob'),
        xs: xsOf(el.getAttribute('d')),
        op: getComputedStyle(el).fillOpacity,
        mainOp: el.parentElement.querySelector('[data-blob]') ? getComputedStyle(el.parentElement.querySelector('[data-blob]')).fillOpacity : null,
      })),
      main: [...document.querySelectorAll('#roll path[data-blob]')].map((el) => ({ id: el.dataset.blob, fill: el.getAttribute('fill') })),
    };
  });
}

test('#21 (1) 歌詞が無いときは子音を区別しない', async () => {
  const c = await consLook();
  expect(c.cons).toEqual([]);
  expect(new Set(c.main.map((m) => m.fill))).toEqual(new Set(['#e6d24a']));
});

test('#21 (2) 歌詞があると、帯の中の子音の区間だけ同じ色相のまま彩度を落とす', async () => {
  // 最初の発声のかたまりにだけ歌詞を付ける（区間ごと）
  const u = (await win.evaluate(() => window.__app.utterances()))[0];
  await win.evaluate((t) => window.__app.openLyrics(t), (u[0] + u[1]) / 2);
  await win.locator('#lyrIn').fill(M.text('C.part1'));
  await win.locator('#lyrIn').press('Enter');
  await win.waitForFunction(() => window.__app.phonemes().length > 0, null, { timeout: 120000 });
  await settle();
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
  const c = await consLook();
  expect(c.ranges.length).toBeGreaterThan(2);
  const bodies = c.cons.filter((x) => x.blob);
  expect(bodies.length).toBeGreaterThan(0);                  // 有声の子音（m・r）はノートの帯の中
  for (const x of c.cons) {
    expect(x.fill).toBe('#bdb57a');
    // 子音の path は子音の音素の範囲の中だけ
    for (const v of x.xs) expect(c.ranges.some(([a, b]) => v >= a - 0.15 && v <= b + 0.15)).toBe(true);
  }
  // 歌詞を付けていない 2 つ目のかたまりには子音の色が無い
  const second = (await win.evaluate(() => window.__app.utterances()))[1];
  const ids = await win.evaluate((r) => window.__app.S.notes.filter((n) => n.start_sec >= r[0] && n.end_sec <= r[1] + 0.01).map((n) => n.id), second);
  expect(c.cons.filter((x) => ids.includes(x.id))).toEqual([]);
  // 選択・ホバーの濃さは子音にも同じ比でかかる（同じ .blob）
  for (const x of bodies) expect(x.op).toBe(x.mainOp ?? x.op);
  const id = bodies[0].id;
  await win.evaluate((i) => { window.__app.S.sel = [i]; window.__app.render(); }, id);
  const sel = (await consLook()).cons.find((x) => x.id === id && x.blob);
  expect(+sel.op).toBeCloseTo(0.62, 2);
  if (sel.mainOp != null) expect(+sel.mainOp).toBeCloseTo(0.62, 2);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-consonant.png') });
  await win.evaluate(() => { window.__app.S.sel = []; window.__app.render(); });
});

// ---------------------------------------------------------------- #19
const pview = () => win.evaluate(() => window.__app.pitchView());
const hview = () => win.evaluate(() => window.__app.view());
/** #roll の中の y（px）→ その高さの音程（draw.js の M と同じ式）。 */
const pitchAt = (y) => win.evaluate((yy) => {
  const S = window.__app.S; const r = document.querySelector('#roll').getBoundingClientRect();
  const top = 20; const bot = r.height - 46;
  return S.pv.top - (yy - top) / ((bot - top) / S.pv.span);
}, y);

async function wheelAt(x, y, dx, dy, mods = []) {
  await win.mouse.move(x, y);
  for (const k of mods) await win.keyboard.down(k);
  await win.mouse.wheel(dx, dy);
  for (const k of mods.slice().reverse()) await win.keyboard.up(k);
}

test('#19 (1) Ctrl+ホイール（#27 で Studio One に合わせた）: ポインタの下の音程を中心に縦ズーム（6〜36 半音）。横はそのまま', async () => {
  await win.evaluate(() => window.__app.zoomReset());
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
  const r = await win.locator('#roll').boundingBox();
  const y = 200;
  const p0 = await pview(); const h0 = await hview();
  const m0 = await pitchAt(y);
  await wheelAt(r.x + 600, r.y + y, 0, -100, ['Control']);     // 上へ回す = 拡大
  await expect.poll(async () => (await pview()).span).toBeLessThan(p0.span);
  expect(await pitchAt(y)).toBeCloseTo(m0, 3);                            // ポインタの下の音程は動かない
  expect(await hview()).toEqual(h0);
  for (let i = 0; i < 30; i++) await wheelAt(r.x + 600, r.y + y, 0, -100, ['Control']);
  await expect.poll(async () => (await pview()).span).toBe(6);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-vzoom.png') });
  for (let i = 0; i < 40; i++) await wheelAt(r.x + 600, r.y + y, 0, 100, ['Control']);
  await expect.poll(async () => (await pview()).span).toBe(36);
  expect(await hview()).toEqual(h0);
});

test('#19 (2) ホイール（#27 で Studio One に合わせた）: 音程方向の縦スクロール。横はそのまま', async () => {
  await win.evaluate(() => { const a = window.__app; a.S.pv = { top: a.S.midiHi + 0.5, span: 12 }; a.render(); });
  const r = await win.locator('#roll').boundingBox();
  const p0 = await pview(); const h0 = await hview();
  await wheelAt(r.x + 600, r.y + 200, 0, 100);                           // 下へ回す = 低い方へ
  await expect.poll(async () => (await pview()).top).toBeLessThan(p0.top - 0.9);
  expect((await pview()).span).toBe(12);
  const p1 = await pview();
  await wheelAt(r.x + 600, r.y + 200, 0, -100);                          // 上へ回す = 高い方へ
  await expect.poll(async () => (await pview()).top).toBeGreaterThan(p1.top + 0.9);
  expect(await hview()).toEqual(h0);
  // 範囲の外へは行かない（データの範囲の上下 1 オクターブまで）
  for (let i = 0; i < 40; i++) await wheelAt(r.x + 600, r.y + 200, 0, 100);
  const lo = await win.evaluate(() => window.__app.S.midiLo);
  await expect.poll(async () => { const p = await pview(); return +(p.top - p.span).toFixed(3); }).toBe(Math.max(0, lo - 12));
});

test('#19 (3) Shift+ホイール = 横スクロール、Ctrl+Shift+ホイール = 横ズーム（#27。縦は変わらない）', async () => {
  await win.evaluate(() => { window.__app.S.view = { t0: 0.3, span: 1.4 }; window.__app.render(); });
  const r = await win.locator('#roll').boundingBox();
  const p0 = await pview();
  await wheelAt(r.x + 600, r.y + 200, 0, 100, ['Shift']);
  await expect.poll(async () => (await hview()).t0).toBeGreaterThan(0.3);
  expect((await hview()).span).toBeCloseTo(1.4, 6);
  await wheelAt(r.x + 600, r.y + 200, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => (await hview()).span).toBeLessThan(1.4);
  expect(await pview()).toEqual(p0);
});

test('#19 (4) トラックビュー: Ctrl+ホイール（#27）で全トラックの高さ（28〜96 px）、Ctrl+Shift+ホイールは上の横ズーム（#39）', async () => {
  await win.evaluate(() => window.__app.zoomReset());       // 下のエディターも開いたときの表示に（スクリーンショット用）
  const lanes = await win.locator('#lanes').boundingBox();
  const ts = () => win.evaluate(() => window.__app.tracksState());
  const zoom0 = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor());
  const t0 = await ts();
  expect(t0.trackH).toBe(44);
  await wheelAt(lanes.x + 300, lanes.y + 10, 0, -100, ['Control']);
  await expect.poll(async () => (await ts()).trackH).toBeGreaterThan(44);
  // 見出しの高さもレーンと同じ
  const hh = await win.evaluate(() => document.querySelector('#heads .th').getBoundingClientRect().height);
  expect(hh).toBe((await ts()).trackH);
  for (let i = 0; i < 20; i++) await wheelAt(lanes.x + 300, lanes.y + 10, 0, -100, ['Control']);
  await expect.poll(async () => (await ts()).trackH).toBe(96);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-track-height.png') });
  // ふつうのホイールはトラックの縦スクロール（入り切らないとき）
  const sc = await win.evaluate(() => { const b = document.querySelector('#tvBody'); return b.scrollHeight > b.clientHeight + 4; });
  if (sc) {
    await wheelAt(lanes.x + 300, lanes.y + 10, 0, 100);
    await expect.poll(async () => (await ts()).scrollTop).toBeGreaterThan(0);
  }
  for (let i = 0; i < 20; i++) await wheelAt(lanes.x + 300, lanes.y + 10, 0, 100, ['Control']);
  await expect.poll(async () => (await ts()).trackH).toBe(28);
  // Ctrl+Shift（横ズーム）: 上の時間の幅だけ変わる（#39。下の表示・高さ・ページの拡大は変わらない）
  const before = await ts();
  const ed0 = await hview();
  await wheelAt(lanes.x + 300, lanes.y + 10, 0, -100, ['Control', 'Shift']);
  await expect.poll(async () => { const r = (await ts()).range; return r[1] - r[0]; })
    .toBeLessThan(before.range[1] - before.range[0]);
  const after = await ts();
  expect(after.trackH).toBe(before.trackH);
  expect(await hview()).toEqual(ed0);
  expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor())).toBe(zoom0);
});

test('#19 (5) 表示 > ズームを戻す', async () => {
  await win.evaluate(() => window.__app.onMenu({ cmd: 'zoom-reset' }));
  const p = await pview();
  const S = await win.evaluate(() => ({ lo: window.__app.S.midiLo, hi: window.__app.S.midiHi }));
  expect(p.span).toBe(Math.min(36, Math.max(6, S.hi - S.lo + 1)));
  expect((await win.evaluate(() => window.__app.tracksState())).trackH).toBe(44);
  // メニューにある
  const labels = await win.evaluate(() => window.__app.menubar().model.map((m) => [m.label, m.submenu?.map((x) => x.label)]));
  expect(labels.find(([l]) => l === '表示')?.[1]).toContain('ズームを戻す');
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
