// トラック一覧を畳んで開ける（docs/track-view.md §10。2026-10-10 承認）。素材・解析モデルの重みは要らない（合成の歌）。
//
//   プラグイン（GLISS_TEST_ARA=1。既定は畳んだ 24 px の帯）:
//   (L1) 既定は畳む: 帯は「▸ トラック」・編集中のトラックの名前（DAW のトラック名つき）・ガイドのプルダウンだけ。件数・失敗や読み込みの印は出さない
//   (L2) ボタン（キーボードの Enter も）で開く・畳む。aria-expanded・一覧の見え方・開いた高さ
//   (L3) L キーで開閉。入力欄の中では効かない。表示メニューの「トラック一覧を表示」のチェックが追従し、メニューからも開閉できる
//   (L4) 帯のガイドのプルダウン: 帯のボタンを起点に開く・Esc で戻る・もう一度押すと閉じる・選ぶと帯の表示が替わる
//   (L5) 動き: 約 180 ms の高さの変化。prefers-reduced-motion では動かさない
//   (L6) 窓が低くても 480 px の下限で縮まず、実際の高さの 40%。開くと編集中の行が見える位置まで送る
//   (L7) 幅 360 px でも帯が収まる
//   (L8) 帯のつかみ（上下の境）: ダブルクリックで開閉・畳んだ帯から引くと開く
//   (L9) 開閉は PC 全体で 1 つ覚える（開き直しても残る）
//   単体（Electron）:
//   (L10) 初期は開いている。L・ボタンで畳める
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/** 倍音のある合成の歌。notes: [開始秒, 終わり秒, MIDI]。 */
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

const PARTS = [['vo-main', 'Vo Main', 0], ['vo-hamo+3', 'Vo Hamo +3', 4], ['vo-hamo-3', 'Vo Hamo -3', -3], ['vo-asmr', 'Vo ASMR', 0]];

async function launch(root, { ara, size = [1280, 800] }) {
  const files = PARTS.map(([n, , d], i) => {
    const f = path.join(root, `${n.replace(/[^\w+-]/g, '_')}-${i}.wav`);
    if (!fs.existsSync(f)) writeSong(f, [[0.4, 1.4, 64 + d], [1.8, 2.8, 67 + d], [3.2, 4.4, 69 + d]]);
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
  await app.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setSize(w, h), size);
  if (ara) {
    await win.evaluate(async ([fs_, parts]) => {
      const c = async (n, a) => { const r = await window.api.call(n, a); if (r.ok === false) throw new Error(`${n}: ${r.error}`); return r; };
      await c('ara_open', { work_key: 'track-list-spec', name: 'track list' });
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
  await app?.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 1 }); }).catch(() => {});
  await app?.close();
  fs.rmSync(root, { recursive: true, force: true, maxRetries: 5 });
}

const state = (win) => win.evaluate(() => {
  const s = window.__app.tracksState();
  return { open: s.open, height: s.height ?? null };
});
const tvH = (win) => win.evaluate(() => document.querySelector('#tv').getBoundingClientRect().height);
const isClosed = (win) => win.evaluate(() => document.querySelector('#tv').classList.contains('closed'));
/** 開閉の動きが終わるまで待つ（高さの変化の間だけ #tv に anim が付く）。 */
const settled = (win) => win.waitForFunction(() => !document.querySelector('#tv').classList.contains('anim'));
const setWindow = (app, w, h) => app.evaluate(({ BrowserWindow }, [a, b]) => BrowserWindow.getAllWindows()[0].setSize(a, b), [w, h]);
const viewItem = (win) => win.evaluate(() => {
  const v = window.__app.menubar().model.find((m) => m.label.startsWith('表示'));
  const it = v?.submenu.find((x) => x.label === 'トラック一覧を表示');
  return it ? { checked: !!it.checked, enabled: it.enabled !== false, accel: it.accelerator || null } : null;
});
const checkedNow = (win) => win.evaluate(() => {
  const v = window.__app.menubar().model.find((m) => m.label.startsWith('表示'));
  return !!v?.submenu.find((x) => x.label === 'トラック一覧を表示')?.checked;
});

