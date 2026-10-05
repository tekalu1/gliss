#include "EditorWebView.h"

#include "../Diagnostics.h"
#include "../ProcessUtils.h"
#include "EmbeddedAssets.h"
#include "KeyForwarding.h"

namespace gliss::editor
{

namespace
{
const juce::Colour background (0xff1e1e24);

juce::String text (const char* utf8) { return juce::String::fromUTF8 (utf8); }

/** pickFile で最後に選んだフォルダ（プロセスの中だけで覚える）。 */
juce::File& lastPickedFolder()
{
    static juce::File folder;
    return folder;
}

bool isWebUrl (const juce::String& url)
{
    return url.startsWithIgnoreCase ("https://") || url.startsWithIgnoreCase ("http://");
}
}

//==============================================================================
/** ページの移動を見張る WebBrowserComponent。画面の外（http(s) のリンク）は既定のブラウザで開き、WebView では開かない。 */
class EditorWebView::Browser final : public juce::WebBrowserComponent
{
public:
    Browser (const Options& options, EditorWebView& ownerIn) : WebBrowserComponent (options), owner (ownerIn) {}

    bool pageAboutToLoad (const juce::String& url) override
    {
        if (url.startsWith (getResourceProviderRoot()) || url.startsWith ("about:") || url.startsWith ("data:"))
        {
            owner.pageWillLoad();
            return true;
        }

        if (isWebUrl (url))
            juce::URL (url).launchInDefaultBrowser();

        return false;
    }

