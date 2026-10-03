// ピッチ検出の方式（編集 > ピッチ検出の方式）。RMVPE（既定）・Gliss（試作）・Praat を選んで、開いているテイクが
// その方式で解析し直されること、選んだ方式がユーザー設定に残ることを見る。音声は合成（素材は要らない）。
// 3 方式のうち RMVPE は重みが無い環境では Gliss で代わりに解析する（メニューは「モデル未取得」で選べない）。
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

test('(F1) 編集 > ピッチ検出の方式 で Gliss・Praat に替えて解析し直し、設定に残る', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-f0-spec-'));
  const wav = path.join(dir, 'tone.wav');
  writeTone(wav);
  fs.mkdirSync(SHOTS, { recursive: true });
  const userdata = path.join(dir, 'userdata');
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_EDITOR_MUTE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_WORK_DIR: path.join(dir, 'work'),
    VOCAL_ENGINE_PROJECTS: path.join(dir, 'projects') };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_F0_ESTIMATOR;
  const app = await electron.launch({
    args: [APP, wav, '--project-dir', path.join(dir, 'project'), '--user-data-dir', userdata, '--mute'], env,
  });
  try {
    const win = await app.firstWindow();
    await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
    // vd: 開いているテイクの解析が使った方式（get_pitch）
    const f0 = () => win.evaluate(async () => ({
      ...window.__app.f0(), vd: (await window.api.call('get_pitch', {})).estimator }));
    const idle = () => win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
    const menuLabels = () => win.locator('.mbd').last().locator('.l').allTextContents();
    const start = await f0();
    expect(start.chosen).toBe('rmvpe');                 // 既定は RMVPE のまま

    const pick = async (label) => {
      await win.locator('#menubar button', { hasText: '編集' }).click();
      await win.locator('.mbd button', { hasText: 'ピッチ検出の方式' }).click();
      await win.locator('.mbd button', { hasText: label }).last().click();
      await idle();
    };

    // 開いてみる: 3 つの選択肢が並び、今使っている方式にチェックが付く
    await win.locator('#menubar button', { hasText: '編集' }).click();
    await win.locator('.mbd button', { hasText: 'ピッチ検出の方式' }).click();
    expect((await menuLabels()).filter((l) => /RMVPE|Gliss（試作）|^Praat$/.test(l)).length).toBe(3);
    await expect(win.locator('.mbd').last().locator('[aria-checked="true"]')).toHaveCount(1);
    await win.screenshot({ path: path.join(SHOTS, 'f0-menu.png') });
    await win.keyboard.press('Escape');
    await win.keyboard.press('Escape');

    await pick('Gliss（試作）');
    await expect.poll(async () => (await f0()).vd).toBe('gliss');
    expect((await f0()).effective).toBe('gliss');
    await win.screenshot({ path: path.join(SHOTS, 'f0-gliss.png') });

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
    const saved = JSON.parse(fs.readFileSync(path.join(userdata, 'state.json'), 'utf8'));
    expect(saved.f0Estimator).toBe('praat');
    await win.evaluate(() => window.api.setDoc({ name: 'test', dirty: false })).catch(() => {});
  } finally {
    await Promise.race([app.close(), new Promise((r) => setTimeout(r, 15000))]);
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  }
});
