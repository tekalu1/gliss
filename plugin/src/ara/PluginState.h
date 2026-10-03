#pragma once

#include <juce_core/juce_core.h>

namespace gliss
{

/** 画面の設定（表示範囲・キー・グリッドなど）の置き場 plugin-state.json（design-stage23 §1-1 の saveState）。
    単体アプリの state.json とは別のファイル（同時に書いて壊さない）。WebView2 の userDataFolder は一時なので localStorage には置かない。 */
namespace pluginstate
{
/** 既定 %APPDATA%\Gliss\plugin-state.json。環境変数 GLISS_PLUGIN_STATE_FILE で差し替える（試験で利用者の設定を書かないため）。 */
juce::File defaultFile();

/** 読めなければ空のオブジェクト。 */
juce::var load (const juce::File&);

/** base に patch のキーを浅く足した新しいオブジェクト（base は変えない）。patch のキーの値が null なら消す。 */
juce::var merge (const juce::var& base, const juce::var& patch);

/** 一時ファイルに書いてから置き換える。 */
bool save (const juce::File&, const juce::var& state);
} // namespace pluginstate

} // namespace gliss
