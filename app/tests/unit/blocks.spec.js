import { test, expect } from '@playwright/test';

// タイミングの単位は、音程ノート・子音（無声）・息のすべて（種類によらず同じ規則。無音は隙間）。
// 画面の接続の記号・右クリックの「切り離す」「つなぐ」・当たりは、この並び（S.blocks）の隣り合う組で出す。
test('S.blocks holds every pitched note, consonant and breath in time order; silence is not a block', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  globalThis.window = { api: { mode: 'ara' }, addEventListener() {}, dispatchEvent() {} };
  globalThis.document = { documentElement: { dataset: {} }, querySelector: () => null, addEventListener() {} };
  try {
    const { S, adopt, BLOCK_KINDS } = await import('../../renderer/state.js');
    const n = (id, kind, a, b) => ({ id, kind, start_sec: a, end_sec: b, edited_start_sec: a, edited_end_sec: b });
    const notes = [n('u0', 'unvoiced', 0.0, 0.1), n('a', 'note', 0.1, 0.5), n('s', 'silence', 0.5, 0.6), n('x', 'unvoiced', 0.6, 0.7),
      n('b', 'note', 0.7, 1.0), n('h', 'breath', 1.0, 1.2), n('c', 'note', 1.3, 1.6)].reverse();
    try { adopt({ duration_sec: 2, notes, edits: [], f0: {}, history: {} }); } catch { /* 描画の部品が無い環境では途中で止まってよい */ }
    expect([...BLOCK_KINDS]).toEqual(['note', 'unvoiced', 'breath']);
    expect(S.blocks.map((x) => x.id)).toEqual(['u0', 'a', 'x', 'b', 'h', 'c']);
    // 記号の高さの元: 音程ノートは自身、子音・息は直前の音程ノート（先頭の子音は直後の音程ノート）
    const anchor = (id) => S.blockAnchor.get(id)?.id;
    expect(anchor('a')).toBe('a');
    expect(anchor('x')).toBe('a');
    expect(anchor('h')).toBe('b');
    expect(anchor('u0')).toBe('a');
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
  }
});
