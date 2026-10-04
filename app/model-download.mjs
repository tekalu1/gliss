// 公開元の zip を Electron net で取得し、検証してからエンジンが読む場所に置く。
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { finished } from 'node:stream/promises';
import { extractZip } from './unzip.mjs';

export const MODEL_SOURCES = [
  {
    // 任意（編集 > ピッチ検出の方式 で RMVPE を選ぶときに使う。既定の Gliss の F0 モデルはエンジンに同梱）
    id: 'rmvpe', name: 'RMVPE', optional: true,
    url: 'https://github.com/yxlllc/RMVPE/releases/download/230917/rmvpe-onnx.zip',
    size: 334213248,
    sha256: '72b7cfda722bd2697cab537b38ece2698dfe18d8f3487735608d62e82a583ed8',
    files: ['rmvpe.onnx'],
  },
  {
    id: 'hubertfa', name: 'HubertFA',
    url: 'https://github.com/wolfgitpr/HubertFA/releases/download/v0.0.7/1218_hfa_model_new_dict.zip',
    size: 256589553,
    sha256: '48bd6dbcc293e47cc6cbcc556baf575e3d91d61d0ac88ddeccd6098f8f346fa2',
    files: ['model.onnx', 'vocab.json', 'config.json', 'VERSION', 'japanese_dict_full.txt'],
  },
];

export const totalModelBytes = MODEL_SOURCES.reduce((n, s) => n + s.size, 0);

/**
 * 保存先をここで決める。エンジンにも同じ場所を渡す。
 * 配布版・開発版とも %LOCALAPPDATA%\Gliss\models（開発版でも初回の画面で取得でき、worktree・配布版と共有できる）。
 * 環境変数 VOCAL_ENGINE_MODELS_DIR（旧名 VOCAL_ENGINE_MODELS）で差し替えられる。packaged・repo は呼び出し側の互換のため受け取るだけ。
 */
export function modelDirectory({ packaged, env = process.env, localAppData, repo }) {
  if (env.VOCAL_ENGINE_MODELS_DIR) return path.resolve(env.VOCAL_ENGINE_MODELS_DIR);
  if (env.VOCAL_ENGINE_MODELS) return path.resolve(env.VOCAL_ENGINE_MODELS);
  return path.join(localAppData || path.join(os.homedir(), 'AppData', 'Local'), 'Gliss', 'models');
}

export function modelPresence(info) {
  return {
    rmvpe: !!info?.rmvpe_model_found,
    hubertfa: !!info?.phonemes?.model_found,
  };
}

function cancelled(signal) {
  if (signal.aborted) throw new Error('取り消した');
}

/** zip を 1 つ取得する。転送先への追従と OS のプロキシ設定は Electron net を使う。 */
export function fetchArchive(net, source, destination, signal, onBytes) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let redirects = 0;
    let received = 0;
    const hash = crypto.createHash('sha256');
    const output = fs.createWriteStream(destination, { flags: 'wx' });
    const request = net.request({ url: source.url, redirect: 'manual' });
    const fail = (error) => {
      if (settled) return;
      settled = true;
      request.abort();
      output.destroy();
      reject(error);
    };
    const abort = () => fail(new Error('取り消した'));
    signal.addEventListener('abort', abort, { once: true });
    request.on('redirect', () => {
      if (++redirects > 10) { fail(new Error('転送が多すぎる')); return; }
      request.followRedirect();
    });
    request.on('error', fail);
    output.on('error', fail);
    request.on('response', (response) => {
      if (response.statusCode !== 200) {
        fail(new Error(`HTTP ${response.statusCode}: ${source.name}`));
        return;
      }
      response.on('error', fail);
      response.on('data', (chunk) => {
        received += chunk.length;
        if (received > source.size) { fail(new Error(`${source.name}: サイズが違う`)); return; }
        hash.update(chunk);
        if (!output.write(chunk)) { response.pause(); output.once('drain', () => response.resume()); }
        onBytes(received);
      });
      response.on('end', async () => {
        if (settled) return;
        output.end();
        try {
          await finished(output);
          cancelled(signal);
          if (received !== source.size) throw new Error(`${source.name}: サイズが違う`);
          if (hash.digest('hex').toLowerCase() !== source.sha256.toLowerCase()) {
            throw new Error(`${source.name}: SHA-256 が一致しない`);
          }
          settled = true;
          resolve();
        } catch (e) { fail(e); }
      });
    });
    request.end();
  });
}

function findFiles(root, expected) {
  const found = new Map();
  function visit(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) visit(full);
      else if (expected.includes(entry.name)) {
        if (found.has(entry.name)) throw new Error(`${entry.name}: zip 内に複数ある`);
        found.set(entry.name, full);
      }
    }
  }
  visit(root);
  for (const name of expected) if (!found.has(name)) throw new Error(`${name}: zip 内に見つからない`);
  return found;
}

export async function installArchive(zip, source, modelsDir, signal) {
  const staging = fs.mkdtempSync(path.join(path.dirname(zip), 'extract-'));
  try {
    // main のイベントループでは極端に遅くなることがあるので worker で（unzip.mjs）
    await extractZip(zip, staging, signal);
    cancelled(signal);
    const files = findFiles(staging, source.files);
    const target = source.id === 'rmvpe' ? modelsDir
      : path.join(modelsDir, 'hubertfa', '1218_hfa_model_new_dict');
    fs.mkdirSync(target, { recursive: true });
    for (const [name, src] of files) {
      cancelled(signal);
      const dest = path.join(target, name);
      const temp = `${dest}.gliss-download`;
      try {
        fs.copyFileSync(src, temp);
        fs.renameSync(temp, dest);
      } finally { fs.rmSync(temp, { force: true }); }
    }
  } finally { fs.rmSync(staging, { recursive: true, force: true }); }
}

/** テスト時は URL・サイズ・SHA-256 を差し替えられる。通常は MODEL_SOURCES をそのまま使う。 */
export function sourcesFromEnvironment(env = process.env) {
  if (!env.GLISS_MODEL_SOURCE_JSON) return MODEL_SOURCES;
  const overrides = JSON.parse(env.GLISS_MODEL_SOURCE_JSON);
  return MODEL_SOURCES.map((source) => {
    const v = overrides[source.id];
    return v ? { ...source, url: v.url, size: v.size, sha256: v.sha256 } : source;
  });
}

/** ids: 取得するモデル（省くと必須のものだけ。任意の RMVPE は初回画面の「RMVPE も取得」で ids に入れて頼む）。 */
export async function downloadModels({ net, sources, modelsDir, present, signal, progress, ids = null }) {
  const missing = sources.filter((s) => (ids ? ids.includes(s.id) : !s.optional) && !present[s.id]);
  const total = missing.reduce((n, s) => n + s.size, 0);
  let done = 0;
  if (!missing.length) { progress({ phase: 'done', bytes: 0, total }); return; }
  fs.mkdirSync(modelsDir, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-models-'));
  try {
    for (const source of missing) {
      cancelled(signal);
      const zip = path.join(tmp, `${source.id}.zip`);
      progress({ phase: 'downloading', model: source.name, bytes: done, total });
      await fetchArchive(net, source, zip, signal, (bytes) => {
        progress({ phase: 'downloading', model: source.name, bytes: done + bytes, total });
      });
      cancelled(signal);
      progress({ phase: 'extracting', model: source.name, bytes: done + source.size, total });
      await installArchive(zip, source, modelsDir, signal);
      done += source.size;
      fs.rmSync(zip, { force: true });
    }
    progress({ phase: 'done', bytes: total, total });
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
}
