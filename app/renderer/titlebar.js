// タイトルバー（ウィンドウの上端のバー）: ロゴ・メニューバー・中央に開いているプロジェクトの名前。
// 最小化・最大化・閉じるは OS が右上に描く（main の titleBarOverlay。スナップレイアウトが効く）。
//
// **メニューの中身は main の Electron の Menu が唯一の正**。ここは main が送ってくる写し
// （`api.getMenu` / `api.onAppMenu`。名前・有効・チェック・キーの表記・下の段）を描くだけで、
// 押された項目は `api.menuClick(id)` で main に返し、main が Menu の同じ項目の click を呼ぶ。
// Menu のテンプレートに項目を足せば、ここを変えずに画面に出る。
//
// キーボードは Windows のメニューバーと同じ: Alt だけを押して離す（か F10）でメニューバーにフォーカス、
// ←→ で上の段、↓・Enter・Space で開く、↑↓ で項目、→ で下の段、Enter で実行、Esc で 1 段ずつ閉じる。
// メニューバーにフォーカスがある間、キーはメニューだけが受ける（コマンドのキーは効かない）。
// Alt を修飾キーに使った（Alt+X・Alt+ドラッグ・Alt+ホイール）ときは開かない。

const $ = (s) => document.querySelector(s);

const M = {
  model: [],          // main の Menu の写し
  active: false,      // メニューバーにフォーカスがある
  top: -1,            // 選んでいる上の段
  levels: [],         // 開いている段（[0] = 上の段の下、[1] = その下の段…）: { items, el, sel, parent }
  prevFocus: null,    // メニューバーに来る前のフォーカス（閉じたら戻す）
  altArmed: false,    // Alt だけを押している（離したらメニューバーへ）
};
const hooks = { beforeOpen: () => {}, blocked: () => false, keyTaken: () => false };

/** マウスが実際に動いた（画面が変わったことで出る動いていない move は数えない）。 */
let lastPt = null;
function moved(e) {
  const p = `${e.screenX},${e.screenY}`;
  if (p === lastPt) return false;
  lastPt = p;
  return true;
}

const visible = (items) => (items || []).filter((it) => it.visible !== false);
const navigable = (it) => it && it.type !== 'separator';
const tops = () => visible(M.model).filter((it) => it.submenu);

// ---------------------------------------------------------------- 描く
function renderBar() {
  const bar = $('#menubar');
  if (!bar) return;
  const list = tops();
  const key = JSON.stringify(list.map((t) => t.label));
  if (bar.dataset.key !== key) {
    bar.dataset.key = key;
    bar.textContent = '';
    list.forEach((t, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = t.label;
      b.setAttribute('role', 'menuitem');
      b.setAttribute('aria-haspopup', 'menu');
      b.tabIndex = -1;
      b.dataset.top = String(i);
      b.addEventListener('pointerdown', (e) => onTopDown(e, i));
      b.addEventListener('pointermove', (e) => {
        if (moved(e) && M.levels.length && M.top !== i) openTop(i, { sel: -1 });
      });
      bar.append(b);
    });
  }
  [...bar.children].forEach((b, i) => {
    const on = M.active && M.top === i;
    b.classList.toggle('hot', on && !M.levels.length);
    b.setAttribute('aria-expanded', String(on && M.levels.length > 0));
  });
}

function itemEl(it, level, idx) {
  if (it.type === 'separator') {
    const hr = document.createElement('div');
    hr.className = 'sep';
    hr.setAttribute('role', 'separator');
    return hr;
  }
  const b = document.createElement('button');
  b.type = 'button';
  b.tabIndex = -1;
  const check = it.type === 'checkbox' || it.type === 'radio';
  b.setAttribute('role', check ? (it.type === 'radio' ? 'menuitemradio' : 'menuitemcheckbox') : 'menuitem');
  if (check) b.setAttribute('aria-checked', String(!!it.checked));
  if (!it.enabled) b.setAttribute('aria-disabled', 'true');
  if (it.submenu) b.setAttribute('aria-haspopup', 'menu');
  if (it.toolTip) b.title = it.toolTip;
  b.dataset.id = it.id;
  const c = document.createElement('span');
  c.className = 'c';
  c.textContent = check && it.checked ? (it.type === 'radio' ? '•' : '✓') : '';
  const l = document.createElement('span');
  l.className = 'l';
  l.textContent = it.label;
  const k = document.createElement('span');
  k.className = 'k';
  k.textContent = it.submenu ? '›' : (it.accelLabel || '');
  b.append(c, l, k);
  // 乗ったら選ぶ。ただしマウスが実際に動いたときだけ（キーで開いた段がじっとしているマウスの下に出ても、
  // Windows と同じくマウスの下の項目を選ばない）
  b.addEventListener('pointermove', (e) => { if (moved(e)) hover(level, idx); });
  b.addEventListener('click', (e) => { e.preventDefault(); choose(level, idx, { mouse: true }); });
  return b;
}

