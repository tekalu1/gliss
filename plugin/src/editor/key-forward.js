// 画面（WebView2）が使わなかったキーを DAW へ渡す。plugin/src/editor/EditorWebView.cpp が
// WebBrowserComponent::Options::withUserScript で ara-bridge.js の後に差し込む（クラシックのスクリプト）。
//
// WebView2 にフォーカスがあると、キーはブラウザのプロセスへ行き、DAW の窓には届かない。画面のキー（commands.js など）は
// 自分の使うキーで preventDefault するので、window の bubble の段（ページのどの受け手よりも後）で
// 「誰も preventDefault しなかったキー」だけを C++ へイベント gliss-key で送る。C++ はそれをプラグインの窓に
// WM_KEYDOWN / WM_KEYUP として置き、JUCE が使わなかったキーとして DAW の窓へ渡す（JUCE の普通のプラグインと同じ道）。
// 渡したキーは preventDefault して、ブラウザ自身の動き（印刷・検索・拡大・再読み込み）をさせない。
// 文字の入力欄・IME の変換中・修飾キーだけ・Tab（フォーカスの移動）・Esc・F10（画面のメニューバー）は渡さない。
(() => {
  'use strict';
  if (window.__glissKeyForward) return;
  window.__glissKeyForward = true;

  const DEV = /*GLISS_DEV*/false;   // 開発時（GLISS_PLUGIN_WEB_DIR）は F5・Ctrl+R をブラウザの再読み込みに残す
  const backend = window.__JUCE__ && window.__JUCE__.backend;
  if (!backend || typeof backend.emitEvent !== 'function') return;

  const NEVER = new Set(['Alt', 'AltGraph', 'Control', 'Shift', 'Meta', 'CapsLock', 'NumLock', 'ScrollLock',
    'Tab', 'Escape', 'F10', 'Process', 'Dead', 'Unidentified', 'ContextMenu']);
  const editable = (t) => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''));
  const pressesButton = (e) => !!e.target && e.target.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter');
  const reload = (e) => DEV && (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && e.code === 'KeyR'));
  const idOf = (e) => e.code || String(e.keyCode);
  const forwarded = new Set();

  const send = (type, e) => backend.emitEvent('gliss-key', {
    type, keyCode: e.keyCode | 0, code: String(e.code || ''), key: String(e.key || ''),
    ctrl: !!e.ctrlKey, shift: !!e.shiftKey, alt: !!e.altKey, meta: !!e.metaKey, repeat: !!e.repeat,
  });

  window.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.isComposing || !e.keyCode || NEVER.has(e.key)
      || editable(e.target) || pressesButton(e) || reload(e)) return;
    e.preventDefault();
    forwarded.add(idOf(e));
    send('keydown', e);
  });
  window.addEventListener('keyup', (e) => {
    if (!forwarded.delete(idOf(e))) return;
    send('keyup', e);
  });
  window.addEventListener('blur', () => forwarded.clear());
})();
