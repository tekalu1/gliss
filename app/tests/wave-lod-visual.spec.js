import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repo = path.dirname(appDir);
const root = path.join(repo, 'output', 'wave-lod-visual');
const shots = path.join(repo, 'output');

function writeStereo(file, phase) {
  const sr = 48000, frames = sr * 4;
  const buf = Buffer.alloc(44 + frames * 4);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(2, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 4, 28);
  buf.writeUInt16LE(4, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(frames * 4, 40);
  for (let i = 0; i < frames; i++) {
    const t = i / sr;
    const env = t < 0.25 || (t > 1.4 && t < 1.65) ? 0 : 0.35 + 0.4 * Math.abs(Math.sin(t * 2.2));
    const a = env * (0.7 * Math.sin(2 * Math.PI * (180 + t * 20) * t + phase) + 0.15);
    const b = env * (0.55 * Math.sin(2 * Math.PI * 269 * t + phase + 0.8) - 0.12);
    buf.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(a * 32767))), 44 + i * 4);
    buf.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(b * 32767))), 46 + i * 4);
  }
  fs.writeFileSync(file, buf);
}

test('signed waveform stays detailed at three zoom levels', async () => {
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(shots, { recursive: true });
  const take = path.join(root, 'vocal.wav');
  const inst = path.join(root, 'Inst_mix.wav');
  writeStereo(take, 0); writeStereo(inst, 0.5);
  const env = { ...process.env, VOCAL_EDITOR_MUTE: '1', VOCAL_EDITOR_IGNORE_MOUSE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_PROJECTS: path.join(root, 'projects'),
    ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({ args: [appDir, '--take', take, '--project-dir', path.join(root, 'project'),
    '--user-data-dir', path.join(root, 'userdata'), '--mute'], env });
  try {
    const win = await app.firstWindow();
    await win.waitForFunction(() => window.__app?.S.tracks.length >= 1,
      null, { timeout: 30000 });
    await win.evaluate(async (p) => {
      if (window.__app.S.tracks.length === 1) {
        await window.api.call('add_track', { path: p });
        await window.__app.loadSession();
      }
    }, inst);
    await win.waitForFunction(() => window.__app.tracksState().overviews === 2,
      null, { timeout: 120000 });
    for (const [name, view] of [['full', null], ['middle', { t0: 0.8, span: 2 }],
      ['max', { t0: 0.8, span: 0.5 }]]) {
      await win.evaluate((v) => window.__app.setTracksView(v), view);
      const paths = await win.locator('#lanes path[transform]').evaluateAll((els) =>
        els.map((el) => el.getAttribute('d')));
      expect(paths.length).toBe(3); // mono vocal and two separate accompaniment channels
      expect(paths.every((d) => d.includes('L') && d.endsWith('Z'))).toBe(true);
      const points = paths[0].match(/-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?/g)
        .map((s) => s.split(',').map(Number));
      expect(points.length).toBeLessThan(4000);
      const half = points.length / 2;
      // A positive DC offset makes min/max unequal around the vocal centre (22.5 px).
      expect(points.slice(0, half).some((p, i) =>
        Math.abs(p[1] + points[points.length - 1 - i][1] - 45) > 1)).toBe(true);
      if (name === 'max') {
        const xs = points.slice(0, half).map((p) => p[0]);
        expect(xs.some((x) => Math.abs(x - Math.round(x)) > 0.01)).toBe(true);
        expect(Math.max(...xs.slice(1).map((x, i) => x - xs[i]))).toBeLessThan(4);
      }
      await win.screenshot({ path: path.join(shots, `wave-lod-${name}.png`) });
    }
  } finally {
    await app.close();
    if (!path.resolve(root).startsWith(path.resolve(repo) + path.sep)) throw new Error('fixture path outside worktree');
    fs.rmSync(root, { recursive: true, force: true });
  }
});
