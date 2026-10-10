// トラックごとのガイド（見出しのガイドのプルダウン。docs/track-view.md §9）。素材・解析モデルの重みは要らない（合成の歌）。
//
//   プラグイン（GLISS_TEST_ARA=1。DAW の修飾のトラックを ara_open / ara_set_modification で作る）:
//   (G1) 見出しの 2 段目に今のガイド（「共通（名前）」）のボタン。共通のガイドのクリップに「ガイド」の札
//   (G2) キーボードで開いて選ぶ（menuitemradio・DAW のトラック名・自分自身は出さない）→ set_track_guide。
//        見出し・ステータス・エディターに重なるガイド・レーンの灰色が選んだトラックに替わる。Esc でボタンに戻る
//   (G3) Ctrl+Z / Ctrl+Y で戻る・やり直せる（取り消しの名前は「ガイドの指定」）
//   (G4) 編集していないトラックのガイドも選べる（編集対象は変わらない）。共通に戻すと札が消える
//   (G5) 失敗したら元の選択に戻して知らせる
//   (G6) 共通のガイドの指定もプルダウンの最後から。共通のガイドが無いときは「なし（共通のガイドも未指定）」
//   (G7) 360 px 幅: ボタンは見出しの中・メニューは画面の中に収まる。高さを詰めると 1 段目のアイコンに替わる
//   単体（Electron）:
//   (S1) 見出しの 1 段目のアイコンのボタン（M・S の左）で同じプルダウン。選ぶと set_track_guide
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/** 倍音のある合成の歌（ビブラート付き）。notes: [開始秒, 終わり秒, MIDI]。 */
function writeSong(file, notes, sec = 5, sr = 22050) {
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

// 主旋律・3 度上・3 度下・囁き（同じタイミング、高さだけ違う）
const PARTS = [['vo-main', 'Vo Main', 0], ['vo-hamo+3', 'Vo Hamo +3', 4], ['vo-hamo-3', 'Vo Hamo -3', -3], ['vo-asmr', 'Vo ASMR', 0]];

async function launch(root, { ara }) {
  const files = PARTS.map(([n, , d], i) => {
    const f = path.join(root, `${n.replace(/[^\w+-]/g, '_')}-${i}.wav`);
    writeSong(f, [[0.4, 1.4, 64 + d], [1.8, 2.8, 67 + d], [3.2, 4.4, 69 + d]]);
    return f;
  });
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_AUTO_LYRICS: '0',
    VOCAL_ENGINE_WORK_DIR: path.join(root, 'work'), VOCAL_ENGINE_PROJECTS: path.join(root, 'projects'),
    VOCAL_ENGINE_LOG_DIR: path.join(root, 'log'), GLISS_BRIDGE: path.join(root, 'bridge.json') };
  if (ara) env.GLISS_TEST_ARA = '1';
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_F0_ESTIMATOR;
  const app = await electron.launch({ args: [APP, '--user-data-dir', path.join(root, 'userdata'), '--mute'], env });
  const win = await app.firstWindow();
  const errors = [];
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => !!window.__app, null, { timeout: 120000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 800));
  if (ara) {
    await win.evaluate(async ([fs_, parts]) => {
      const c = async (n, a) => { const r = await window.api.call(n, a); if (r.ok === false) throw new Error(`${n}: ${r.error}`); return r; };
      await c('ara_open', { work_key: 'track-guide-spec', name: 'track guide' });
      for (let i = 0; i < fs_.length; i++) {
        await c('ara_set_modification', { ara_id: `mod-${i}`, source_path: fs_[i], source_id: `src-${i}`,
          name: parts[i][0], group: parts[i][1], offset_sec: 0 });
      }
    }, [files, PARTS]);
    await win.evaluate(() => window.api.__araEmit('session-changed', { dir: '' }));
  } else {
    await win.evaluate(async (fs_) => {
      await window.__app.newProject({ take: fs_[0] });
      for (const f of fs_.slice(1)) await window.__app.addTrackFile(f);
    }, files);
  }
  await win.waitForFunction((n) => window.__app.S.tracks.length === n && window.__app.ready(), files.length, { timeout: 240000 });
  const idle = () => win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
  await idle();
  return { app, win, files, errors, idle };
}

