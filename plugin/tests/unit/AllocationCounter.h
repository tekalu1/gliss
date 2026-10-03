#pragma once

// オーディオスレッドで確保・解放が起きないことを確かめるための、operator new / delete の数え上げ。
// GlissPluginTests は global の operator new / delete を AllocationCounter.cpp のものに置き換えている。
// 数えるのは ScopedAllocationCounter の生きている間、そのスレッドで起きたものだけ。
// std::malloc を直に呼ぶもの（juce::HeapBlock・AudioBuffer の中身）は数えられない。

namespace gliss::test
{

class ScopedAllocationCounter
{
public:
    ScopedAllocationCounter() noexcept;
    ~ScopedAllocationCounter() noexcept;

    long long getAllocations() const noexcept;
    long long getDeallocations() const noexcept;

    ScopedAllocationCounter (const ScopedAllocationCounter&) = delete;
    ScopedAllocationCounter& operator= (const ScopedAllocationCounter&) = delete;

private:
    long long allocationsAtStart = 0;
    long long deallocationsAtStart = 0;
};

} // namespace gliss::test