test.describe('プラグイン', () => {
  test.describe.configure({ mode: 'serial' });
  const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-track-list-ara-'));
  let ctx;
  test.beforeAll(async () => { ctx = await launch(ROOT, { ara: true }); });
  test.afterAll(async () => { await close(ctx?.app, ROOT); });

  test('(L1) 既定は畳む: 帯は「トラック」のボタン・編集中の名前・ガイドのプルダウンだけ', async () => {
    const { win } = ctx;
    expect(await isClosed(win)).toBe(true);
    expect(await tvH(win)).toBeCloseTo(24, 0);
    const toggle = win.locator('#tvToggle');
    await expect(toggle).toHaveText('トラック');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toHaveAttribute('aria-controls', 'tvRows');
    const cur = await win.evaluate(() => {
      const S = window.__app.S;
      const t = S.tracks.find((x) => x.id === S.session.current);
      return { id: t.id, name: t.name, group: t.group };
    });
    const strip = win.locator('#tvStrip');
    await expect(strip).toBeVisible();
    await expect(strip.locator('.st b')).toHaveText(cur.name);
    await expect(strip.locator('.st i')).toHaveText(`（DAW: ${cur.group}）`);
    const g = strip.locator('button.g');
    await expect(g).toHaveCount(1);
    await expect(g).toBeVisible();
    await expect(g).toHaveAttribute('aria-haspopup', 'menu');
    await expect(g).toHaveAttribute('data-id', cur.id);
    // 件数・失敗や読み込みの印は帯に出さない
    const txt = (await strip.innerText()).replace(/\s+/g, ' ');
    expect(txt).not.toMatch(/[!！◌]/);
    expect(txt).not.toMatch(/\d+\s*(本|トラック)/);
    expect(await win.locator('#tvToggle').innerText()).not.toMatch(/\d/);
    // 畳んでいる間、行は見えず、キーボードの移動にも入らない
    await expect(win.locator('#tvRows')).toBeHidden();
    expect(await win.evaluate(() => document.querySelector('#tvRows').inert)).toBe(true);
    await expect(win.locator('#heads .th').first()).toBeHidden();
  });

  test('(L2) ボタン（Enter も）で開く・畳む', async () => {
    const { win } = ctx;
    const n = await win.evaluate(() => window.__app.S.tracks.length);
    await win.locator('#tvToggle').click();
    await settled(win);
    expect(await isClosed(win)).toBe(false);
    await expect(win.locator('#tvToggle')).toHaveAttribute('aria-expanded', 'true');
    await expect(win.locator('#heads .th')).toHaveCount(n);
    await expect(win.locator('#heads .th').first()).toBeVisible();
    expect(await win.evaluate(() => document.querySelector('#tvRows').inert)).toBe(false);
    expect(await tvH(win)).toBeGreaterThan(24 + 40);
    // 開いている間、ルーラーの上の帯のガイドは見せない（見出しの 2 段目にある）
    await expect(win.locator('#tvStrip')).toBeHidden();          // Tab でも入らない（透明なだけにしない）
    await win.locator('#tvToggle').focus();
    await win.keyboard.press('Enter');
    await settled(win);
    expect(await isClosed(win)).toBe(true);
    expect(await tvH(win)).toBeCloseTo(24, 0);
    await expect(win.locator('#tvToggle')).toBeFocused();
  });

  test('(L3) L キーで開閉・入力欄の中では効かない・表示メニューのチェックと項目', async () => {
    const { win } = ctx;
    await expect.poll(() => viewItem(win)).toEqual({ checked: false, enabled: true, accel: expect.stringMatching(/^L$/i) });   // メニューの送りは 60 ms のデバウンス
    await win.locator('#roll').hover();
    await win.keyboard.press('l');
    await settled(win);
    expect(await isClosed(win)).toBe(false);
    await expect.poll(() => checkedNow(win)).toBe(true);
    // 入力欄の中の L は文字（開閉しない）
    await win.evaluate(() => {
      const i = document.createElement('input');
      i.id = 'tmpInput';
      document.body.appendChild(i);
      i.focus();
    });
    await win.keyboard.press('l');
    expect(await isClosed(win)).toBe(false);
    expect(await win.locator('#tmpInput').inputValue()).toBe('l');
    await win.evaluate(() => document.querySelector('#tmpInput').remove());
    // 表示メニューの項目（コマンド）からも畳める
    await win.evaluate(() => window.__app.runCommand('track-list'));
    await settled(win);
    expect(await isClosed(win)).toBe(true);
    await expect.poll(() => checkedNow(win)).toBe(false);
    // 押したキーはそのまま DAW に渡さない（Gliss が使う）
    const defaultPrevented = await win.evaluate(() => {
      const ev = new KeyboardEvent('keydown', { key: 'l', code: 'KeyL', bubbles: true, cancelable: true });
      document.querySelector('#roll').dispatchEvent(ev);
      return ev.defaultPrevented;
    });
    expect(defaultPrevented).toBe(true);
    await settled(win);
    await win.keyboard.press('l');          // 後の試験のために畳んだ状態へ戻す
    await settled(win);
    expect(await isClosed(win)).toBe(true);
  });

  test('(L4) 帯のガイドのプルダウン: 帯のボタンを起点に開く・Esc で戻る・押し直しで閉じる・選ぶと帯が替わる', async () => {
    const { win, idle } = ctx;
    const btn = win.locator('#tvStrip button.g');
    const before = await btn.locator('.gt').innerText();
    await btn.click();
    await expect(win.locator('#menu')).toBeVisible();
    const items = win.locator('#menu [role="menuitemradio"]');
    expect(await items.count()).toBeGreaterThan(1);
    await expect(btn).toHaveAttribute('aria-expanded', 'true');
    const bb = await btn.boundingBox();
    const mb = await win.locator('#menu').boundingBox();
    expect(mb.y).toBeGreaterThanOrEqual(bb.y + bb.height - 2);        // 帯のボタンの下に開く
    expect(Math.abs(mb.x - bb.x)).toBeLessThan(40);
    await win.keyboard.press('Escape');
    await expect(win.locator('#menu')).toBeHidden();
    await expect(btn).toBeFocused();
    await expect(btn).toHaveAttribute('aria-expanded', 'false');
    // 押し直しで開いて、もう一度押すと閉じる（開き直さない）
    await btn.click();
    await expect(win.locator('#menu')).toBeVisible();
    await btn.click();
    await expect(win.locator('#menu')).toBeHidden();
    // 選ぶと set_track_guide。帯のボタンの表示が替わる（閉じた直後の 0.5 秒は同じボタンで開き直さない）
    await win.waitForTimeout(600);
    await btn.click();
    await expect(win.locator('#menu')).toBeVisible();
    const pick = win.locator('#menu [role="menuitemradio"][aria-checked="false"]').first();
    const label = (await pick.innerText()).trim();
    await pick.click();
    await idle();
    await expect(win.locator('#menu')).toBeHidden();
    await expect.poll(async () => (await btn.locator('.gt').innerText()).trim()).not.toBe(before.trim());
    expect(label.length).toBeGreaterThan(0);
    // 畳んだままで、帯は編集中のトラックのまま
    expect(await isClosed(win)).toBe(true);
    // 元に戻す（共通のガイドの指定はセッションに残るので Ctrl+Z）
    await win.evaluate(() => window.__app.runCommand('undo'));
    await idle();
  });

  test('(L5) 動き: 約 180 ms の高さの変化・reduced-motion では動かさない', async () => {
    const { win } = ctx;
    await win.emulateMedia({ reducedMotion: 'no-preference' });
    await win.locator('#tvToggle').click();
    expect(await win.evaluate(() => document.querySelector('#tv').classList.contains('anim'))).toBe(true);
    const dur = await win.evaluate(() => getComputedStyle(document.querySelector('#tv')).transitionDuration);
    expect(parseFloat(dur)).toBeCloseTo(0.18, 2);
    await settled(win);
    await win.locator('#tvToggle').click();
    await settled(win);
    expect(await isClosed(win)).toBe(true);
    await win.emulateMedia({ reducedMotion: 'reduce' });
    await win.locator('#tvToggle').click();
    expect(await win.evaluate(() => document.querySelector('#tv').classList.contains('anim'))).toBe(false);
    expect(await win.evaluate(() => getComputedStyle(document.querySelector('#tv')).transitionDuration)).toBe('0s');
    await win.locator('#tvToggle').click();
    expect(await isClosed(win)).toBe(true);
    await win.emulateMedia({ reducedMotion: 'no-preference' });
  });

  test('(L6) 窓が低くても 480 px の下限で縮まず、実際の高さの 40%。開くと編集中の行まで送る', async () => {
    const { win, app } = ctx;
    const ids = await win.evaluate(() => window.__app.S.tracks.map((t) => t.id));
    for (const h of [600, 400, 300]) {
      await setWindow(app, 1280, h);
      await win.waitForFunction((hh) => window.innerHeight <= hh, h);
      await win.evaluate(() => { if (document.querySelector('#tv').classList.contains('closed')) window.__app.runCommand('track-list'); });
      await settled(win);
      const m = await win.evaluate(() => {
        const root = document.querySelector('#tv').parentElement;
        const tb = root.querySelector('.tb')?.getBoundingClientRect().height || 36;
        const st = document.querySelector('#status')?.getBoundingClientRect().height || 18;
        const sp = document.querySelector('#split').getBoundingClientRect().height;
        const s = window.__app.tracksState();
        return { avail: root.getBoundingClientRect().height - tb - st - sp, tv: document.querySelector('#tv').getBoundingClientRect().height,
          th: s.trackH, roll: document.querySelector('#roll').getBoundingClientRect().height };
      });
      const floor = 24 + m.th;                                   // 1 行は残す
      expect(m.tv).toBeLessThanOrEqual(Math.max(floor, m.avail * 0.4) + 1);
      expect(m.tv).toBeGreaterThanOrEqual(floor - 1);
      expect(m.roll).toBeGreaterThan(60);                          // エディターが潰れない
      if (h === 300) expect(m.tv).toBeLessThan(m.avail * 0.6);   // 480 を下限にしていた頃は窓の高さを超えて広がった
      await win.evaluate(() => window.__app.runCommand('track-list'));
      await settled(win);
    }
    // 窓が低くて一覧に全部は入らない: 最後のトラックを編集中にして開くと、その行が見える
    await win.evaluate((id) => window.__app.selectTrack(id), ids[ids.length - 1]);
    await ctx.idle();
    await win.evaluate(() => window.__app.runCommand('track-list'));
    await settled(win);
    const vis = await win.evaluate(() => {
      const body = document.querySelector('#tvBody').getBoundingClientRect();
      const cur = document.querySelector(`#heads .th[data-id="${window.__app.S.session.current}"]`)?.getBoundingClientRect();
      return cur ? { top: cur.top - body.top, bottom: body.bottom - cur.bottom, scroll: document.querySelector('#tvBody').scrollTop } : null;
    });
    expect(vis).not.toBeNull();
    expect(vis.scroll).toBeGreaterThan(0);
    expect(vis.top).toBeGreaterThanOrEqual(24 - 1);                 // ルーラーの下に隠れない
    expect(vis.bottom).toBeGreaterThanOrEqual(-1);
    await win.evaluate(() => window.__app.runCommand('track-list'));
    await settled(win);
    await setWindow(app, 1280, 800);
    await win.waitForFunction(() => window.innerHeight > 700);
  });

  test('(L7) 幅 360 px でも帯が収まる', async () => {
    const { win, app } = ctx;
    await setWindow(app, 360, 700);
    await win.waitForFunction(() => window.innerWidth <= 360);
    expect(await isClosed(win)).toBe(true);
    const m = await win.evaluate(() => {
      const tv = document.querySelector('#tv').getBoundingClientRect();
      const g = document.querySelector('#tvStrip button.g').getBoundingClientRect();
      const t = document.querySelector('#tvToggle').getBoundingClientRect();
      const s = document.querySelector('#tvStrip');
      return { tvR: tv.right, gR: g.right, gL: g.left, tR: t.right, sw: s.scrollWidth, cw: s.clientWidth };
    });
    expect(m.gR).toBeLessThanOrEqual(m.tvR + 0.5);
    expect(m.gL).toBeGreaterThanOrEqual(m.tR - 0.5);
    expect(m.sw).toBeLessThanOrEqual(m.cw + 1);
    await win.locator('#tvStrip button.g').click();
    const mb = await win.locator('#menu').boundingBox();
    expect(mb.x).toBeGreaterThanOrEqual(0);
    expect(mb.x + mb.width).toBeLessThanOrEqual(360 + 0.5);
    await win.keyboard.press('Escape');
    await setWindow(app, 1280, 800);
    await win.waitForFunction(() => window.innerWidth > 1000);
  });

  test('(L8) 上下の境: ダブルクリックで開閉・畳んだ帯から引くと開く', async () => {
    const { win } = ctx;
    const center = async () => {
      const b = await win.locator('#split').boundingBox();
      return [b.x + b.width / 2, b.y + b.height / 2];
    };
    let [x, y] = await center();
    await win.mouse.dblclick(x, y);
    await settled(win);
    expect(await isClosed(win)).toBe(false);
    const h0 = await tvH(win);
    expect(h0).toBeGreaterThan(24 + 40);
    [x, y] = await center();
    await win.mouse.dblclick(x, y);
    await settled(win);
    expect(await isClosed(win)).toBe(true);
    expect(await tvH(win)).toBeCloseTo(24, 0);
    // 畳んだ帯から引くと、その場で開いて（動きなし）引いた分だけ高くなる
    [x, y] = await center();
    await win.mouse.move(x, y);
    await win.mouse.down();
    await win.mouse.move(x, y + 30, { steps: 4 });
    expect(await win.evaluate(() => document.querySelector('#tv').classList.contains('anim'))).toBe(false);
    await win.mouse.move(x, y + 90, { steps: 6 });
    await win.mouse.up();
    expect(await isClosed(win)).toBe(false);
    expect(await tvH(win)).toBeGreaterThan(24 + 40);
    // ダブルクリックで畳み、もう一度で自動の高さへ戻る
    [x, y] = await center();
    await win.mouse.dblclick(x, y);
    await settled(win);
    expect(await isClosed(win)).toBe(true);
    [x, y] = await center();
    await win.mouse.dblclick(x, y);
    await settled(win);
    expect(Math.abs((await tvH(win)) - h0)).toBeLessThan(2);
    await win.evaluate(() => window.__app.runCommand('track-list'));
    await settled(win);
    expect(await isClosed(win)).toBe(true);
  });

  test('(L9) 開閉は PC 全体で 1 つ覚える（開き直しても残る）', async () => {
    const stateFile = path.join(ROOT, 'userdata', 'state.json');
    const saved = () => JSON.parse(fs.readFileSync(stateFile, 'utf8')).view?.tvOpen;
    const reopen = async () => {
      await ctx.app.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 1 }); }).catch(() => {});
      await ctx.app.close();
      ctx = await launch(ROOT, { ara: true });
    };
    await ctx.win.locator('#tvToggle').click();
    await settled(ctx.win);
    expect(await ctx.win.evaluate(() => window.__app.tracksState().open)).toBe(true);
    await expect.poll(() => { try { return saved(); } catch { return null; } }).toBe(true);   // 表示の設定として書く（少し間を置いてから）
    await reopen();
    expect(await isClosed(ctx.win)).toBe(false);
    await expect(ctx.win.locator('#tvToggle')).toHaveAttribute('aria-expanded', 'true');
    await expect.poll(() => checkedNow(ctx.win)).toBe(true);        // メニューの送りは 60 ms のデバウンス
    await ctx.win.locator('#tvToggle').click();
    await settled(ctx.win);
    await expect.poll(saved).toBe(false);
    await reopen();
    expect(await isClosed(ctx.win)).toBe(true);        // 畳んだ状態も残る
    await expect.poll(() => checkedNow(ctx.win)).toBe(false);
    expect(ctx.errors).toEqual([]);
  });
});