    void newWindowAttemptingToLoad (const juce::String& url) override
    {
        if (isWebUrl (url) && ! url.startsWith (getResourceProviderRoot()))
            juce::URL (url).launchInDefaultBrowser();
    }

private:
    EditorWebView& owner;
};

//==============================================================================
EditorWebView::UserDataFolder::UserDataFolder()
{
    const auto temp = juce::File::getSpecialLocation (juce::File::tempDirectory);
    const auto prefix = juce::String ("GlissARA-");

    // 終了のときに WebView2 がまだフォルダを掴んでいて消せなかった、前のプロセスの残りを片付ける。
    for (const auto& old : temp.findChildFiles (juce::File::findDirectories, false, prefix + "*"))
        if (const auto pid = old.getFileName().substring (prefix.length()).getIntValue(); pid > 0 && ! isProcessRunning (pid))
            old.deleteRecursively();

    folder = temp.getChildFile (prefix + juce::String (currentProcessId()));
    folder.createDirectory();
}

EditorWebView::UserDataFolder::~UserDataFolder()
{
    // WebView2 のブラウザプロセスが終わるのを待たないので、消せなければそのままにする。
    folder.deleteRecursively();
}

//==============================================================================
EditorWebView::EditorWebView (DocumentBridge& bridgeIn)
    : bridge (bridgeIn),
      resources (&embeddedPageAsset,
                 embeddedJuceInterop(),
                 WebResources::devFolderFromEnvironment(),
                 [&bridgeIn] (const juce::File& file) { return bridgeIn.isReadableByEditor (file); })
{
    if (resources.isServingFromFolder())
        diag::log ("editor: serving the page from GLISS_PLUGIN_WEB_DIR");

    const auto options = makeOptions();

    if (juce::WebBrowserComponent::areOptionsSupported (options))
    {
        browser = std::make_unique<Browser> (options, *this);
        addAndMakeVisible (*browser);
        browser->goToURL (juce::WebBrowserComponent::getResourceProviderRoot());
    }
    else
    {
        diag::log ("editor: WebView2 is not available");
    }

    bridge.addListener (this);
}

EditorWebView::~EditorWebView()
{
    // The document can remain alive after this native editor is closed.
    bridge.preview ("stop", {}, [] (const juce::var&) {});
    diag::log ("editor: preview stopped on close");
    bridge.removeListener (this);
    pendingConfirm = nullptr;
    messageBox.close();
    browser.reset();
}

juce::WebBrowserComponent::Options EditorWebView::makeOptions()
{
    using Options = juce::WebBrowserComponent::Options;
    using Args = const juce::Array<juce::var>&;

    auto bridgeScript = resources.bridgeScript();

    if (bridgeScript.isEmpty())
        diag::log ("editor: ara-bridge.js was not found; window.api will be missing");

    auto keyScript = embeddedKeyForwardScript();

    if (resources.isServingFromFolder())
        keyScript = keyScript.replace ("/*GLISS_DEV*/false", "true");

    return Options()
        .withBackend (Options::Backend::webview2)
        .withWinWebView2Options (Options::WinWebView2()
                                     .withUserDataFolder (userDataFolder->folder)
                                     .withBackgroundColour (background)
                                     .withStatusBarDisabled()
                                     .withBuiltInErrorPageDisabled())
        .withNativeIntegrationEnabled()
        // DAW がエディタを隠しても（別のタブなど）画面を読み直さない。隠れている間の知らせは捨て、画面は見えたときに hostState() で引き直す
        .withKeepPageLoadedWhenBrowserIsHidden()
        .withResourceProvider ([this] (const juce::String& path) -> std::optional<juce::WebBrowserComponent::Resource>
                               {
                                   auto resource = resources.get (path);
                                   return juce::WebBrowserComponent::Resource { std::move (resource.data), resource.mimeType };
                               })
        .withUserScript (bridgeScript)
        .withUserScript (keyScript)
        .withEventListener ("ui-ready", [this] (juce::var)
                            {
                                uiReady = true;
                                diag::log ("editor: ui-ready");
                            })
        .withEventListener ("gliss-key", [this] (juce::var payload) { forwardKey (payload); })

        // ---- ドキュメントへ渡すもの
        .withNativeFunction ("engineCall", [this] (Args args, Completion done)
                             {
                                 const auto tool = args[0].toString();

                                 if (tool.isEmpty())
                                 {
                                     auto* error = new juce::DynamicObject();
                                     error->setProperty ("ok", false);
                                     error->setProperty ("error", text ("ツールの名前が無い"));
                                     return done (juce::var (error));
                                 }

                                 bridge.engineCall (tool, args[1], guarded (std::move (done)));
                             })
        .withNativeFunction ("bootstrap", [this] (Args, Completion done) { done (bridge.bootstrap()); })
        .withNativeFunction ("saveState", [this] (Args args, Completion done)
                             {
                                 bridge.saveState (args[0]);
                                 done (true);
                             })
        .withNativeFunction ("transport", [this] (Args args, Completion done) { done (bridge.transport (args[0].toString(), args[1])); })
        .withNativeFunction ("preview", [this] (Args args, Completion done)
                             { bridge.preview (args[0].toString(), args[1], guarded (std::move (done))); })
        .withNativeFunction ("setCompare", [this] (Args args, Completion done)
                             {
                                 bridge.setCompare ((bool) args[0]);
                                 done (true);
                             })
        .withNativeFunction ("hostState", [this] (Args, Completion done) { done (bridge.hostState()); })
        .withNativeFunction ("restartEngine", [this] (Args, Completion done) { bridge.restartEngine (guarded (std::move (done))); })

        // ---- エディタが答えるもの
        .withNativeFunction ("pickFile", [this] (Args args, Completion done) { pickFile (args[0].toString(), guarded (std::move (done))); })
        .withNativeFunction ("confirm", [this] (Args args, Completion done) { confirm (args[0], guarded (std::move (done))); })
        .withNativeFunction ("copyText", [] (Args args, Completion done)
                             {
                                 juce::SystemClipboard::copyTextToClipboard (args[0].toString());
                                 done (true);
                             })
        .withNativeFunction ("reveal", [this] (Args args, Completion done) { done (reveal (args[0].toString())); });
}

EditorWebView::Completion EditorWebView::guarded (Completion completion)
{
    return [safeThis = juce::Component::SafePointer<EditorWebView> (this), completion = std::move (completion)] (juce::var result)
    {
        auto deliver = [safeThis, completion, result = std::move (result)]
        {
            if (safeThis != nullptr && safeThis->browser != nullptr)
                completion (result);
        };

        if (juce::MessageManager::existsAndIsCurrentThread())
            deliver();
        else
            juce::MessageManager::callAsync (std::move (deliver));
    };
}

void EditorWebView::evaluateJavascript (const juce::String& script, juce::WebBrowserComponent::EvaluationCallback callback)
{
    if (browser != nullptr)
        browser->evaluateJavascript (script, std::move (callback));
    else if (callback != nullptr)
        callback (juce::WebBrowserComponent::EvaluationResult (juce::WebBrowserComponent::EvaluationResult::Error { {}, "no browser" }));
}

void EditorWebView::pageWillLoad()
{
    // 読み直す（開発時の再読み込みなど）と、新しいページが ui-ready を送るまで知らせを止める
    uiReady = false;
}

void EditorWebView::documentEvent (const juce::String& name, const juce::var& data)
{
    if (! juce::MessageManager::existsAndIsCurrentThread())
    {
        // DocumentBridge はメッセージスレッドから呼ぶ約束。外れていたらメッセージスレッドへ移す
        jassertfalse;
        juce::MessageManager::callAsync ([safeThis = juce::Component::SafePointer<EditorWebView> (this), name, data]
        {
            if (safeThis != nullptr)
                safeThis->documentEvent (name, data);
        });
        return;
    }

    if (browser == nullptr || ! uiReady)
    {
        ++eventsDropped;
        return;
    }

    browser->emitEventIfBrowserIsVisible (name, data);
    ++eventsSent;
}

void EditorWebView::pickFile (const juce::String& kind, Completion done)
{
    juce::String title, patterns;

    if (kind == "guide")
    {
        title = text ("ガイドの WAV を選ぶ（任意）");
        patterns = "*.wav;*.flac;*.aiff;*.aif";
    }
    else if (kind == "lyrics")
    {
        title = text ("歌詞のテキストを選ぶ");
        patterns = "*.txt;*.lab;*.csv;*.md";
    }
    else if (kind == "score")
    {
        title = text ("歌詞付きの SVP / MIDI を選ぶ");
        patterns = "*.svp;*.mid;*.midi";
    }

    // 知らない種類・既に開いている（2 つ目は取り消し扱い）
    if (patterns.isEmpty() || chooser != nullptr)
        return done (juce::var());

    chooser = std::make_unique<juce::FileChooser> (title, lastPickedFolder(), patterns);
    chooser->launchAsync (juce::FileBrowserComponent::openMode | juce::FileBrowserComponent::canSelectFiles,
                          [safeThis = juce::Component::SafePointer<EditorWebView> (this), kind, done] (const juce::FileChooser& fc)
    {
        const auto file = fc.getResult();

        // FileChooser は自分の callback の中で壊さない
        juce::MessageManager::callAsync ([safeThis]
        {
            if (safeThis != nullptr)
                safeThis->chooser.reset();
        });

        if (file == juce::File() || ! file.existsAsFile())
            return done (juce::var());

        lastPickedFolder() = file.getParentDirectory();

        if (kind != "lyrics")
            return done (file.getFullPathName());

        auto* result = new juce::DynamicObject();
        result->setProperty ("path", file.getFullPathName());
        result->setProperty ("text", file.loadFileAsString());
        done (juce::var (result));
    });
}

void EditorWebView::confirm (const juce::var& request, Completion done)
{
    const auto title = request.getProperty ("title", "Gliss").toString();
    const auto message = request.getProperty ("message", {}).toString();
    const auto ok = request.getProperty ("ok", "OK").toString();
    const auto cancel = request.getProperty ("cancel", text ("キャンセル")).toString();

    const auto options = juce::MessageBoxOptions::makeOptionsOkCancel (juce::MessageBoxIconType::QuestionIcon,
                                                                        title, message, ok, cancel, this);

    // 新しい確認が来たら前のものは閉じる（ScopedMessageBox を置き換えると前の callback は呼ばれない）。前の Promise は false で返す
    if (pendingConfirm != nullptr)
        std::exchange (pendingConfirm, nullptr) (false);

    pendingConfirm = done;
    messageBox = juce::NativeMessageBox::showScopedAsync (options, [safeThis = juce::Component::SafePointer<EditorWebView> (this)] (int result)
    {
        if (safeThis != nullptr && safeThis->pendingConfirm != nullptr)
            std::exchange (safeThis->pendingConfirm, nullptr) (result == 1);
    });
}

bool EditorWebView::reveal (const juce::String& path)
{
    if (path.isEmpty() || ! juce::File::isAbsolutePath (path))
        return false;

    const juce::File file (path);

    if (! file.exists() || ! bridge.isReadableByEditor (file))
        return false;

    file.revealToUser();
    return true;
}

void EditorWebView::forwardKey (const juce::var& payload)
{
    const auto key = ForwardedKey::fromVar (payload);
    auto* peer = getPeer();

    if (key.has_value() && peer != nullptr && postKeyToWindow (peer->getNativeHandle(), *key))
        ++keysForwarded;
}

void EditorWebView::paint (juce::Graphics& g)
{
    g.fillAll (background);

    if (browser == nullptr)
    {
        g.setColour (juce::Colours::white);
        g.setFont (juce::FontOptions (15.0f));
        g.drawFittedText (text ("Gliss の画面を出せない: WebView2 のランタイムが見つからない。"),
                          getLocalBounds().reduced (16), juce::Justification::centred, 4);
    }
}

void EditorWebView::resized()
{
    if (browser != nullptr)
        browser->setBounds (getLocalBounds());
}

} // namespace gliss::editor
