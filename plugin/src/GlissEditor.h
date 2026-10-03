#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_extra/juce_gui_extra.h>

#include "GlissProcessor.h"
#include "ara/DocumentBridge.h"
#include "editor/EditorWebView.h"

namespace gliss
{

/** プラグインの窓（DAW のエディタ欄）。ARA の EditorView に結び付いていれば、ドキュメント（DocumentBridge）につないだ
    画面（editor::EditorWebView。WebView2 に app/renderer）を出し、EditorView の選択の変化をドキュメントへ渡す。
    ARA に結び付いていない（ARA に対応しないホスト・普通の VST3 として挿された）ときは、短い案内だけを出す。 */
class GlissEditor final : public juce::AudioProcessorEditor,
                          private juce::AudioProcessorEditorARAExtension,
                          private juce::ARAEditorView::Listener
{
public:
    explicit GlissEditor (GlissProcessor& processor);
    ~GlissEditor() override;

    void paint (juce::Graphics&) override;
    void resized() override;

    juce::AudioProcessorEditorARAExtension* getARAClientExtensions() override { return this; }

    /** ARA の EditorView のドキュメントの DocumentBridge（GlissDocumentController が実装する）。無ければ nullptr。 */
    static DocumentBridge* findDocumentBridge (juce::ARAEditorView* editorView);

private:
    // ARAEditorView::Listener
    void onNewSelection (const juce::ARAViewSelection& selection) override;

    DocumentBridge* bridge = nullptr;
    std::unique_ptr<editor::EditorWebView> webView;

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR (GlissEditor)
};

} // namespace gliss
