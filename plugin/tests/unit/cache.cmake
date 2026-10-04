# 単位 C2 キャッシュの単体テスト（AllocationCounter は global の operator new / delete を置き換え、確保を数える）
list(APPEND GLISS_UNIT_TESTS
    "${CMAKE_CURRENT_SOURCE_DIR}/AllocationCounter.cpp"
    "${CMAKE_CURRENT_SOURCE_DIR}/CacheTests.cpp"
    "${CMAKE_CURRENT_SOURCE_DIR}/StretchTests.cpp"
)
