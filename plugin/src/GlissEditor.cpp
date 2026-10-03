#include "GlissEditor.h"

#include "BinaryData.h"
#include "Diagnostics.h"
#include "ProcessUtils.h"

namespace gliss
{

namespace
{
std::optional<juce::WebBrowserComponent::Resource> provideResource (const juce::String& path)
{
    if (path != "/" && path != "/index.html")
        return std::nullopt;

    const auto* data = reinterpret_cast<const std::byte*> (BinaryData::index_html);
    return juce::WebBrowserComponent::Resource { std::vector<std::byte> (data, data + BinaryData::index_htmlSize), "text/html" };
}
}

GlissEditor::UserDataFolder::UserDataFolder()
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

GlissEditor::UserDataFolder::~UserDataFolder()
{
    // WebView2 のブラウザプロセスが終わるのを待たないので、消せなければそのままにする。
    folder.deleteRecursively();
}

GlissEditor::GlissEditor (GlissProcessor& processor)
    : AudioProcessorEditor (&processor),
      AudioProcessorEditorARAExtension (&processor)
{
    using Options = juce::WebBrowserComponent::Options;

    const auto options = Options()
        .withBackend (Options::Backend::webview2)
        .withWinWebView2Options (Options::WinWebView2()
                                     .withUserDataFolder (userDataFolder->folder)
                                     .withBackgroundColour (juce::Colour (0xff1e1e24)))
        .withNativeIntegrationEnabled()
        .withResourceProvider (&provideResource)
        .withEventListener ("ready", [this] (juce::var)
                            {
                                diag::log ("editor: page ready");
                                sendSelection();
                            });

    if (juce::WebBrowserComponent::areOptionsSupported (options))
    {
        webView = std::make_unique<juce::WebBrowserComponent> (options);
        addAndMakeVisible (*webView);
        webView->goToURL (juce::WebBrowserComponent::getResourceProviderRoot());
    }
    else
    {
        diag::log ("editor: WebView2 is not available");
    }

    if (auto* editorView = getARAEditorView())
    {
        editorView->addListener (this);
        latestSelection = describeSelection (&editorView->getViewSelection());
    }
    else
    {
        latestSelection = describeSelection (nullptr);
    }

    // ARA ではエディタをリサイズできることが求められる（ホストの画面に組み込まれるため）。
    setResizable (true, false);
    setResizeLimits (360, 200, 4096, 4096);
    setSize (640, 360);
}

GlissEditor::~GlissEditor()
{
    if (auto* editorView = getARAEditorView())
        editorView->removeListener (this);
}

void GlissEditor::paint (juce::Graphics& g)
{
    g.fillAll (juce::Colour (0xff1e1e24));

    if (webView == nullptr)
    {
        g.setColour (juce::Colours::white);
        g.setFont (juce::FontOptions (15.0f));
        g.drawFittedText ("Gliss (ARA)\nWebView2 runtime was not found.", getLocalBounds(), juce::Justification::centred, 3);
    }
}

void GlissEditor::resized()
{
    if (webView != nullptr)
        webView->setBounds (getLocalBounds());
}

void GlissEditor::onNewSelection (const juce::ARAViewSelection& selection)
{
    auto description = describeSelection (&selection);

    // ARA のメインスレッドから呼ばれる。画面（WebView2）はメッセージスレッドでしか触れない。
    if (juce::MessageManager::getInstance()->isThisTheMessageThread())
    {
        latestSelection = std::move (description);
        sendSelection();
        return;
    }

    juce::MessageManager::callAsync ([safeThis = juce::Component::SafePointer<GlissEditor> (this), description = std::move (description)]() mutable
    {
        if (safeThis != nullptr)
        {
            safeThis->latestSelection = std::move (description);
            safeThis->sendSelection();
        }
    });
}

void GlissEditor::sendSelection()
{
    if (webView != nullptr)
        webView->emitEventIfBrowserIsVisible ("selection", latestSelection);
}

juce::var GlissEditor::describeSelection (const juce::ARAViewSelection* selection)
{
    juce::Array<juce::var> regions;

    if (selection != nullptr)
    {
        for (const auto* region : selection->getPlaybackRegions<juce::ARAPlaybackRegion>())
        {
            auto* item = new juce::DynamicObject();
            item->setProperty ("name", juce::convertOptionalARAString (region->getEffectiveName(), "(unnamed)"));
            item->setProperty ("startSeconds", region->getStartInPlaybackTime());
            item->setProperty ("durationSeconds", region->getDurationInPlaybackTime());
            regions.add (juce::var (item));
        }
    }

    auto* root = new juce::DynamicObject();
    root->setProperty ("isARA", selection != nullptr);
    root->setProperty ("regions", regions);
    return juce::var (root);
}

} // namespace gliss
