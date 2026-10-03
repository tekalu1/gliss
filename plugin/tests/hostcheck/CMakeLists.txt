# JUCE のホストとして Gliss.vst3 を読み込む確認用のアプリ（Main.cpp の冒頭に確かめることを書いている）。
juce_add_gui_app(GlissHostCheck
    PRODUCT_NAME "GlissHostCheck"
    COMPANY_NAME "Gliss"
    VERSION 1.0.0)

target_sources(GlissHostCheck PRIVATE Main.cpp)

target_compile_options(GlissHostCheck PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/utf-8>)

target_compile_definitions(GlissHostCheck PRIVATE
    JUCE_PLUGINHOST_VST3=1
    JUCE_PLUGINHOST_ARA=1
    JUCE_WEB_BROWSER=0
    JUCE_USE_CURL=0)

target_link_libraries(GlissHostCheck PRIVATE
    juce::juce_audio_processors
    juce::juce_audio_utils
    juce::juce_gui_extra
    juce::juce_recommended_config_flags
    juce::juce_recommended_lto_flags
    juce::juce_recommended_warning_flags)