async function close(app, root) {
  // 試験の単体のセッションは無題のプロジェクト（未保存）なので、閉じるときの問いは「保存しない」
  await app?.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 1 }); }).catch(() => {});
  await app?.close();
  fs.rmSync(root, { recursive: true, force: true, maxRetries: 5 });
}

const heads = (win) => win.evaluate(() => window.__app.tracksState().heads);
const ids = (win) => win.evaluate(() => window.__app.S.tracks.map((t) => t.id));
const trackOf = (win, id) => win.evaluate((i) => ({ ...window.__app.S.tracks.find((t) => t.id === i) }), id);
const guideShownPath = (win) => win.evaluate(() => (window.__app.S.vd?.guide_basis ? window.__app.S.guide?.path || null : null));
/** レーンでガイドの色（青。エディターのガイドと同じ色）に描いているトラック。 */
const guideRows = (win) => win.evaluate(() => {
  const rows = window.__app.tracksState().order;
  const th = window.__app.tracksState().trackH;
  const ys = new Set([...document.querySelectorAll('#lanes path')]
    .filter((p) => (p.getAttribute('fill') || '').toLowerCase() === '#5aa2ff')
    .map((p) => Math.floor((+(p.getAttribute('transform') || '').match(/,([-\d.]+)\)/)?.[1] || 0) / th)));
  return [...ys].map((r) => rows[r]);
});
const tags = (win) => win.evaluate(() => [...document.querySelectorAll('#lanes [data-gtag]')].map((g) => g.dataset.gtag));

