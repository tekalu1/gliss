// ピッチ検出の方式（編集 > ピッチ検出の方式）。RMVPE（既定）・Gliss・Praat を選んで、開いているテイクが
// その方式で解析し直されること、選んだ方式がユーザー設定に残ること、選んでいなければ曲ごとに前に解析した方式の
// ままになることを見る。音声は合成（素材は要らない）。
// RMVPE は重みが無い環境では Gliss で代わりに解析する（メニューは「RMVPE（既定・モデル未取得）」で選べず、チェックは Gliss）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SHOTS = process.env.GLISS_SCREENSHOT_DIR || path.join(os.tmpdir(), 'gliss-f0-estimator-shots');

/** 前半 220 Hz・後半 294 Hz（5 半音上）の倍音のある音。最初と最後の 0.3 秒は無音。 */
function writeTone(file, sr = 44100, sec = 3) {
  const n = sr * sec;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  let ph = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    ph += 2 * Math.PI * (t < sec / 2 ? 220 : 220 * 2 ** (5 / 12)) / sr;
    let x = 0;
    for (let k = 1; k <= 6; k++) x += Math.sin(k * ph) / k;
    const env = t < 0.3 || t > sec - 0.3 ? 0 : 0.25;
    buf.writeInt16LE(Math.round(env * x * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

test('(F1) 編集 > ピッチ検出の方式 で替えて解析し直し、設定に残る。選んでいなければ曲は前の方式のまま', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-f0-spec-'));
  const wav = path.join(dir, 'tone.wav');
  writeTone(wav);
  fs.mkdirSync(SHOTS, { recursive: true });
  const base = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_EDITOR_MUTE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_WORK_DIR: path.join(dir, 'work'),
    VOCAL_ENGINE_PROJECTS: path.join(dir, 'projects') };
  delete base.ELECTRON_RUN_AS_NODE;
  delete base.GLISS_F0_ESTIMATOR;
  let app = null;
  let win = null;
  const launch = async ({ name = 'a', env = base } = {}) => {
    app = await electron.launch({
      args: [APP, wav, '--project-dir', path.join(dir, `project-${name}`), '--user-data-dir', path.join(dir, `userdata-${name}`),
        '--mute'], env,
    });
    win = await app.firstWindow();
    await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
    return win;
  };
  const close = async () => {
    await win.evaluate(() => window.api.setDoc({ name: 'test', dirty: false })).catch(() => {});
    await Promise.race([app.close(), new Promise((r) => setTimeout(r, 15000))]);
    app = null;
  };
  // vd: 開いているテイクの解析が使った方式（get_pitch）
  const f0 = () => win.evaluate(async () => ({
    ...window.__app.f0(), vd: (await window.api.call('get_pitch', {})).estimator }));
  const idle = () => win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
  const menu = () => win.locator('.mbd').last();
  const menuLabels = () => menu().locator('.l').allTextContents();
  const openMenu = async () => {
    await win.locator('#menubar button', { hasText: '編集' }).click();
    await win.locator('.mbd button', { hasText: 'ピッチ検出の方式' }).click();
  };
  const closeMenu = async () => {
    await win.keyboard.press('Escape');
    await win.keyboard.press('Escape');
  };
  const checked = () => menu().locator('[aria-checked="true"]');
  const pick = async (label) => {
    await openMenu();
    await menu().locator('button', { has: win.locator('.l', { hasText: new RegExp(`^${label}$`) }) }).click();
    await idle();
  };
  const stateFile = path.join(dir, 'userdata-a', 'state.json');
  try {
    await launch();
    const start = await f0();
    expect(start.chosen).toBe('rmvpe');                 // 既定は RMVPE
    const first = start.rmvpe ? 'rmvpe' : 'gliss';      // RMVPE の重みが無い環境では Gliss で解析する
    expect(start.effective).toBe(first);
    expect(start.vd).toBe(first);

    // 開いてみる: 3 つの選択肢が並び（RMVPE が先頭）、今使っている方式にチェックが付く
    await openMenu();
    const labels = (await menuLabels()).filter((l) => /^RMVPE（既定|^Gliss$|^Praat$/.test(l));
    expect(labels).toEqual([start.rmvpe ? 'RMVPE（既定）' : 'RMVPE（既定・モデル未取得）', 'Gliss', 'Praat']);
    await expect(checked()).toHaveCount(1);
    await expect(checked()).toContainText(start.rmvpe ? 'RMVPE（既定）' : 'Gliss');
    await win.screenshot({ path: path.join(SHOTS, 'f0-menu.png') });
    await closeMenu();

    await pick('Praat');
    await expect.poll(async () => (await f0()).vd).toBe('praat');
    expect((await f0()).effective).toBe('praat');
    // 解析し直した F0 は合成音の音高（220 Hz = MIDI 57、294 Hz = MIDI 62）に近い
    const mids = await win.evaluate(() => {
      const v = window.__app.origCurve();
      const at = (s) => v[Math.round((s - window.__app.f0Frame().t0) / window.__app.f0Frame().hop)];
      return [at(1.0), at(2.0)];
    });
    expect(Math.abs(mids[0] - 57)).toBeLessThan(0.5);
    expect(Math.abs(mids[1] - 62)).toBeLessThan(0.5);
    await win.screenshot({ path: path.join(SHOTS, 'f0-praat.png') });

    // 選んだ方式はユーザー設定に残る（次の起動でエンジンへ渡す）
    expect(JSON.parse(fs.readFileSync(stateFile, 'utf8')).f0Estimator).toBe('praat');
    await close();

    // 方式を選んでいない状態（RMVPE の重みを後から取った・既定が替わった版で開くのと同じ）: この曲は前の方式のまま
    const st = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    delete st.f0Estimator;
    fs.writeFileSync(stateFile, JSON.stringify(st));
    await launch();
    await idle();
    const again = await f0();
    expect(again.vd).toBe('praat');
    expect(again.chosen).toBe('rmvpe');
    await expect.poll(async () => (await f0()).effective).toBe('praat');
    await openMenu();
    await expect(checked()).toHaveCount(1);
    await expect(checked()).toContainText('Praat');
    await closeMenu();

    await pick('Gliss');
    await expect.poll(async () => (await f0()).vd).toBe('gliss');
    expect((await f0()).effective).toBe('gliss');
    await win.screenshot({ path: path.join(SHOTS, 'f0-gliss.png') });
    expect(JSON.parse(fs.readFileSync(stateFile, 'utf8')).f0Estimator).toBe('gliss');
    await close();

    // RMVPE の重みが無い（初回の画面で取らなかった）: 既定の RMVPE の代わりに Gliss で解析し、チェックは Gliss。
    // RMVPE は「モデル未取得」で選べない
    const empty = path.join(dir, 'no-models');
    fs.mkdirSync(empty);
    const noModels = { ...base, VOCAL_ENGINE_MODELS_DIR: empty };
    delete noModels.VOCAL_ENGINE_MODELS;
    await launch({ name: 'b', env: noModels });
    await idle();
    const none = await f0();
    expect(none).toMatchObject({ chosen: 'rmvpe', effective: 'gliss', rmvpe: false, vd: 'gliss' });
    await openMenu();
    const rmvpe = menu().locator('button', { hasText: 'RMVPE' });
    await expect(rmvpe).toContainText('RMVPE（既定・モデル未取得）');
    await expect(rmvpe).toHaveAttribute('aria-disabled', 'true');
    await expect(checked()).toHaveCount(1);
    await expect(checked()).toContainText('Gliss');
    await win.screenshot({ path: path.join(SHOTS, 'f0-menu-no-rmvpe.png') });
    await closeMenu();
    await close();
  } finally {
    if (app) await Promise.race([app.close(), new Promise((r) => setTimeout(r, 15000))]);
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  }
});
