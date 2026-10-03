#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_extra/juce_gui_extra.h>

#include "GlissProcessor.h"

namespace gliss
{

/** プラグインの窓（DAW のエディタ欄）。WebView2 に plugin/web の静的な HTML を出す。
    ARA の EditorView で選ばれているリージョンを、JS のイベント "selection" で画面へ送る。 */
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

private:
    /** WebView2 のユーザーデータのフォルダ。プロセスごとの一時フォルダで、最後のエディタが閉じたときに消す。
        既定の場所は DAW によっては書き込めず、複数の DAW や複数のプロセスで同じフォルダを奪い合うと固まる。 */
    struct UserDataFolder
    {
        UserDataFolder();
        ~UserDataFolder();
        juce::File folder;
    };

    // ARAEditorView::Listener
    void onNewSelection (const juce::ARAViewSelection& selection) override;

    static juce::var describeSelection (const juce::ARAViewSelection* selection);
    void sendSelection();

    juce::SharedResourcePointer<UserDataFolder> userDataFolder;   // webView より先に作り、後に壊す
    std::unique_ptr<juce::WebBrowserComponent> webView;
    juce::var latestSelection;

    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR (GlissEditor)
};

} // namespace gliss