function buildLevel(items, parentRect, level) {
  const el = document.createElement('div');
  el.className = 'mbd';
  el.setAttribute('role', 'menu');
  el.tabIndex = -1;
  const list = visible(items);
  list.forEach((it, i) => el.append(itemEl(it, level, i)));
  document.body.append(el);
  // 上の段の下（左端を揃える）／下の段は項目の右（はみ出すなら左）。画面の外へ出さない
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  let x;
  let y;
  if (level === 0) {
    x = parentRect.left;
    y = parentRect.bottom;
  } else {
    x = parentRect.right - 2;
    y = parentRect.top - 4;
    if (x + w > innerWidth) x = parentRect.left - w + 2;
  }
  x = Math.max(0, Math.min(x, innerWidth - w));
  y = Math.max(0, Math.min(y, innerHeight - h));
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
  return { items: list, el, sel: -1 };
}

function paintSel(lv) {
  [...lv.el.children].forEach((c, i) => c.classList.toggle('sel', i === lv.sel));
  const cur = lv.el.children[lv.sel];
  (cur || lv.el).focus({ preventScroll: true });
}

// ---------------------------------------------------------------- 開く・閉じる
function closeFrom(level) {
  while (M.levels.length > level) M.levels.pop().el.remove();
}

function openTop(i, { sel = -1, last = false } = {}) {
  const list = tops();
  if (!list.length) return;
  i = ((i % list.length) + list.length) % list.length;
  closeFrom(0);
  M.active = true;
  M.top = i;
  const btn = $('#menubar').children[i];
  const lv = buildLevel(list[i].submenu, btn.getBoundingClientRect(), 0);
  M.levels.push(lv);
  if (sel === 0 || last) lv.sel = step(lv, last ? 0 : -1, last ? -1 : 1);
  renderBar();
  paintSel(lv);
  refresh();
}

function openSub(level, { first = false } = {}) {
  const lv = M.levels[level];
  const it = lv?.items[lv.sel];
  if (!it?.submenu || !it.enabled) return false;
  closeFrom(level + 1);
  const sub = buildLevel(it.submenu, lv.el.children[lv.sel].getBoundingClientRect(), level + 1);
  M.levels.push(sub);
  if (first) sub.sel = step(sub, -1, 1);
  paintSel(sub);
  return true;
}

/** メニューバーにフォーカスを移す（Alt・F10）。上の段の最初を選ぶ（開かない）。 */
export function activate() {
  if (!tops().length) return;
  if (!M.active) M.prevFocus = document.activeElement;
  closeFrom(0);
  M.active = true;
  M.top = Math.max(0, M.top);
  renderBar();
  $('#menubar').children[M.top]?.focus({ preventScroll: true });
}

/** メニューを閉じてメニューバーから出る。restore: 前のフォーカスに戻す。 */
export function deactivate({ restore = true } = {}) {
  closeFrom(0);
  const was = M.active;
  M.active = false;
  M.top = -1;
  renderBar();
  if (was && restore) {
    const p = M.prevFocus;
    if (p && p.isConnected && p !== document.body) p.focus({ preventScroll: true });
    else $('#mock')?.focus({ preventScroll: true });
  }
  M.prevFocus = null;
}

/** 段の中で次に選べる項目（区切りは飛ばす。無効の項目も Windows と同じく選べる）。 */
function step(lv, from, dir) {
  const n = lv.items.length;
  for (let k = 1; k <= n; k++) {
    const i = (((from + dir * k) % n) + n) % n;
    if (navigable(lv.items[i])) return i;
  }
  return -1;
}

