#include "GlissEditor.h"

#include "Diagnostics.h"

namespace gliss
{

GlissEditor::GlissEditor (GlissProcessor& processor)
    : AudioProcessorEditor (&processor),
      AudioProcessorEditorARAExtension (&processor)
{
    auto* editorView = getARAEditorView();
    bridge = findDocumentBridge (editorView);

    if (bridge != nullptr)
    {
        webView = std::make_unique<editor::EditorWebView> (*bridge);
        addAndMakeVisible (*webView);

        editorView->addListener (this);
        // 開いた時点の DAW の選択は、ドキュメントにまだ選択が無いときだけ知らせる（画面は bootstrap / hostState でこれを読む）。
        // 隠れたエディタ・別のインスタンスのエディタが作られるたびに、共有する選択を上書きしない
        if (! bridge->hasEditorSelection())
            bridge->editorSelectionChanged (this, editorView->getViewSelection());
        diag::log ("editor: opened with a document");
    }
    else
    {
        diag::log (editorView != nullptr ? "editor: the ARA document has no DocumentBridge; showing the notice"
                                         : "editor: not bound to an ARA document; showing the notice");
    }

    // ARA ではエディタをリサイズできることが求められる（ホストの画面に組み込まれるため）。
    setResizable (true, false);
    setResizeLimits (360, 200, 4096, 4096);
    setSize (bridge != nullptr ? 1100 : 640, bridge != nullptr ? 680 : 360);
}

GlissEditor::~GlissEditor()
{
    if (bridge != nullptr)
    {
        bridge->editorVisibilityChanged (this, false);

        if (auto* editorView = getARAEditorView())
            editorView->removeListener (this);
    }

    webView.reset();
}

DocumentBridge* GlissEditor::findDocumentBridge (juce::ARAEditorView* editorView)
{
    if (editorView == nullptr)
        return nullptr;

    auto* specialisation = juce::ARADocumentControllerSpecialisation::getSpecialisedDocumentController (editorView->getDocumentController());
    return dynamic_cast<DocumentBridge*> (specialisation);
}

void GlissEditor::paint (juce::Graphics& g)
{
    g.fillAll (juce::Colour (0xff1e1e24));

    if (webView == nullptr)
    {
        g.setColour (juce::Colours::white.withAlpha (0.75f));
        g.setFont (juce::FontOptions (15.0f));
        g.drawFittedText (juce::String::fromUTF8 ("Gliss は ARA の拡張として使います。\n"
                                                  "ARA に対応した DAW で、オーディオのイベントに挿してください。"),
                          getLocalBounds().reduced (16), juce::Justification::centred, 4);
    }
}

void GlissEditor::resized()
{
    if (webView != nullptr)
        webView->setBounds (getLocalBounds());
}

void GlissEditor::onNewSelection (const juce::ARAViewSelection& selection)
{
    // ARA の「メインスレッド」から呼ばれる。JUCE のプラグインではメッセージスレッドと同じ（ARAViewSelection の中身は
    // この呼び出しの間だけ有効なので、別のスレッドへは移さない）。
    jassert (juce::MessageManager::existsAndIsCurrentThread());

    if (bridge != nullptr)
        bridge->editorSelectionChanged (this, selection);
}

void GlissEditor::visibilityChanged()
{
    reportVisibility();
}

void GlissEditor::parentHierarchyChanged()
{
    reportVisibility();
}

void GlissEditor::reportVisibility()
{
    if (bridge == nullptr)
        return;

    const auto showing = isShowing();

    if (showing != reportedShowing)
    {
        reportedShowing = showing;
        bridge->editorVisibilityChanged (this, showing);

        // 窓が見えるようになった: 閉じている間（リスナーが無い間）に DAW の選択が変わっていたら拾う（今の修飾が選択に
        // 残っていれば何も変わらない。SelectionPolicy）
        if (showing)
            if (auto* editorView = getARAEditorView())
                bridge->editorSelectionChanged (this, editorView->getViewSelection());
    }
}

} // namespace gliss
