// 起動引数（app/args.mjs）。`Gliss.exe <WAV>` を `--take <WAV>` と同じに扱う。
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from '../../args.mjs';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-args-'));
const wav = path.join(dir, 'テイク 1.wav');
const flac = path.join(dir, 'take.FLAC');
const txt = path.join(dir, 'notes.txt');
for (const f of [wav, flac, txt]) fs.writeFileSync(f, '');
test.afterAll(() => fs.rmSync(dir, { recursive: true, force: true }));

test('(A1) オプションでない最初の音声ファイルは --take と同じ', () => {
  expect(parseArgs([wav])).toEqual({ take: wav });
  expect(parseArgs([flac]).take).toBe(flac);                       // 拡張子の大小は問わない
  // 開発版: electron.exe <app のフォルダ> <WAV>。フォルダは飛ばす
  expect(parseArgs([dir, '--mute', wav]).take).toBe(wav);
  // 相対パスは絶対パスにする
  const rel = path.relative(process.cwd(), wav);
  expect(parseArgs([rel]).take).toBe(path.resolve(rel));
});

test('(A2) --take が優先・音声でない／無いファイルは無視・値のあるオプションの値は引数に数えない', () => {
  expect(parseArgs(['--take', flac, wav]).take).toBe(flac);
  expect(parseArgs([txt])).toEqual({});
  expect(parseArgs([path.join(dir, 'missing.wav')])).toEqual({});
  expect(parseArgs(['--project-dir', wav])).toEqual({ projectDir: wav });
  expect(parseArgs(['--remote-debugging-port=0', '--user-data-dir=' + dir, wav]))
    .toEqual({ userDataDir: dir, take: wav });
  expect(parseArgs([wav, flac]).take).toBe(wav);                   // 最初の 1 つだけ
});