function hover(level, idx) {
  const lv = M.levels[level];
  if (!lv || (lv.sel === idx && (!lv.items[idx]?.submenu || M.levels[level + 1]))) return;
  closeFrom(level + 1);
  lv.sel = navigable(lv.items[idx]) ? idx : -1;
  paintSel(lv);
  if (lv.items[idx]?.submenu) openSub(level);
}

/** 項目を選んだ（クリック・Enter）: 下の段があれば開く、無ければ閉じて main に押したことを返す。 */
function choose(level, idx, { mouse = false } = {}) {
  const lv = M.levels[level];
  const it = lv?.items[idx];
  if (!navigable(it) || !it.enabled) return;
  lv.sel = idx;
  if (it.submenu) { openSub(level, { first: !mouse }); return; }
  deactivate({ restore: true });
  window.api?.menuClick?.(it.id);
}

/** 開くときに中身を読み直す（選択の変化など、送る前の分を取りこぼさない）。 */
async function refresh() {
  hooks.beforeOpen();
  const r = await window.api?.getMenu?.();
  if (r?.menu) setModel(r.menu);
}

/** main から来た Menu の写しを入れる。開いている段は同じ位置のまま描き直す。 */
function setModel(menu) {
  const key = JSON.stringify(menu);
  if (key === M.key) return;
  M.key = key;
  M.model = menu;
  renderBar();
  if (!M.levels.length) return;
  const list = tops();
  if (M.top >= list.length) { deactivate({ restore: true }); return; }
  const sels = M.levels.map((lv) => lv.sel);
  const btn = $('#menubar').children[M.top];
  closeFrom(0);
  let items = list[M.top].submenu;
  let rect = btn.getBoundingClientRect();
  for (let k = 0; k < sels.length && items; k++) {
    const lv = buildLevel(items, rect, k);
    lv.sel = sels[k] < lv.items.length ? sels[k] : -1;
    M.levels.push(lv);
    const it = lv.items[lv.sel];
    if (k + 1 < sels.length && it?.submenu && it.enabled) {
      items = it.submenu;
      rect = lv.el.children[lv.sel].getBoundingClientRect();
    } else break;
  }
  [...M.levels].forEach((lv) => [...lv.el.children].forEach((c, i) => c.classList.toggle('sel', i === lv.sel)));
  const last = M.levels[M.levels.length - 1];
  (last.el.children[last.sel] || last.el).focus({ preventScroll: true });
}

// ---------------------------------------------------------------- マウス
function onTopDown(e, i) {
  if (e.button !== 0) return;
  e.preventDefault();
  if (M.levels.length && M.top === i) { deactivate({ restore: true }); return; }
  if (!M.active) M.prevFocus = document.activeElement;
  openTop(i, { sel: -1 });
}

function inside(t) {
  return !!(t && (t.closest?.('#menubar') || t.closest?.('.mbd')));
}