test.describe('単体', () => {
  test.describe.configure({ mode: 'serial' });
  const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-track-list-app-'));
  let ctx;
  test.beforeAll(async () => { ctx = await launch(ROOT, { ara: false }); });
  test.afterAll(async () => { await close(ctx?.app, ROOT); });

  test('(L10) 初期は開いている。L・ボタンで畳める。畳んだ帯の編集中の名前には DAW の名前を添えない', async () => {
    const { win } = ctx;
    expect(await isClosed(win)).toBe(false);
    expect(await tvH(win)).toBeGreaterThan(24 + 40);
    await expect(win.locator('#tvToggle')).toHaveAttribute('aria-expanded', 'true');
    expect(await checkedNow(win)).toBe(true);
    await win.locator('#roll').hover();
    await win.keyboard.press('l');
    await settled(win);
    expect(await isClosed(win)).toBe(true);
    await expect(win.locator('#tvStrip .st b')).toHaveText(await win.evaluate(() => {
      const S = window.__app.S;
      return S.tracks.find((t) => t.id === S.session.current).name;
    }));
    await expect(win.locator('#tvStrip .st i')).toHaveCount(0);
    await win.locator('#tvToggle').click();
    await settled(win);
    expect(await isClosed(win)).toBe(false);
  });

  test('エラーが出ていない', async () => {
    expect(ctx.errors).toEqual([]);
  });
});