test.describe('プラグイン', () => {
  test.describe.configure({ mode: 'serial' });
  const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-track-guide-ara-'));
  let ctx;
  test.beforeAll(async () => {
    ctx = await launch(ROOT, { ara: true });
    // 共通のガイド = 主旋律、編集対象 = 3 度下
    const [main, , low] = await ids(ctx.win);
    await ctx.win.evaluate((id) => window.__app.setGuide(id), main);
    await ctx.idle();
    await ctx.win.evaluate((id) => window.__app.selectTrack(id), low);
    await ctx.idle();
  });
  test.afterAll(async () => { await close(ctx?.app, ROOT); });

  test('(G1) 見出しの 2 段目に今のガイド、共通のガイドのクリップに「ガイド」の札', async () => {
    const { win, files } = ctx;
    const [main, hi, low] = await ids(win);
    const h = await heads(win);
    expect(h.map((x) => x.guideText)).toEqual(['なし', '共通（vo-main）', '共通（vo-main）', '共通（vo-main）']);
    expect(h[2].guideLabel).toBe('vo-hamo-3 のガイド: 共通のガイド（vo-main）');
    expect(h.map((x) => x.guide)).toEqual([true, false, false, false]);
    expect(await tags(win)).toEqual([main]);
    expect(await guideRows(win)).toEqual([main]);
    expect(await guideShownPath(win)).toBe(files[0]);
    const btn = win.locator(`#heads .th[data-id="${low}"] button.g.wide`);
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute('aria-haspopup', 'menu');
    await expect(btn).toHaveAttribute('aria-expanded', 'false');
    await expect(win.locator(`#heads .th[data-id="${low}"] button.g.ic`)).toBeHidden();   // 1 段目のアイコンは高さが小さいときだけ
    expect(hi).toBeTruthy();
  });

  test('(G2) キーボードで開いて選ぶ → set_track_guide。表示が選んだガイドに替わり、Esc でボタンに戻る', async () => {
    const { win, idle, files } = ctx;
    const [main, hi, low, asmr] = await ids(win);
    const btn = win.locator(`#heads .th[data-id="${low}"] button.g.wide`);
    await btn.focus();
    await win.keyboard.press('Enter');
    const menu = win.locator('#menu');
    await expect(menu).toBeVisible();
    await expect(menu).toHaveAttribute('aria-label', 'vo-hamo-3 のガイド');
    await expect(btn).toHaveAttribute('aria-expanded', 'true');
    const items = await win.evaluate(() => [...document.querySelectorAll('#menu button')].map((b) => ({
      role: b.getAttribute('role'), checked: b.getAttribute('aria-checked'), label: b.querySelector('.l').textContent,
      key: b.querySelector('.k').textContent, item: b.dataset.item })));
    expect(items).toEqual([
      { role: 'menuitemradio', checked: 'true', label: '共通のガイド（vo-main）', key: '', item: 'guide-common' },
      { role: 'menuitemradio', checked: 'false', label: 'vo-main', key: 'Vo Main · 共通のガイド', item: `guide-to:${main}` },
      { role: 'menuitemradio', checked: 'false', label: 'vo-hamo+3', key: 'Vo Hamo +3', item: `guide-to:${hi}` },
      { role: 'menuitemradio', checked: 'false', label: 'vo-asmr', key: 'Vo ASMR', item: `guide-to:${asmr}` },
      { role: 'menuitem', checked: null, label: 'このトラックを共通のガイドにする', key: '', item: 'guide' },
    ]);
    // 最初のフォーカスは今の選択。↓↓ で vo-hamo+3 → Enter
    expect(await win.evaluate(() => document.activeElement?.dataset.item)).toBe('guide-common');
    await win.keyboard.press('ArrowDown');
    await win.keyboard.press('ArrowDown');
    expect(await win.evaluate(() => document.activeElement?.dataset.item)).toBe(`guide-to:${hi}`);
    await win.keyboard.press('Enter');
    await expect(menu).toBeHidden();
    await win.waitForFunction(([t, g]) => window.__app.S.tracks.find((x) => x.id === t)?.guide_id === g, [low, hi]);
    await idle();
    const t = await trackOf(win, low);
    expect(t.effective_guide_id).toBe(hi);
    expect(await win.evaluate(() => window.__app.S.session.guide)).toBe(main);   // 共通のガイドはそのまま
    const h = await heads(win);
    expect(h[2].guideText).toBe('vo-hamo+3');
    expect(h[2].guideLabel).toBe('vo-hamo-3 のガイド: vo-hamo+3');
    expect(await win.evaluate(() => window.__app.status())).toContain('vo-hamo-3 のガイド: vo-hamo+3');
    expect(await guideShownPath(win)).toBe(files[1]);       // エディターに重なるガイドが替わった
    expect(await guideRows(win)).toEqual([hi]);
    expect(await tags(win)).toEqual([main, hi]);
    expect(await win.evaluate(() => window.__app.S.session.current)).toBe(low);
    // フォーカスはボタンに戻っている（描き直しても）。もう一度開いて Esc → ボタン
    expect(await win.evaluate(() => document.activeElement?.closest('.th')?.dataset.id)).toBe(low);
    await win.keyboard.press('Space');
    await expect(menu).toBeVisible();
    expect(await win.evaluate(() => document.activeElement?.dataset.item)).toBe(`guide-to:${hi}`);
    await win.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    expect(await win.evaluate(() => document.activeElement?.getAttribute('aria-label'))).toBe('vo-hamo-3 のガイド: vo-hamo+3');
    await expect(win.locator(`#heads .th[data-id="${low}"] button.g.wide`)).toHaveAttribute('aria-expanded', 'false');
  });

  test('(G3) Ctrl+Z で共通のガイドに戻り、Ctrl+Y でやり直す', async () => {
    const { win, idle, files } = ctx;
    const [main, hi, low] = await ids(win);
    expect(await win.evaluate(() => window.__app.undoTitle())).toContain('ガイドの指定');
    await win.locator('#mock').focus();
    await win.keyboard.press('Control+z');
    await win.waitForFunction((t) => !window.__app.S.tracks.find((x) => x.id === t)?.guide_id, low);
    await idle();
    expect((await trackOf(win, low)).effective_guide_id).toBe(main);
    expect((await heads(win))[2].guideText).toBe('共通（vo-main）');
    expect(await guideShownPath(win)).toBe(files[0]);
    await win.keyboard.press('Control+y');
    await win.waitForFunction(([t, g]) => window.__app.S.tracks.find((x) => x.id === t)?.guide_id === g, [low, hi]);
    await idle();
    expect((await heads(win))[2].guideText).toBe('vo-hamo+3');
    expect(await guideShownPath(win)).toBe(files[1]);
  });

  test('(G4) 編集していないトラックのガイドもマウスで選べる。共通に戻すと札が消える', async () => {
    const { win, idle } = ctx;
    const [main, hi, low, asmr] = await ids(win);
    await win.locator(`#heads .th[data-id="${asmr}"] button.g.wide`).click();
    await win.locator(`#menu [data-item="guide-to:${low}"]`).click();
    await win.waitForFunction(([t, g]) => window.__app.S.tracks.find((x) => x.id === t)?.effective_guide_id === g, [asmr, low]);
    await idle();
    expect(await win.evaluate(() => window.__app.S.session.current)).toBe(low);   // 編集対象は変わらない
    expect((await heads(win))[3].guideText).toBe('vo-hamo-3');
    expect(await tags(win)).toEqual([main, hi, low]);
    // 同じボタンをもう一度押すと閉じる
    const b = win.locator(`#heads .th[data-id="${asmr}"] button.g.wide`);
    await b.click();
    await expect(win.locator('#menu')).toBeVisible();
    await b.click();
    await expect(win.locator('#menu')).toBeHidden();
    // 右クリックの「ガイドを選ぶ…」からも同じプルダウン → 共通に戻す
    await win.locator(`#heads .th[data-id="${asmr}"] .nm`).click({ button: 'right' });
    await win.locator('#menu [data-item="guide-pick"]').click();
    await expect(win.locator('#menu')).toHaveAttribute('aria-label', 'vo-asmr のガイド');
    await win.locator('#menu [data-item="guide-common"]').click();
    await win.waitForFunction((t) => !window.__app.S.tracks.find((x) => x.id === t)?.guide_id, asmr);
    await idle();
    expect(await tags(win)).toEqual([main, hi]);
  });

  test('(G5) 失敗したら元の選択に戻して知らせる', async () => {
    const { app, win, idle } = ctx;
    const [main, , , asmr] = await ids(win);
    await app.evaluate(({ ipcMain }) => {
      globalThis.__origCall = ipcMain._invokeHandlers.get('engine.call');
      ipcMain.removeHandler('engine.call');
      ipcMain.handle('engine.call', async (event, name, args) => {
        if (name === 'set_track_guide') {
          await new Promise((r) => setTimeout(r, 300));
          return { ok: false, error: '試験の失敗' };
        }
        return globalThis.__origCall(event, name, args);
      });
    });
    try {
      await win.locator(`#heads .th[data-id="${asmr}"] button.g.wide`).click();
      await win.locator(`#menu [data-item="guide-to:${main}"]`).click();
      // 待っている間は選んだ値で描く
      await expect.poll(async () => (await heads(win))[3].guideText).toBe('vo-main');
      await idle();
      await expect.poll(async () => (await heads(win))[3].guideText).toBe('共通（vo-main）');
      expect(await win.evaluate(() => window.__app.status())).toContain('vo-asmr のガイドを変えられなかった（元に戻した）');
      expect((await trackOf(win, asmr)).guide_id || null).toBeNull();
    } finally {
      await app.evaluate(({ ipcMain }) => {
        ipcMain.removeHandler('engine.call');
        ipcMain.handle('engine.call', globalThis.__origCall);
      });
    }
  });

  test('(G6) 共通のガイドの指定もプルダウンから。共通のガイドが無いときの表示', async () => {
    const { win, idle } = ctx;
    const [main, , low] = await ids(win);
    const b0 = win.locator(`#heads .th[data-id="${main}"] button.g.wide`);
    await b0.click();
    await expect(win.locator('#menu [data-item="guide-common"] .l')).toHaveText('なし（このトラックが共通のガイド）');
    await expect(win.locator('#menu [data-item="guide"] .l')).toHaveText('共通のガイドから外す');
    await win.locator('#menu [data-item="guide"]').click();
    await win.waitForFunction(() => window.__app.S.session.guide === null);
    await idle();
    const h = await heads(win);
    expect(h.map((x) => x.guideText)).toEqual(['なし', 'なし', 'vo-hamo+3', 'なし']);   // 3 度下は自分の指定のまま
    await win.locator(`#heads .th[data-id="${main}"] button.g.wide`).click();
    await expect(win.locator('#menu [data-item="guide-common"] .l')).toHaveText('なし（共通のガイドも未指定）');
    await win.locator('#menu [data-item="guide"]').click();                               // このトラックを共通のガイドにする
    await win.waitForFunction((id) => window.__app.S.session.guide === id, main);
    await idle();
    expect((await heads(win)).map((x) => x.guideText)).toEqual(['なし', '共通（vo-main）', 'vo-hamo+3', '共通（vo-main）']);
    expect(await win.evaluate(() => window.__app.S.session.current)).toBe(low);
  });

  test('(G7) 360 px 幅: ボタンとメニューが収まる。高さを詰めると 1 段目のアイコンに替わる', async () => {
    const { app, win, idle } = ctx;
    const [, , low] = await ids(win);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(360, 760));
    await win.waitForFunction(() => window.innerWidth <= 380);
    const btn = win.locator(`#heads .th[data-id="${low}"] button.g.wide`);
    const hb = await win.locator(`#heads .th[data-id="${low}"]`).boundingBox();
    const bb = await btn.boundingBox();
    expect(bb.x).toBeGreaterThanOrEqual(hb.x);
    expect(bb.x + bb.width).toBeLessThanOrEqual(hb.x + hb.width + 0.5);
    await btn.click();
    const mb = await win.locator('#menu').boundingBox();
    const vw = await win.evaluate(() => window.innerWidth);
    const vh = await win.evaluate(() => window.innerHeight);
    expect(mb.x).toBeGreaterThanOrEqual(0);
    expect(mb.x + mb.width).toBeLessThanOrEqual(vw);
    expect(mb.y + mb.height).toBeLessThanOrEqual(vh);
    expect(await win.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await win.keyboard.press('Escape');
    // 高さを詰める（2 段目を畳む）→ 1 段目のアイコンのボタン
    await win.evaluate(() => window.__app.setTrackHeight(30));
    await expect.poll(() => win.evaluate(() => window.__app.tracksState().trackH)).toBeLessThan(40);
    await expect(win.locator(`#heads .th[data-id="${low}"] button.g.ic`)).toBeVisible();
    await expect(btn).toBeHidden();
    await win.locator(`#heads .th[data-id="${low}"] button.g.ic`).click();
    await expect(win.locator('#menu')).toBeVisible();
    await win.keyboard.press('Escape');
    await win.evaluate(() => window.__app.setTrackHeight(44));
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 800));
    await idle();
    expect(ctx.errors).toEqual([]);
  });
});

