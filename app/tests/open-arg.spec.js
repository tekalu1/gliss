// `Gliss <WAV>`（オプションでない最初の引数の音声ファイル）でテイクとして開く。DAW の外部エディタの形。
// 音声は合成するので素材は要らない（解析モデルの重みが無くても、トラックとして開くところまでを見る）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function writeTone(file, sr = 48000, sec = 2) {
  const n = sr * sec;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const env = t < 0.3 || t > 1.7 ? 0 : 0.4;
    buf.writeInt16LE(Math.round(env * Math.sin(2 * Math.PI * 220 * t) * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

test('(O1) Gliss <WAV> でそのファイルをテイクとして開く', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-open-arg-'));
  const wav = path.join(dir, 'テイク 1.wav');
  writeTone(wav);
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_EDITOR_MUTE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_PROJECTS: path.join(dir, 'projects') };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({
    args: [APP, wav, '--project-dir', path.join(dir, 'project'), '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env,
  });
  try {
    const win = await app.firstWindow();
    await win.waitForFunction(() => window.__app?.S.tracks.length >= 1, null, { timeout: 60000 });
    const tracks = await win.evaluate(() => window.__app.tracks());
    expect(tracks.map((t) => [t.name, path.resolve(t.path)])).toEqual([['テイク 1', path.resolve(wav)]]);
    await win.evaluate(() => window.api.setDoc({ name: 'test', dirty: false })).catch(() => {});
  } finally {
    await Promise.race([app.close(), new Promise((r) => setTimeout(r, 15000))]);
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  }
});
