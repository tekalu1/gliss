// キーボードショートカットの設定（issue #22。v3 §10）。編集 > キーボードショートカット…（Ctrl+,）。
//
//  - 上の欄でコマンド名かキーで検索、グループ（再生・ツール／編集／ノート／表示／トラック／ファイル）ごとの一覧
//  - 行をダブルクリック → キーを押すと割り当て（Esc でやめる）
//  - ほかのコマンドと重なると、その行の下に「Q は「半音に合わせる」に割り当て済み　置き換える／やめる」
//  - 変えた行は右に「既定: …」と ↺（既定に戻す）、× でキーを外す、下に「すべて既定に戻す」
//  - 設定は取り消しの履歴に入れない（ユーザー設定 = userData の state.json に保存。keys.js）
//  - いちばん下に「ホイール」のグループ（縦・横のズームとスクロール。issue #27）。行をダブルクリック →
//    修飾キーを押しながらホイールを回すと割り当て（キーは受け付けない。Esc でやめる）。重なりはキーと同じ
// VS Code・Studio One のキーボードショートカットに寄せた。ヘッダーにボタンは足さない。
import { COMMANDS, GROUPS, WHEEL, WHEEL_GROUP } from './commands.js';
import {
  comboOf, commandFor, defaultKeysOf, display, isModified, keysOf, replaceKey, resetAll, resetKeys, setKeys,
  wheelCombo,
} from './keys.js';

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const KS = { q: '', cap: null, conf: null };
let dlg = null;
let bg = null;
let root = null;
let back = null;              // 閉じたときにフォーカスを戻す先

const ALL = [...COMMANDS, ...WHEEL];
const ALL_GROUPS = [...GROUPS, WHEEL_GROUP];
const label = (id) => ALL.find((c) => c.id === id)?.label || id;
const isWheelRow = (id) => !!id && WHEEL.some((w) => w.id === id);
const keysText = (ks) => ks.map(display).join('、');

export function keysOpen() { return !!dlg && !dlg.hidden; }
export function capturing() { return keysOpen() && !!KS.cap; }
/** ホイールの行の割り当て待ち。 */
export function capturingWheel() { return capturing() && isWheelRow(KS.cap); }

export function openKeys() {
  if (keysOpen()) { dlg.querySelector('.ks').focus(); return; }
  back = document.activeElement;
  document.querySelector('#menu')?.setAttribute('hidden', '');
  dlg.hidden = false;
  bg.hidden = false;
  KS.cap = null; KS.conf = null;
  renderKeys();
  const s = dlg.querySelector('.ks');
  s.focus();
  s.select();
}

export function closeKeys() {
  if (!keysOpen()) return;
  dlg.hidden = true;
  bg.hidden = true;
  KS.cap = null; KS.conf = null;
  (back && back.isConnected ? back : root).focus({ preventScroll: true });
}

function matches(c, q) {
  if (!q) return true;
  const ks = keysOf(c.id);
  return c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q)
    || ks.some((k) => k.toLowerCase().includes(q) || display(k).toLowerCase().includes(q));
}

export function renderKeys() {
  const q = KS.q.trim().toLowerCase();
  let h = '';
  for (const g of ALL_GROUPS) {
    const list = ALL.filter((c) => c.group === g && matches(c, q));
    if (!list.length) continue;
    h += `<div class="kg">${esc(g)}</div>`;
    for (const c of list) {
      const ks = keysOf(c.id);
      const mod = isModified(c.id);
      const cap = KS.cap === c.id;
      h += `<div class="kr${mod ? ' mod' : ''}${cap ? ' cap' : ''}" data-id="${c.id}" role="row">`
        + `<span class="n">${esc(c.label)}</span>`
        + `<span class="k">${cap ? (c.wheel ? 'ホイールを回してください（Esc でやめる）' : 'キーを押してください（Esc でやめる）')
          : (esc(keysText(ks)) || '—')}</span>`
        + `<span class="st">${mod ? `既定: ${esc(keysText(defaultKeysOf(c.id)) || 'なし')}` : ''}</span>`
        + `<span class="ab">${mod ? '<button data-b="def" title="既定に戻す" aria-label="既定に戻す">↺</button>' : ''}`
        + `${ks.length ? '<button data-b="rm" title="キーを外す" aria-label="キーを外す">×</button>' : ''}</span></div>`;
      if (cap && KS.conf) {
        h += `<div class="kc">${esc(display(KS.conf.combo))} は「${esc(label(KS.conf.other))}」に割り当て済み`
          + '<button data-b="rep">置き換える</button><button data-b="cancel">やめる</button></div>';
      }
    }
  }
  dlg.querySelector('.kl').innerHTML = h || '<div class="kg">見つかりません</div>';
  const n = ALL.filter((c) => isModified(c.id)).length;
  dlg.querySelector('.kn').textContent = n ? `${n} 件を変更` : '';
}

