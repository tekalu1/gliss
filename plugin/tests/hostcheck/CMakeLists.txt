# JUCE のホストとして Gliss.vst3 を読み込む確認用のアプリ（Main.cpp の冒頭に確かめることを書いている）。
# --editor はエディタの画面の部品（src/editor）をこのアプリに直に入れて確かめるので、WebView2 も使う。
juce_add_gui_app(GlissHostCheck
    PRODUCT_NAME "GlissHostCheck"
    COMPANY_NAME "Gliss"
    VERSION 1.0.0
    NEEDS_WEBVIEW2 TRUE)

set(_gliss_hostcheck_editor_sources)
foreach(src IN LISTS GLISS_EDITOR_GUI_SOURCES ITEMS src/editor/WebResources.cpp src/Diagnostics.cpp src/ProcessUtils.cpp)
    list(APPEND _gliss_hostcheck_editor_sources "${PROJECT_SOURCE_DIR}/${src}")
endforeach()

target_sources(GlissHostCheck PRIVATE Main.cpp ${_gliss_hostcheck_editor_sources})

# このアプリはプラグイン側の ARA（JucePlugin_Enable_ARA）を持たないので、DocumentBridge.h が使う juce::ARAViewSelection を
# 不完全な型として先に宣言する（AraSelectionShim.h）。EditorWebView.cpp はプラグインと同じファイルなので、ここでだけ前置きする。
set_source_files_properties("${PROJECT_SOURCE_DIR}/src/editor/EditorWebView.cpp" PROPERTIES
    COMPILE_OPTIONS "$<$<CXX_COMPILER_ID:MSVC>:/FI${CMAKE_CURRENT_SOURCE_DIR}/AraSelectionShim.h>")

target_include_directories(GlissHostCheck PRIVATE "${PROJECT_SOURCE_DIR}/src")

target_compile_options(GlissHostCheck PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/utf-8>)

target_compile_definitions(GlissHostCheck PRIVATE
    JUCE_PLUGINHOST_VST3=1
    JUCE_PLUGINHOST_ARA=1
    JUCE_WEB_BROWSER=1
    JUCE_USE_WIN_WEBVIEW2_WITH_STATIC_LINKING=1
    JUCE_USE_CURL=0)

target_link_libraries(GlissHostCheck PRIVATE
    GlissWebAssets
    GlissEditorAssets
    juce::juce_audio_processors
    juce::juce_audio_utils
    juce::juce_gui_extra
    juce::juce_recommended_config_flags
    juce::juce_recommended_lto_flags
    juce::juce_recommended_warning_flags)
