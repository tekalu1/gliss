#pragma once

#include <juce_audio_processors/juce_audio_processors.h>

#include <functional>

namespace gliss
{

/** エディタ（WebView2 の画面）がドキュメントに頼む口。C3（GlissDocumentController）が実装し、C4（GlissEditor）が使う。
    形は app/renderer/ara-bridge.js（画面が期待するネイティブ関数・イベント）に合わせる（docs/ara-plugin.md の「エディタ」）。

    スレッド: すべてメッセージスレッドから呼ぶ。中で待たない。結果は Completion で（メッセージスレッドで）返す。
    値はすべて JSON にできる juce::var。エンジンの {ok:false} も例外にせず値で返す。 */
class DocumentBridge
{
public:
    using Completion = std::function<void (juce::var result)>;

    virtual ~DocumentBridge() = default;

    // ---- ネイティブ関数（JS → C++） ----

    /** エンジンのツールを呼ぶ（禁止の一覧なら {ok:false} で断る）。成功したら DAW との同期を予約する。 */
    virtual void engineCall (const juce::String& tool, const juce::var& args, Completion done) = 0;

    /** 起動の値: { engineReady, engineError, version, keys, grid, preview, view, selection, compare, hostCanTransport, fileGuide }。 */
    virtual juce::var bootstrap() = 0;

    /** 画面の設定（表示範囲・キー・グリッドなど）を浅く足して保存する。 */
    virtual void saveState (const juce::var& patch) = 0;

    /** 再生の制御。op: play・stop・toggle・seek・loop。返り値 { ok, reason? }（制御が無ければ reason "no-controller"）。 */
    virtual juce::var transport (const juce::String& op, const juce::var& arg) = 0;

    /** つかんだノートの試聴。start の WAV 読み込みは作業スレッドで行い、結果を非同期に返す。 */
    virtual void preview (const juce::String& op, const juce::var& arg, Completion done) = 0;

    /** 原音と比べる（編集を外して鳴らす）。 */
    virtual void setCompare (bool on) = 0;

    /** 取りこぼしの補い: { selection, playhead, tracks: [{ track_id, ara_id, regions, cache: { state, rev } }], engine: { state, error } }。 */
    virtual juce::var hostState() = 0;

    /** エンジンを起動し直す。返り値 { ok }。 */
    virtual void restartEngine (Completion done) = 0;

    /** /fs/ で読ませてよいファイルか（作業場所の下だけ。正規化した絶対パスで判定）。 */
    virtual bool isReadableByEditor (const juce::File& file) = 0;

    // ---- イベント（C++ → JS） ----

    /** ドキュメントからエディタへの知らせ。name: playhead・selection・session-changed・project-changed・cache・engine。
        メッセージスレッドで呼ばれる。エディタは emitEventIfBrowserIsVisible で画面へ送る。 */
    struct Listener
    {
        virtual ~Listener() = default;
        virtual void documentEvent (const juce::String& name, const juce::var& data) = 0;
    };

    virtual void addListener (Listener*) = 0;
    virtual void removeListener (Listener*) = 0;

    /** エディタの EditorView（view = その識別）で DAW の選択が変わった。SelectionPolicy が編集対象を切り替えるかを決め、
        切り替えるなら track_id・region の形にして "selection" のイベントで返す。 */
    virtual void editorSelectionChanged (const void* view, const juce::ARAViewSelection& selection) = 0;

    /** エディタの窓が見える・見えなくなった（隠れた・別のインスタンスのエディタの選択は、共有する選択を上書きしない）。 */
    virtual void editorVisibilityChanged (const void* view, bool showing) = 0;

    /** DAW の選択をもう 1 度でも受けたか（開いた直後のエディタが、すでにある選択を上書きしないための問い合わせ）。 */
    virtual bool hasEditorSelection() const = 0;
};

} // namespace gliss