/** キーを押した（割り当て待ちの行があるとき）。 */
function onCapture(e) {
  if (!capturing()) return;
  e.preventDefault();
  e.stopImmediatePropagation();          // 同じ document の Esc で閉じる処理にも渡さない
  if (e.key === 'Escape') { KS.cap = null; KS.conf = null; renderKeys(); return; }
  if (isWheelRow(KS.cap)) return;               // ホイールの行: キーは割り当てない（修飾キーはホイールと一緒に見る）
  const c = comboOf(e);
  if (!c) return;                               // 修飾キーだけ: 次のキーを待つ
  if (e.altKey) window.api?.consumeAlt?.();
  const other = commandFor(c, KS.cap);
  if (other) { KS.conf = { combo: c, other }; renderKeys(); return; }
  const id = KS.cap;
  KS.cap = null; KS.conf = null;
  setKeys(id, [c]);
  renderKeys();
}

/** ホイールを回した（ホイールの行の割り当て待ちのとき）。修飾キー＋ホイールを割り当てる。 */
function onWheelCapture(e) {
  if (!capturingWheel()) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (!e.deltaY && !e.deltaX) return;
  const c = wheelCombo(e);
  if (e.altKey) window.api?.consumeAlt?.();
  const other = commandFor(c, KS.cap);
  if (other) {
    if (KS.conf?.combo === c) return;           // 続けて回した分（同じ表示のまま）
    KS.conf = { combo: c, other };
    renderKeys();
    return;
  }
  const id = KS.cap;
  KS.cap = null; KS.conf = null;
  setKeys(id, [c]);
  renderKeys();
}

export function installKeysDialog(rootEl) {
  root = rootEl;
  dlg = $('#keys');
  bg = $('#keysBg');
  const search = dlg.querySelector('.ks');
  search.addEventListener('input', (e) => { KS.q = e.target.value; renderKeys(); });
  dlg.addEventListener('dblclick', (e) => {
    const r = e.target.closest('.kr');
    if (!r || e.target.closest('button')) return;
    KS.cap = r.dataset.id;
    KS.conf = null;
    renderKeys();
    dlg.focus({ preventScroll: true });           // 検索欄に文字を入れない
  });
  dlg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const id = e.target.closest('.kr')?.dataset.id;
    switch (b.dataset.b) {
      case 'close': closeKeys(); return;
      case 'all': KS.cap = null; KS.conf = null; resetAll(); break;
      case 'def': resetKeys(id); break;
      case 'rm': setKeys(id, []); break;
      case 'rep': { const { combo } = KS.conf; const cap = KS.cap; KS.cap = null; KS.conf = null; replaceKey(cap, combo); break; }
      case 'cancel': KS.cap = null; KS.conf = null; break;
      default: return;
    }
    renderKeys();
  });
  // 割り当て待ちの間は、キーを何にも渡さない（コマンド・検索欄より先に取る）
  document.addEventListener('keydown', onCapture, true);
  document.addEventListener('wheel', onWheelCapture, { capture: true, passive: false });
  // 開いている間の Esc は閉じる（割り当て待ちなら、やめるだけ = 上）
  document.addEventListener('keydown', (e) => {
    if (!keysOpen() || capturing()) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeKeys(); }
  }, true);
  bg.addEventListener('pointerdown', closeKeys);
}
