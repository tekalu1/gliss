// 再生位置に表示範囲を追従させる規則（下のピアノロールの draw.js の follow・上のトラックの tracks.js の moveHead・単体の audio.js の
// tick が共通で使う。単体試験は tests/unit/follow.spec.js）。画面の部品を import しない（状態を持つだけ）。
//
//  - 停止中は追従しない。ただし DAW 側で再生位置が動いた（ロケート・シーク）ときだけ、再生位置が表示外なら 1 回寄せる（locate）。
//  - 再生中、利用者が表示範囲を動かした（スクロール・ズーム・ナビゲーター・ミニマップなど、追従が置いた範囲と違う）ら追従を止め、
//    再生位置が表示範囲に戻るか、次の再生が始まったら再開する。
// 表示範囲を動かす操作は多くのファイルに散らばるので、操作ごとには見ない: 追従が最後に見た・置いた範囲（seen）と、次の呼び出しで
// 見る範囲が違えば、追従以外が動かしたと見なす。
export function createFollower() {
  let seen = null;       // 最後に見た・置いた範囲 { t0, span }
  let held = false;      // 利用者が動かした: 追従を止めている
  let was = false;       // 前の呼び出しで再生中だったか

  return {
    /** 追従して範囲を動かすべきなら true。
     *  playing: 再生中か。locate: 停止中に再生位置が動いた知らせか。view: 今の範囲 { t0, span }。
     *  head: view と同じ時間軸の再生位置。valid: 再生位置がこの範囲の対象の中にあるか（編集中のトラックの外なら false）。 */
    check({ playing, locate = false, view, head, valid = true }) {
      if (playing && !was) { held = false; seen = null; }          // 次の再生が始まった: 追従を再開する
      was = !!playing;
      const moved = seen !== null && (Math.abs(seen.t0 - view.t0) > 1e-9 || Math.abs(seen.span - view.span) > 1e-9);
      seen = { t0: view.t0, span: view.span };
      if (playing && moved) held = true;                            // 再生中に利用者が範囲を動かした
      if (!valid || !Number.isFinite(head)) return false;
      if (head >= view.t0 && head <= view.t0 + view.span) { held = false; return false; }   // 範囲に戻ってきた: 再開
      return playing ? !held : !!locate;
    },
    /** 追従が範囲を動かした（利用者の操作と見なさないよう、置いた範囲を覚える）。 */
    placed(view) { seen = { t0: view.t0, span: view.span }; },
    /** 範囲そのものが無くなった（上の表示が全体表示に戻った）。 */
    forget() { seen = null; held = false; },
  };
}
