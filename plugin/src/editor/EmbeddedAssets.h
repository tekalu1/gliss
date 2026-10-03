#pragma once

#include <juce_core/juce_core.h>

#include <optional>

namespace gliss::editor
{

/** DLL に埋め込んだ画面の資源（plugin/CMakeLists.txt の GlissWebAssets＝app/renderer の index.html と *.js）を
    元のファイル名で引く。無ければ nullopt。 */
std::optional<juce::MemoryBlock> embeddedPageAsset (const juce::String& fileName);

/** JUCE 9.0.3 の webview-interop の dist/index.js（GlissEditorAssets）。画面の `/juce/index.js`。 */
juce::MemoryBlock embeddedJuceInterop();

/** plugin/src/editor/key-forward.js（GlissEditorAssets）。 */
juce::String embeddedKeyForwardScript();

} // namespace gliss::editor
