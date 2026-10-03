# Gliss の VST3 + ARA 2 プラグイン。docs/ara-plugin.md に手順と決まった形を書いている。
#
#   cmake -S plugin -B plugin/build -G "Visual Studio 17 2022" -A x64
#   cmake --build plugin/build --config Release --target GlissARA_VST3
#
# 依存（JUCE・ARA SDK・WebView2）は FetchContent でタグを固定して plugin/build/_deps に取る。リポジトリには入れない。
cmake_minimum_required(VERSION 3.22)

# 実行時ライブラリは静的にする（利用者の PC に VC++ 再頒布可能パッケージを要求しない）。project() より前に置く。
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

project(GlissARA VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# ---- 依存（版は docs/ara-plugin.md の表と合わせる） ----
set(GLISS_JUCE_TAG "9.0.3")
set(GLISS_ARA_SDK_TAG "releases/2.3.0")
set(GLISS_WEBVIEW2_VERSION "1.0.4258.31")
set(GLISS_WEBVIEW2_SHA256 "56f7f4b8bf9aee4b8efefbbdd4f67d5f74ebd1b100ed0806da71bf76af481aa9")

FetchContent_Declare(juce
    GIT_REPOSITORY https://github.com/juce-framework/JUCE.git
    GIT_TAG ${GLISS_JUCE_TAG}
    GIT_SHALLOW TRUE)

# ARA SDK は CMakeLists を持たない（置き場を指すだけ）。ARA_Library・ARA_API・ARA_Examples（サブモジュール）も要る。
FetchContent_Declare(ara_sdk
    GIT_REPOSITORY https://github.com/Celemony/ARA_SDK.git
    GIT_TAG ${GLISS_ARA_SDK_TAG}
    GIT_SHALLOW TRUE
    GIT_SUBMODULES_RECURSE TRUE)

# WebView2 の NuGet パッケージ（静的ライブラリ WebView2LoaderStatic.lib）。JUCE の FindWebView2 は
# 「*Microsoft.Web.WebView2*」という名前のフォルダを JUCE_WEBVIEW2_PACKAGE_LOCATION の下に探す。
set(GLISS_NUGET_DIR "${FETCHCONTENT_BASE_DIR}/nuget")
FetchContent_Declare(webview2
    URL "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/${GLISS_WEBVIEW2_VERSION}"
    URL_HASH SHA256=${GLISS_WEBVIEW2_SHA256}
    DOWNLOAD_NAME "Microsoft.Web.WebView2.${GLISS_WEBVIEW2_VERSION}.zip"
    SOURCE_DIR "${GLISS_NUGET_DIR}/Microsoft.Web.WebView2.${GLISS_WEBVIEW2_VERSION}")

FetchContent_MakeAvailable(ara_sdk webview2 juce)

set(JUCE_WEBVIEW2_PACKAGE_LOCATION "${GLISS_NUGET_DIR}" CACHE PATH "WebView2 の NuGet パッケージを置いた場所" FORCE)
juce_set_ara_sdk_path("${ara_sdk_SOURCE_DIR}")

# ---- 画面（WebView2 に配る静的な HTML。実行ファイルに埋め込む） ----
juce_add_binary_data(GlissWebAssets
    SOURCES web/index.html)

# ---- プラグイン ----
# ID は appId（io.github.tekalu1.gliss）を基にする。ARA_FACTORY_ID は能力（解析の種類・再生の変形）が変わったら版を上げる。
# ARA_DOCUMENT_ARCHIVE_ID は保存の形が下位互換でなくなったら上げる（互換の古い ID は ARA_COMPATIBLE_ARCHIVE_IDS に残す）。
# JUCE はこの 2 つを C の文字列リテラルとしてそのまま埋め込むので、引用符まで値に含める。
juce_add_plugin(GlissARA
    PRODUCT_NAME "Gliss"
    COMPANY_NAME "Gliss"
    COMPANY_WEBSITE "https://github.com/tekalu1/gliss"
    BUNDLE_ID "io.github.tekalu1.gliss"
    PLUGIN_MANUFACTURER_CODE Glis
    PLUGIN_CODE Gara
    FORMATS VST3
    VST3_CATEGORIES Fx "Pitch Shift"
    IS_SYNTH FALSE
    NEEDS_MIDI_INPUT FALSE
    NEEDS_MIDI_OUTPUT FALSE
    IS_ARA_EFFECT TRUE
    ARA_FACTORY_ID "\"io.github.tekalu1.gliss.arafactory.1\""
    ARA_DOCUMENT_ARCHIVE_ID "\"io.github.tekalu1.gliss.aradocumentarchive.1\""
    NEEDS_WEBVIEW2 TRUE
    COPY_PLUGIN_AFTER_BUILD FALSE)

target_sources(GlissARA PRIVATE
    src/Diagnostics.cpp
    src/GlissDocumentController.cpp
    src/GlissEditor.cpp
    src/GlissPlaybackRenderer.cpp
    src/GlissProcessor.cpp
    src/ProcessUtils.cpp)

target_compile_options(GlissARA PUBLIC $<$<CXX_COMPILER_ID:MSVC>:/utf-8>)

target_compile_definitions(GlissARA PUBLIC
    JUCE_WEB_BROWSER=1
    JUCE_USE_WIN_WEBVIEW2_WITH_STATIC_LINKING=1
    JUCE_VST3_CAN_REPLACE_VST2=0
    JUCE_STRICT_REFCOUNTEDPOINTER=1)

target_link_libraries(GlissARA
    PRIVATE
        GlissWebAssets
        juce::juce_audio_basics
        juce::juce_audio_formats
        juce::juce_audio_plugin_client
        juce::juce_audio_processors
        juce::juce_audio_utils
        juce::juce_core
        juce::juce_events
        juce::juce_gui_basics
        juce::juce_gui_extra
    PUBLIC
        juce::juce_recommended_config_flags
        juce::juce_recommended_lto_flags
        juce::juce_recommended_warning_flags)