// ---------------------------------------------------------------- キーボード
function onKeyDown(e) {
  if (!M.active) {
    if (e.key === 'Alt') {
      if (!e.repeat) M.altArmed = !e.ctrlKey && !e.shiftKey && !e.metaKey;
      return;
    }
    M.altArmed = false;
    if (e.key === 'F10' && !e.altKey && !e.ctrlKey && !e.shiftKey && !e.metaKey
      && !hooks.blocked() && !hooks.keyTaken('F10')) {
      e.preventDefault();
      e.stopPropagation();
      activate();
    }
    return;
  }
  // メニューバーにフォーカスがある間は、キーはメニューだけが受ける
  e.preventDefault();
  e.stopPropagation();
  const lv = M.levels[M.levels.length - 1];
  const depth = M.levels.length;
  switch (e.key) {
    case 'Alt':
      if (!e.repeat) M.altArmed = true;
      return;
    case 'F10':
      deactivate();
      return;
    case 'Escape':
      if (depth > 1) { closeFrom(depth - 1); paintSel(M.levels[depth - 2]); } else if (depth === 1) {
        closeFrom(0);
        renderBar();
        $('#menubar').children[M.top]?.focus({ preventScroll: true });
      } else deactivate();
      return;
    case 'ArrowLeft':
      if (depth > 1) { closeFrom(depth - 1); paintSel(M.levels[depth - 2]); return; }
      moveTop(-1, depth > 0);
      return;
    case 'ArrowRight':
      if (depth > 0 && lv.items[lv.sel]?.submenu && lv.items[lv.sel].enabled) { openSub(depth - 1, { first: true }); return; }
      moveTop(1, depth > 0);
      return;
    case 'ArrowDown':
      if (!depth) { openTop(M.top, { sel: 0 }); return; }
      lv.sel = step(lv, lv.sel, 1);
      paintSel(lv);
      return;
    case 'ArrowUp':
      if (!depth) { openTop(M.top, { last: true }); return; }
      lv.sel = step(lv, lv.sel < 0 ? 0 : lv.sel, -1);
      paintSel(lv);
      return;
    case 'Home':
    case 'End':
      if (depth) { lv.sel = e.key === 'Home' ? step(lv, -1, 1) : step(lv, 0, -1); paintSel(lv); }
      return;
    case 'Enter':
    case ' ':
      if (!depth) { openTop(M.top, { sel: 0 }); return; }
      if (lv.sel >= 0) choose(depth - 1, lv.sel);
      return;
    default:
  }
}

function moveTop(dir, reopen) {
  const n = tops().length;
  const i = (((M.top + dir) % n) + n) % n;
  if (reopen) { openTop(i, { sel: 0 }); return; }
  M.top = i;
  renderBar();
  $('#menubar').children[i]?.focus({ preventScroll: true });
}

function onKeyUp(e) {
  if (e.key !== 'Alt') {
    if (M.active) { e.preventDefault(); e.stopPropagation(); }
    return;
  }
  const armed = M.altArmed;
  M.altArmed = false;
  if (!armed) return;
  // 画面にも届ける（interact.js が Alt を離したことで境目の予告を消す）
  e.preventDefault();
  if (M.active) { deactivate(); return; }
  if (hooks.blocked()) return;
  activate();
}

/** 中央の名前（ウィンドウのタイトルと同じ「名前*」。何も開いていなければ空）。 */
function setTitle(t) {
  const el = $('#docTitle');
  if (!el || !t) return;
  el.textContent = t.doc || '';
  el.title = t.title || '';
}

/** hooks: beforeOpen = 開く前にメニューの中身を main に送り直す（commands.js の flushAppMenu）、
 * blocked = Alt・F10 でメニューバーへ行かないとき（キーの設定画面）、keyTaken(key) = そのキーがコマンドに割り当て済み。 */
export function installTitlebar(h = {}) {
  Object.assign(hooks, h);
  window.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('keyup', onKeyUp, true);
  window.addEventListener('pointerdown', (e) => {
    M.altArmed = false;
    if (M.active && !inside(e.target)) deactivate({ restore: false });
  }, true);
  window.addEventListener('wheel', () => { M.altArmed = false; }, { capture: true, passive: true });
  // 画面が Alt を修飾に使った（はさみの吸着の切り替え・境目の予告・Alt+ドラッグなど。preload の consumeAlt）
  window.addEventListener('gliss-alt-consumed', () => { M.altArmed = false; });
  window.addEventListener('blur', () => { M.altArmed = false; if (M.active) deactivate({ restore: false }); });
  window.addEventListener('resize', () => { if (M.levels.length) deactivate({ restore: false }); });
  // 右クリックはメニューの上では何もしない（画面の右クリックのメニューを出さない）
  document.addEventListener('contextmenu', (e) => { if (inside(e.target)) e.preventDefault(); }, true);
  window.api?.onAppMenu?.((menu) => setModel(menu));
  window.api?.onTitle?.(setTitle);
  window.api?.getMenu?.().then((r) => { if (r?.menu) setModel(r.menu); setTitle(r); });
  renderBar();
}

/** テスト用: 描いているメニューの写しと、開いている状態。 */
export function menubarState() {
  return {
    model: M.model,
    active: M.active,
    top: M.top,
    open: M.levels.map((lv) => ({ sel: lv.sel, labels: lv.items.map((it) => it.label) })),
  };
}
