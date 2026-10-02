// 配布版と開発版で場所が変わるもの（paths.mjs）。Electron は起動しない（ファイルを作って関数を直接呼ぶ）。
//
//   (P1) 配布版: resources/engine/vocal-engine/vocal-engine.exe を引数なしで起動し、.mcp.json は読まない
//   (P2) 開発版: .mcp.json があればそれ（VOCAL_ENGINE_CWD で cwd だけ差し替え）
//   (P3) 開発版: .mcp.json が無ければ <repo>/.venv の python で `-m vocal_engine.mcp`（cwd は engine/）
//   (P4) 画面が起動したエンジンの印（GLISS_CLIENT・GLISS_BRIDGE）はどの経路でも付く
//   (P5) AI に登録する配布版の command は、画面が起動する exe と同じパス
//   (P6) アイコンの置き場: 配布版は resources/assets、開発版は <repo>/assets
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { serverConfig } from '../../ai-connect.mjs';
import { assetsDir, engineLaunch, packagedEngineExe } from '../../paths.mjs';

let tmp;
test.beforeEach(() => { tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-paths-')); });
test.afterEach(() => { fs.rmSync(tmp, { recursive: true, force: true }); });

const BRIDGE = 'B:\\bridge.json';

test('(P1) 配布版は同梱の exe を起動し、.mcp.json は読まない', () => {
  const repo = path.join(tmp, 'repo');
  fs.mkdirSync(repo);
  fs.writeFileSync(path.join(repo, '.mcp.json'), '{ これは JSON ではない');   // 読んだら落ちる
  const res = path.join(tmp, 'resources');
  const l = engineLaunch({ packaged: true, resourcesDir: res, repo, bridge: BRIDGE, env: { PATH: 'x', VOCAL_ENGINE_CWD: 'ignored' } });
  expect(l.source).toBe('packaged');
  expect(l.command).toBe(path.join(res, 'engine', 'vocal-engine', 'vocal-engine.exe'));
  expect(l.args).toEqual([]);
  expect(l.cwd).toBe(path.dirname(l.command));
  expect(l.engineDir).toBeNull();
  expect(l.configPath).toBeNull();
  expect(l.env.PYTHONIOENCODING).toBe('utf-8');
});

test('(P2) 開発版は .mcp.json の gliss（VOCAL_ENGINE_CWD で cwd だけ差し替え）', () => {
  const repo = path.join(tmp, 'repo');
  fs.mkdirSync(repo);
  fs.writeFileSync(path.join(repo, '.mcp.json'), JSON.stringify({ mcpServers: { gliss: {
    command: 'C:\\py\\python.exe', args: ['-m', 'vocal_engine.mcp'], cwd: 'C:\\main\\engine', env: { FOO: '1' } } } }));
  const a = engineLaunch({ packaged: false, resourcesDir: '', repo, bridge: BRIDGE, env: {} });
  expect(a).toMatchObject({ source: 'mcp.json', command: 'C:\\py\\python.exe', args: ['-m', 'vocal_engine.mcp'],
    cwd: 'C:\\main\\engine', engineDir: 'C:\\main\\engine', configPath: path.join(repo, '.mcp.json') });
  expect(a.env.FOO).toBe('1');
  const b = engineLaunch({ packaged: false, resourcesDir: '', repo, bridge: BRIDGE, env: { VOCAL_ENGINE_CWD: 'D:\\wt\\engine' } });
  expect(b).toMatchObject({ command: 'C:\\py\\python.exe', cwd: 'D:\\wt\\engine', engineDir: 'D:\\wt\\engine' });
  // gliss が無い .mcp.json は黙って別の経路に落とさず、理由を出す
  fs.writeFileSync(path.join(repo, '.mcp.json'), JSON.stringify({ mcpServers: {} }));
  expect(() => engineLaunch({ packaged: false, resourcesDir: '', repo, bridge: BRIDGE, env: {} })).toThrow(/mcpServers\.gliss/);
});

test('(P3) .mcp.json が無い開発版は <repo>/.venv の python', () => {
  const repo = path.join(tmp, 'repo');
  fs.mkdirSync(repo);
  const l = engineLaunch({ packaged: false, resourcesDir: '', repo, bridge: BRIDGE, env: {}, platform: 'win32' });
  expect(l).toMatchObject({ source: 'venv', command: path.join(repo, '.venv', 'Scripts', 'python.exe'),
    args: ['-m', 'vocal_engine.mcp'], cwd: path.join(repo, 'engine'), engineDir: path.join(repo, 'engine'), configPath: null });
  expect(l.env.PYTHONIOENCODING).toBe('utf-8');
  const w = engineLaunch({ packaged: false, resourcesDir: '', repo, bridge: BRIDGE, env: { VOCAL_ENGINE_CWD: 'D:\\wt\\engine' }, platform: 'linux' });
  expect(w.command).toBe(path.join(repo, '.venv', 'bin', 'python'));
  expect(w.cwd).toBe('D:\\wt\\engine');
});

test('(P4) 画面が起動したエンジンの印はどの経路でも付く', () => {
  const repo = path.join(tmp, 'repo');
  fs.mkdirSync(repo);
  for (const packaged of [true, false]) {
    const l = engineLaunch({ packaged, resourcesDir: tmp, repo, bridge: BRIDGE, env: { GLISS_CLIENT: 'ai' } });
    expect(l.env.GLISS_CLIENT).toBe('app');
    expect(l.env.GLISS_BRIDGE).toBe(BRIDGE);
  }
});

test('(P5) AI に登録する配布版の command は、画面が起動する exe と同じ', () => {
  const res = path.join(tmp, 'resources');
  const ai = serverConfig({ packaged: true, resourcesDir: res, bridge: BRIDGE, env: {} });
  expect(ai.command).toBe(packagedEngineExe(res));
  expect(ai.command).toBe(engineLaunch({ packaged: true, resourcesDir: res, repo: tmp, bridge: BRIDGE, env: {} }).command);
  expect(ai.args).toEqual([]);
  expect(ai.env).toEqual({ GLISS_BRIDGE: BRIDGE });
});

test('(P6) アイコンの置き場', () => {
  expect(assetsDir({ packaged: true, resourcesDir: 'R:\\resources', repo: 'X:\\repo' })).toBe(path.join('R:\\resources', 'assets'));
  expect(assetsDir({ packaged: false, resourcesDir: 'R:\\resources', repo: 'X:\\repo' })).toBe(path.join('X:\\repo', 'assets'));
});
