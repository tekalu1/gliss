# 単位 C3 DocumentController への組み込み
# ARA の型を使わないもの（単体テストにも入る）
list(APPEND GLISS_UNIT_SOURCES
    src/ara/ArchiveIO.cpp
    src/ara/DocumentSync.cpp
    src/ara/EngineCalls.cpp
    src/ara/FloatWavWriter.cpp
    src/ara/NoteContent.cpp
    src/ara/PlayheadState.cpp
    src/ara/PluginState.cpp
    src/ara/PreviewAudio.cpp
    src/ara/RegionMapping.cpp
    src/ara/SelectionPolicy.cpp
)

# ARA（juce_audio_processors の ARA の型）を使うもの: プラグインだけに入れる。
# このファイルは plugin/CMakeLists.txt で GlissARA を作った後に include される。
if(TARGET GlissARA)
    target_sources(GlissARA PRIVATE
        src/GlissEditorRenderer.cpp
    )
endif()
