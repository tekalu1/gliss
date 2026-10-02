// キーの割り当て（issue #22）。コマンドの表（commands.js）の既定のキーに、ユーザーの設定（userData の
// state.json の `keys`）を重ねたもの。右クリックのメニュー・メニューバー・ツールチップの表記と
// キー入力の割り当ては、すべてここを引く。設定は取り消しの履歴に入れない（ユーザーごとに保存）。
//
// キーの書き方（combo）: 修飾は Ctrl・Shift・Alt の順、`+` でつなぐ。キーは物理キー（KeyboardEvent.code）
// から決めるので、日本語配列でも `Ctrl+,` などが同じに取れる。例: `Ctrl+Shift+Z` `Alt+X` `Delete` `F2` `Space`

const defaults = new Map();     // id → [combo]（コマンドの表の既定）
const current = new Map();      // id → [combo]（設定を重ねた今の割り当て）
const listeners = [];

const CODE = {
  Space: 'Space', Comma: ',', Period: '.', Slash: '/', Minus: '-', Equal: '=', BracketLeft: '[',
  BracketRight: ']', Semicolon: ';', Quote: "'", Backquote: '`', Backslash: '\\', IntlRo: '\\',
  IntlYen: '¥', Delete: 'Delete', Backspace: 'Backspace', Enter: 'Enter', Tab: 'Tab', Home: 'Home',
  End: 'End', PageUp: 'PageUp', PageDown: 'PageDown', Insert: 'Insert',
};

/** キー入力 → combo（修飾キーだけ・割り当てられないキーは null）。 */
export function comboOf(e) {
  const c = e.code || '';
  let k = null;
  if (/^Key[A-Z]$/.test(c)) k = c.slice(3);
  else if (/^Digit\d$/.test(c)) k = c.slice(5);
  else if (/^Numpad\d$/.test(c)) k = `Num${c.slice(6)}`;
  else if (/^F\d{1,2}$/.test(c) || /^Arrow(Up|Down|Left|Right)$/.test(c)) k = c;
  else k = CODE[c] || null;
  if (!k) return null;
  return [e.ctrlKey || e.metaKey ? 'Ctrl' : '', e.shiftKey ? 'Shift' : '', e.altKey ? 'Alt' : '', k]
    .filter(Boolean).join('+');
}

/** ホイールの操作 → combo（`Wheel` `Shift+Wheel` `Ctrl+Wheel` `Ctrl+Shift+Wheel` `Alt+Wheel` …。issue #27）。
 * キーと同じ表（ここの current）に入れて、同じ設定画面で一覧・変更する。キーの combo とは重ならない。 */
export function wheelCombo(e) {
  return [e.ctrlKey || e.metaKey ? 'Ctrl' : '', e.shiftKey ? 'Shift' : '', e.altKey ? 'Alt' : '', 'Wheel']
    .filter(Boolean).join('+');
}
/** combo がホイールの操作か。 */
export function isWheel(combo) { return /(^|\+)Wheel$/.test(combo || ''); }

/** 画面に出す表記（Delete は Del、Wheel は ホイール）。 */
export function display(combo) {
  return String(combo || '').replace(/(^|\+)Delete$/, '$1Del').replace(/(^|\+)Wheel$/, '$1ホイール');
}

/** Electron のメニューの accelerator。 */
export function accelerator(combo) {
  if (!combo || isWheel(combo)) return undefined;
  const parts = combo.split('+');
  const key = parts.pop();
  let k = key;
  if (/^Num\d$/.test(key)) k = `num${key.slice(3)}`;
  else if (/^Arrow/.test(key)) k = key.slice(5);
  else if (key === '¥' || key === '`' || key === "'" || key === '\\') return undefined;   // 表記だけメニューに出さない
  return [...parts.map((p) => (p === 'Ctrl' ? 'CmdOrCtrl' : p)), k].join('+');
}

/** コマンドの表の既定を登録する（commands.js が読み込まれたときに 1 回）。 */
export function registerDefaults(list) {
  for (const { id, keys } of list) {
    defaults.set(id, (keys || []).slice());
    if (!current.has(id)) current.set(id, (keys || []).slice());
  }
}

/** 設定（{id: [combo]}。既定と違うものだけ）を重ねる。 */
export function loadOverrides(ov) {
  for (const [id, d] of defaults) current.set(id, d.slice());
  for (const [id, ks] of Object.entries(ov || {})) {
    if (defaults.has(id) && Array.isArray(ks)) current.set(id, ks.filter((k) => typeof k === 'string'));
  }
}

const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

/** 既定と違うものだけ（保存する形）。 */
export function overrides() {
  const out = {};
  for (const [id, ks] of current) if (!same(ks, defaults.get(id) || [])) out[id] = ks.slice();
  return out;
}

export function keysOf(id) { return (current.get(id) || []).slice(); }
export function defaultKeysOf(id) { return (defaults.get(id) || []).slice(); }
export function isModified(id) { return !same(current.get(id) || [], defaults.get(id) || []); }
/** メニュー・ツールチップに出す 1 つ目のキー（無ければ ''）。 */
export function keyText(id) { const k = (current.get(id) || [])[0]; return k ? display(k) : ''; }
/** 「名前（キー）」の形のツールチップ。 */
export function withKey(label, id) { const k = keyText(id); return k ? `${label}（${k}）` : label; }
/** combo が割り当てられているコマンド（except は除く）。 */
export function commandFor(combo, except = null) {
  for (const [id, ks] of current) if (id !== except && ks.includes(combo)) return id;
  return null;
}

/** 割り当てを変える（設定画面から）。変えたら保存して、表記を描き直す。 */
export function setKeys(id, ks) {
  current.set(id, ks.slice());
  changed();
}
export function resetKeys(id) { setKeys(id, defaults.get(id) || []); }
export function resetAll() {
  for (const [id, d] of defaults) current.set(id, d.slice());
  changed();
}
/** combo を別のコマンドから外して id に付ける（重なりの「置き換える」）。 */
export function replaceKey(id, combo) {
  for (const [other, ks] of current) {
    if (other !== id && ks.includes(combo)) current.set(other, ks.filter((k) => k !== combo));
  }
  current.set(id, [combo]);
  changed();
}

export function onKeysChanged(fn) { listeners.push(fn); }
function changed() {
  window.api?.saveState?.({ keys: overrides() });
  for (const fn of listeners) fn();
}
