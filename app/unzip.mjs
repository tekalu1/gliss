// zip の展開を worker スレッドで行う（アドオン・解析モデルの zip。addons.mjs・model-download.mjs）。
//
// Electron の main プロセスのイベントループで extract-zip（yauzl → zlib のストリーム）を回すと、ひと回りごとに
// 待たされて極端に遅くなることがある（2026-10-03、展開版をテストの見えないウィンドウで動かしたとき: setImmediate 1000 回に
// 45 秒、20 MB の inflate に 56 秒。317 MB のアドオンの展開が終わらない）。worker スレッドは自分のイベントループで
// 回るので速い（同じ条件で 317 MB を 1.7 秒）。extract-zip は app.asar の中にあっても worker から require できる。
import { createRequire } from 'node:module';
import { Worker } from 'node:worker_threads';

const require = createRequire(import.meta.url);

const WORKER = `
const { parentPort, workerData } = require('node:worker_threads');
const extract = require(workerData.module);
extract(workerData.zip, { dir: workerData.dir })
  .then(() => parentPort.postMessage({ ok: true }), (e) => parentPort.postMessage({ ok: false, error: String(e && e.message || e) }));
`;

/** zip を dir（絶対パス）に展開する。signal で取り消すと worker を止めて「取り消した」で終わる。 */
export function extractZip(zip, dir, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error('取り消した')); return; }
    const worker = new Worker(WORKER, { eval: true, workerData: { module: require.resolve('extract-zip'), zip, dir } });
    let settled = false;
    const done = (fn, v) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', abort);
      void worker.terminate();
      fn(v);
    };
    const abort = () => done(reject, new Error('取り消した'));
    signal?.addEventListener('abort', abort, { once: true });
    worker.on('message', (m) => (m.ok ? done(resolve) : done(reject, new Error(`zip を展開できない: ${m.error}`))));
    worker.on('error', (e) => done(reject, e));
    worker.on('exit', (code) => done(reject, new Error(`zip の展開が終わる前に止まった（${code}）`)));
  });
}
