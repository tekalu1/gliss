# 単体テスト。プラグインの単位のソース（GLISS_UNIT_SOURCES）と、単位ごとのテスト（<単位>.cmake が GLISS_UNIT_TESTS に足す）を 1 つの console のアプリにする。
# 流し方: GlissPluginTests.exe（カテゴリ "Gliss"）／ GlissPluginTests.exe <カテゴリ>／ GlissPluginTests.exe all。失敗があれば終了コード 1。

juce_add_console_app(GlissPluginTests
    PRODUCT_NAME "GlissPluginTests")

set(GLISS_UNIT_TESTS)
foreach(unit engine cache ara editor)
    include("${CMAKE_CURRENT_SOURCE_DIR}/${unit}.cmake" OPTIONAL)
endforeach()

set(_gliss_unit_abs)
foreach(src IN LISTS GLISS_UNIT_SOURCES)
    list(APPEND _gliss_unit_abs "${PROJECT_SOURCE_DIR}/${src}")
endforeach()

target_sources(GlissPluginTests PRIVATE
    Main.cpp
    ${_gliss_unit_abs}
    ${GLISS_UNIT_TESTS})

target_include_directories(GlissPluginTests PRIVATE "${PROJECT_SOURCE_DIR}/src")

target_compile_options(GlissPluginTests PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/utf-8>)

target_compile_definitions(GlissPluginTests PRIVATE
    JUCE_UNIT_TESTS=1
    JUCE_WEB_BROWSER=0
    JUCE_USE_CURL=0)

target_link_libraries(GlissPluginTests
    PRIVATE
        juce::juce_audio_basics
        juce::juce_audio_formats
        juce::juce_core
        juce::juce_events
        signalsmith-stretch
    PUBLIC
        juce::juce_recommended_config_flags
        juce::juce_recommended_warning_flags)