test.describe('単体', () => {
  test.describe.configure({ mode: 'serial' });
  const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-track-guide-app-'));
  let ctx;
  test.beforeAll(async () => { ctx = await launch(ROOT, { ara: false }); });
  test.afterAll(async () => { await close(ctx?.app, ROOT); });

  test('(S1) 1 段目のアイコンのボタン（M・S の左）で同じプルダウン。選ぶと set_track_guide', async () => {
    const { win, idle, files } = ctx;
    const [main, hi, low] = await ids(win);
    await win.evaluate((id) => window.__app.setGuide(id), main);
    await idle();
    await win.evaluate((id) => window.__app.selectTrack(id), low);
    await idle();
    const th = win.locator(`#heads .th[data-id="${low}"]`);
    expect(await th.evaluate((el) => [...el.querySelectorAll('button')].map((b) => b.dataset.act))).toEqual(['guide', 'm', 's']);
    const btn = th.locator('button.g.ic');
    await expect(btn).toBeVisible();                       // ホバーしなくても出ている（キーボードでも届く）
    await expect(btn).toHaveAttribute('aria-label', 'vo-hamo-3-2 のガイド: 共通のガイド（vo-main-0）');
    await btn.click();
    await win.locator(`#menu [data-item="guide-to:${hi}"]`).click();
    await win.waitForFunction(([t, g]) => window.__app.S.tracks.find((x) => x.id === t)?.effective_guide_id === g, [low, hi]);
    await idle();
    await expect(btn).toHaveAttribute('aria-label', 'vo-hamo-3-2 のガイド: vo-hamo+3-1');
    expect(await guideShownPath(win)).toBe(files[1]);
    expect(await guideRows(win)).toEqual([hi]);
    // 単体の保存（session.json）にも残る
    const s = await win.evaluate(() => window.api.call('list_tracks', {}));
    expect(s.tracks.find((t) => t.id === low).guide_id).toBe(hi);
    expect(ctx.errors).toEqual([]);
  });
});
