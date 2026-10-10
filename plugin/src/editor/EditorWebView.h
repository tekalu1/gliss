#pragma once

#include <juce_gui_extra/juce_gui_extra.h>

#include <functional>

#include "WebResources.h"
#include "../ara/DocumentBridge.h"

namespace gliss::editor
{

/** エディタの画面（app/renderer）を出す WebView2 と、画面 ↔ ドキュメント（DocumentBridge）の橋。

    - ara-bridge.js（window.api を作る）と key-forward.js を withUserScript で差し込み、resource provider（WebResources）で
      `/`・`/<名前>.js`・`/juce/index.js`・`/fs/…` を配る。
    - ネイティブ関数（engineCall・bootstrap・saveState・transport・preview・setCompare・hostState・restartEngine）は
      DocumentBridge へ渡し、pickFile・confirm・copyText・reveal はここで答える。どれも中で待たない。
    - 画面のイベント ui-ready を受けてから、DocumentBridge::Listener の知らせを emitEventIfBrowserIsVisible で送る
      （それより前の知らせは捨てる。画面は起動のときに hostState() で引き直す）。
    - 画面が使わなかったキー（イベント gliss-key）を、このコンポーネントのピアの窓に置く（KeyForwarding.h）。

    DocumentBridge はこれより長く生きること（ARA の DocumentController はエディタより後に壊れる）。
    メッセージスレッドで作り、壊す。 */
class EditorWebView final : public juce::Component,
                            private DocumentBridge::Listener
{
public:
    explicit EditorWebView (DocumentBridge& bridge);
    ~EditorWebView() override;

    /** WebView2 を作れたか（ランタイムが無ければ false。文言を出す）。 */
    bool hasBrowser() const { return browser != nullptr; }

    /** 画面が ui-ready を送ってきたか（ページを読み直すと false に戻る）。 */
    bool isUiReady() const { return uiReady; }

    /** 開発時のフォルダ（GLISS_PLUGIN_WEB_DIR）から画面を読んでいるか。 */
    bool isServingFromFolder() const { return resources.isServingFromFolder(); }

    /** 画面へ送った知らせの数・捨てた数（ui-ready の前・WebView2 の無いとき）・DAW へ渡したキーの数（検証用）。 */
    int getNumEventsSent() const { return eventsSent; }
    int getNumEventsDropped() const { return eventsDropped; }
    int getNumKeysForwarded() const { return keysForwarded; }

    /** 試聴を求めたこのエディタのインスタンスの EditorRenderer の id を返す関数（画面の preview に requester として添える。
        返す関数が無い・0 のときは添えない）。メッセージスレッドから呼ばれる。 */
    void setRequesterProvider (std::function<std::uint64_t()> provider) { requesterProvider = std::move (provider); }

    /** 検証用: 画面で JS を評価する。 */
    void evaluateJavascript (const juce::String& script, juce::WebBrowserComponent::EvaluationCallback callback = nullptr);

    void paint (juce::Graphics&) override;
    void resized() override;

private:
    class Browser;

    /** WebView2 のユーザーデータのフォルダ。プロセスごとの一時フォルダで、最後のエディタが閉じたときに消す。
        既定の場所は DAW によっては書き込めず、複数の DAW や複数のプロセスで同じフォルダを奪い合うと固まる。 */
    struct UserDataFolder
    {
        UserDataFolder();
        ~UserDataFolder();
        juce::File folder;
    };

    using Completion = juce::WebBrowserComponent::NativeFunctionCompletion;

    juce::WebBrowserComponent::Options makeOptions();

    /** completion をメッセージスレッドへ移し、このエディタが閉じていれば捨てる（JUCE の completion は
        WebBrowserComponent が壊れた後に呼ぶと解放済みのものを触る）。 */
    Completion guarded (Completion completion);

    void pickFile (const juce::String& kind, Completion done);
    void confirm (const juce::var& request, Completion done);
    bool reveal (const juce::String& path);
    void forwardKey (const juce::var& payload);
    void pageWillLoad();

    // DocumentBridge::Listener
    void documentEvent (const juce::String& name, const juce::var& data) override;

    DocumentBridge& bridge;
    WebResources resources;
    juce::SharedResourcePointer<UserDataFolder> userDataFolder;   // browser より先に作り、後に壊す
    std::unique_ptr<Browser> browser;
    std::unique_ptr<juce::FileChooser> chooser;
    juce::ScopedMessageBox messageBox;
    Completion pendingConfirm;
    bool uiReady = false;
    std::function<std::uint64_t()> requesterProvider;
    int eventsSent = 0, eventsDropped = 0, keysForwarded = 0;

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR (EditorWebView)
};

} // namespace gliss::editor
