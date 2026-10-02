// 配布版と開発版で場所が変わるものを 1 か所で決める。main.mjs から使う。Electron に依存しない（テストから直接呼べる）。
//
//  - engineLaunch(): エンジン（Python MCP サーバー）の起動方法。
//      配布版（app.isPackaged）  同梱の単体 exe（resources/engine/vocal-engine/vocal-engine.exe）。
//                                 エンジン exe は scripts/build-engine.mjs（pnpm build:engine）が作り、electron-builder が同梱する。
//      開発版                    リポジトリ直下の `.mcp.json`（サーバー ID `gliss`）があればそれ。無ければ
//                                 `<repo>/.venv/Scripts/python.exe -m vocal_engine.mcp`（cwd は engine/）。
//                                 公開リポジトリの `.mcp.json` は例（`engine/.mcp.json.example`）だけなので、
//                                 これが無いときも `.venv` を作れば動く。
//  - assetsDir(): ロゴ・アイコンの置き場。配布版は resources/assets（electron-builder の extraResources）、開発版は <repo>/assets。
import fs from 'node:fs';
import path from 'node:path';

export const SERVER_ID = 'gliss';

/** 配布版のエンジン exe のパス。ai-connect.mjs の serverConfig（AI に登録する command）と同じ場所。 */
export function packagedEngineExe(resourcesDir) {
  return path.join(resourcesDir || '', 'engine', 'vocal-engine', 'vocal-engine.exe');
}

export function assetsDir({ packaged, resourcesDir, repo }) {
  return packaged ? path.join(resourcesDir, 'assets') : path.join(repo, 'assets');
}

/** 開発版の既定: `<repo>/.venv/Scripts/python.exe -m vocal_engine.mcp`（Windows 以外は bin/python）。 */
export function defaultVenvPython(repo, platform = process.platform) {
  return platform === 'win32'
    ? path.join(repo, '.venv', 'Scripts', 'python.exe')
    : path.join(repo, '.venv', 'bin', 'python');
}

/**
 * エンジンの起動方法。
 * @param {object} o
 * @param {boolean} o.packaged  app.isPackaged
 * @param {string} o.resourcesDir  process.resourcesPath
 * @param {string} o.repo  リポジトリのルート（開発版）
 * @param {string} o.bridge  bridge.json のパス
 * @param {object} [o.env]  画面の環境変数
 * @param {string} [o.platform]
 * @returns {{command: string, args: string[], cwd: string, env: object, engineDir: string|null,
 *            source: 'packaged'|'mcp.json'|'venv', configPath: string|null}}
 *   engineDir は開発版だけ（vocal_engine の親。AI に登録するときの PYTHONPATH）。
 */
export function engineLaunch({ packaged, resourcesDir, repo, bridge, env = process.env, platform = process.platform }) {
  // GLISS_CLIENT=app: 画面が起動したエンジン（AI に許可の対象外。engine/vocal_engine/bridge.py）。
  // GLISS_BRIDGE: 画面と AI 側のエンジンが読み書きする bridge.json（AI に許可・画面で開いている曲）
  const own = { GLISS_CLIENT: 'app', GLISS_BRIDGE: bridge };
  if (packaged) {
    const command = packagedEngineExe(resourcesDir);
    return {
      command, args: [], cwd: path.dirname(command), engineDir: null, source: 'packaged', configPath: null,
      env: { ...env, PYTHONIOENCODING: 'utf-8', ...own },
    };
  }
  const mcpJson = path.join(repo, '.mcp.json');
  // VOCAL_ENGINE_CWD: エンジンの作業ディレクトリを差し替える（git worktree で試すとき用）。
  // `python -m vocal_engine.mcp` は作業ディレクトリを sys.path の先頭に入れるので、.mcp.json の
  // cwd のままだと PYTHONPATH を付けても本体側の vocal_engine が読まれる。
  const cwdOverride = env.VOCAL_ENGINE_CWD || null;
  if (fs.existsSync(mcpJson)) {
    const cfg = JSON.parse(fs.readFileSync(mcpJson, 'utf8'))?.mcpServers?.[SERVER_ID];
    if (!cfg) throw new Error(`${mcpJson} に mcpServers.${SERVER_ID} が無い`);
    return {
      command: cfg.command, args: cfg.args || [], cwd: cwdOverride || cfg.cwd || repo,
      engineDir: cwdOverride || cfg.cwd || path.join(repo, 'engine'), source: 'mcp.json', configPath: mcpJson,
      env: { ...env, ...(cfg.env || {}), ...own },
    };
  }
  const engineDir = cwdOverride || path.join(repo, 'engine');
  return {
    command: defaultVenvPython(repo, platform), args: ['-m', 'vocal_engine.mcp'], cwd: engineDir,
    engineDir, source: 'venv', configPath: null,
    env: { ...env, PYTHONIOENCODING: 'utf-8', ...own },
  };
}
