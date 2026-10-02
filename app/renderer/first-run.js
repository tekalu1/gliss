import { $ } from './engine.js';
import { refreshAddons } from './addons.js';

const root = $('#mock');
const panel = $('#firstRun');
let models = { rmvpe: false, hubertfa: false };
let sizes = { rmvpe: 334213248, hubertfa: 256589553 };
let progress = { phase: 'idle', bytes: 0, total: 0 };
let installed = false;

function active() { return ['downloading', 'extracting'].includes(progress.phase); }
function formatBytes(n) { return `${Math.ceil(n / 1e6)} MB`; }

function draw() {
  const ready = models.rmvpe && models.hubertfa;
  const starting = progress.phase === 'starting';
  const missing = Object.entries(models).filter(([, found]) => !found).reduce((n, [id]) => n + sizes[id], 0);
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
  $('#modelLicenseButton').hidden = ready;
  $('#modelDownloadButton').hidden = ready || active() || starting;
  $('#modelDownloadButton').textContent = progress.phase === 'failed' ? '再試行' : 'ダウンロード';
  $('#modelCancelButton').hidden = !active();
  $('#modelError').textContent = progress.phase === 'failed' ? progress.error : '';
  if (ready) $('#modelLicense').hidden = true;
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
  $('#modelDownloadButton').addEventListener('click', async () => {
    progress = { phase: 'starting', bytes: 0, total: 0, model: '' };
    draw();
    try { progress = await window.api.modelStart(); draw(); }
    catch (err) { progress = { phase: 'failed', error: String(err.message || err) }; draw(); }
  });
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
