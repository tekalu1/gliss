# C4 エディタと画面の橋（GlissEditor が使う）。
# WebResources は juce_core だけで書いてあり、単体テスト（GlissPluginTests）にも入る。
list(APPEND GLISS_UNIT_SOURCES
    src/editor/WebResources.cpp)

# WebView2・埋め込みの資源に触るものは、プラグインと GlissHostCheck（tests/hostcheck）だけに入れる
# （単体テストは juce_gui_extra を持たない）。
set(GLISS_EDITOR_GUI_SOURCES
    src/editor/EditorWebView.cpp
    src/editor/EmbeddedAssets.cpp
    src/editor/KeyForwarding.cpp)
target_sources(GlissARA PRIVATE ${GLISS_EDITOR_GUI_SOURCES})
