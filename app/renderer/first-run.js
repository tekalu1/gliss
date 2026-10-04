import { $ } from './engine.js';
import { refreshAddons } from './addons.js';

const root = $('#mock');
const panel = $('#firstRun');
let models = { rmvpe: false, hubertfa: false };
let sizes = { rmvpe: 334213248, hubertfa: 256589553 };
let progress = { phase: 'idle', bytes: 0, total: 0 };
let installed = false;
// 解析に要る重み。RMVPE は任意（編集 > ピッチ検出の方式 で RMVPE を選ぶときに使う。既定の Gliss の F0 モデルは
// エンジンに同梱）なので「準備完了」の条件に入れず、別のボタン（RMVPE も取得）で取る
const REQUIRED = ['hubertfa'];
let optionalStarted = false;     // 最後に始めたのが任意の RMVPE だけの取得か（失敗したときの「再試行」を出すボタン）

function active() { return ['downloading', 'extracting'].includes(progress.phase); }
function formatBytes(n) { return `${Math.ceil(n / 1e6)} MB`; }

function draw() {
  const ready = REQUIRED.every((id) => models[id]);
  const optional = !models.rmvpe;
  const starting = progress.phase === 'starting';
  const failed = progress.phase === 'failed';
  const missing = REQUIRED.filter((id) => !models[id]).reduce((n, id) => n + sizes[id], 0);
  const summary = $('#modelSummary');
  summary.textContent = ready ? '準備完了' : starting ? '準備中…' : active()
    ? progress.phase === 'extracting' ? `${progress.model} を配置中…` : `${progress.model} をダウンロード中…`
    : formatBytes(missing);
  const percent = progress.total ? Math.min(100, Math.floor(100 * progress.bytes / progress.total)) : 0;
  $('#modelProgress').hidden = !active();
  $('#modelProgress').setAttribute('aria-valuenow', String(percent));
  $('#modelProgressFill').style.width = `${percent}%`;
  $('#modelPercent').hidden = !active();
  $('#modelPercent').textContent = `${percent}%`;
  $('#modelLicenseButton').hidden = ready && !optional;
  $('#modelDownloadButton').hidden = ready || active() || starting;
  $('#modelDownloadButton').textContent = failed && !optionalStarted ? '再試行' : 'ダウンロード';
  $('#modelOptionalButton').hidden = !optional || active() || starting;
  $('#modelOptionalButton').textContent = failed && optionalStarted ? 'RMVPE を再試行'
    : `RMVPE も取得（任意・${formatBytes(sizes.rmvpe)}）`;
  $('#modelCancelButton').hidden = !active();
  $('#modelError').textContent = failed ? progress.error : '';
  if (ready && !optional) $('#modelLicense').hidden = true;
}

export async function refreshModels() {
  try {
    const info = await window.api.call('engine_info', {});
    if (info.ok === false) throw new Error(info.error);
    models = { rmvpe: !!info.rmvpe_model_found, hubertfa: !!info.phonemes?.model_found };
    draw();
  } catch (err) {
    progress = { phase: 'failed', error: String(err.message || err), bytes: 0, total: 0 };
    draw();
  }
}

function aiCommand() {
  const help = window.__app?.appMenuTemplate?.().find((menu) => menu.label === 'ヘルプ');
  return help?.submenu?.find((item) => item.label?.startsWith('AI とつなぐ'))?.cmd || null;
}

export function showFirstRun(show) {
  root.classList.toggle('first-run', show);
  panel.hidden = !show;
  const cmd = aiCommand();
  $('#firstRunAi').hidden = !cmd;
  $('#firstRunAi').dataset.cmd = cmd || '';
  if (show && installed) { void refreshModels(); void refreshAddons(); }
}

export function installFirstRun({ onOpen, onAi }) {
  installed = true;
  $('#firstRunOpen').addEventListener('click', onOpen);
  $('#firstRunAi').addEventListener('click', (e) => { if (e.currentTarget.dataset.cmd) onAi(e.currentTarget.dataset.cmd); });
  $('#modelLicenseButton').addEventListener('click', () => {
    $('#modelLicense').hidden = !$('#modelLicense').hidden;
  });
  const start = async (ids) => {
    optionalStarted = !!ids;
    progress = { phase: 'starting', bytes: 0, total: 0, model: '' };
    draw();
    try { progress = await window.api.modelStart(ids); draw(); }
    catch (err) { progress = { phase: 'failed', error: String(err.message || err) }; draw(); }
  };
  $('#modelDownloadButton').addEventListener('click', () => start(null));
  $('#modelOptionalButton').addEventListener('click', () => start(['rmvpe']));
  $('#modelCancelButton').addEventListener('click', () => void window.api.modelCancel());
  window.api.onModelProgress((next) => {
    progress = next;
    draw();
    if (['done', 'cancelled', 'failed'].includes(next.phase)) void refreshModels();
  });
  void window.api.modelState().then((current) => { progress = current; draw(); });
  draw();
}

export function setModelSizes(next) { sizes = next; draw(); }
