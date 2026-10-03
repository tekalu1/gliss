# ARA SDK の検証用のホスト（ARATestHost）と、その部品で作った Gliss の通し試験 GlissARATest を 1 つのツリーでビルドする。
# plugin/scripts/test-plugin.ps1 が構成する（プラグインの CMake とは別のツリー。ARA SDK の VST3 SDK を使う）。
#
#   cmake -S plugin/tests/aratest -B plugin/build/ara-hosts -G "Visual Studio 17 2022" -A x64
#         -DGLISS_ARA_SDK_DIR=<ara_sdk-src> -DARA_VST3_SDK_DIR=<vst3sdk> -DARA_SETUP_DEBUGGING=OFF -DSMTG_CREATE_PLUGIN_LINK=OFF
#   cmake --build plugin/build/ara-hosts --config Release --target ARATestHost GlissARATest
cmake_minimum_required(VERSION 3.19)

project(GlissARAHosts LANGUAGES C CXX)

set(GLISS_ARA_SDK_DIR "" CACHE PATH "ARA SDK（ARA_Examples を含む）の置き場")
if(NOT EXISTS "${GLISS_ARA_SDK_DIR}/ARA_Examples/CMakeLists.txt")
    message(FATAL_ERROR "GLISS_ARA_SDK_DIR に ARA SDK を指定する（見つからない: ${GLISS_ARA_SDK_DIR}）")
endif()

add_subdirectory("${GLISS_ARA_SDK_DIR}/ARA_Examples" ara-examples)

# ARATestHost と同じソースから main.cpp だけを差し替える。
get_target_property(_testhost_sources ARATestHost SOURCES)
set(_sources)
foreach(src IN LISTS _testhost_sources)
    if(NOT src MATCHES "/main\\.cpp$" AND NOT src MATCHES "\\.rc$")   # 版の情報の .rc は ARATestHost だけの設定が要る
        list(APPEND _sources "${src}")
    endif()
endforeach()

# ARA の CMake はソースを「ターゲットのフォルダの下」として並べ直す（source_group TREE）。このターゲットのソースは
# SDK のフォルダにもあるので、並べ直しは飛ばす（関数は後の定義が使われる。SDK のターゲットは add_subdirectory の中で済んでいる）。
function(ara_group_target_files target)
endfunction()

add_executable(GlissARATest ${_sources} GlissARATest.cpp)
target_include_directories(GlissARATest PRIVATE "${GLISS_ARA_SDK_DIR}/ARA_Examples/TestHost")
target_compile_definitions(GlissARATest PRIVATE ARA_ENABLE_VST3=1 ARA_ENABLE_CLAP=0 _USE_MATH_DEFINES)
target_link_libraries(GlissARATest PRIVATE ARAExamplesCommon ARA_Host_Library)
target_compile_options(GlissARATest PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/utf-8>)
configure_ARA_Examples_target(GlissARATest)
set_target_properties(GlissARATest PROPERTIES
    CXX_STANDARD 17
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
