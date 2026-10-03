#include "AllocationCounter.h"

#include <cstdlib>
#include <new>

namespace
{

thread_local int countingDepth = 0;
thread_local long long allocations = 0;
thread_local long long deallocations = 0;

void countAllocation() noexcept
{
    if (countingDepth > 0)
        ++allocations;
}

void countDeallocation (void* p) noexcept
{
    if (p != nullptr && countingDepth > 0)
        ++deallocations;
}

void* allocate (std::size_t size) noexcept
{
    countAllocation();
    return std::malloc (size != 0 ? size : 1);
}

void deallocate (void* p) noexcept
{
    countDeallocation (p);
    std::free (p);
}

void* allocateAligned (std::size_t size, std::align_val_t alignment) noexcept
{
    countAllocation();
    const auto align = static_cast<std::size_t> (alignment);
#ifdef _WIN32
    return _aligned_malloc (size != 0 ? size : 1, align);
#else
    return std::aligned_alloc (align, ((size != 0 ? size : 1) + align - 1) / align * align);
#endif
}

void deallocateAligned (void* p) noexcept
{
    countDeallocation (p);
#ifdef _WIN32
    _aligned_free (p);
#else
    std::free (p);
#endif
}

void* allocateOrThrow (std::size_t size)
{
    if (auto* p = allocate (size))
        return p;
    throw std::bad_alloc();
}

void* allocateAlignedOrThrow (std::size_t size, std::align_val_t alignment)
{
    if (auto* p = allocateAligned (size, alignment))
        return p;
    throw std::bad_alloc();
}

} // namespace

void* operator new (std::size_t size)                                       { return allocateOrThrow (size); }
void* operator new[] (std::size_t size)                                     { return allocateOrThrow (size); }
void* operator new (std::size_t size, const std::nothrow_t&) noexcept       { return allocate (size); }
void* operator new[] (std::size_t size, const std::nothrow_t&) noexcept     { return allocate (size); }
void operator delete (void* p) noexcept                                     { deallocate (p); }
void operator delete[] (void* p) noexcept                                   { deallocate (p); }
void operator delete (void* p, std::size_t) noexcept                        { deallocate (p); }
void operator delete[] (void* p, std::size_t) noexcept                      { deallocate (p); }
void operator delete (void* p, const std::nothrow_t&) noexcept              { deallocate (p); }
void operator delete[] (void* p, const std::nothrow_t&) noexcept            { deallocate (p); }

void* operator new (std::size_t size, std::align_val_t a)                                   { return allocateAlignedOrThrow (size, a); }
void* operator new[] (std::size_t size, std::align_val_t a)                                 { return allocateAlignedOrThrow (size, a); }
void* operator new (std::size_t size, std::align_val_t a, const std::nothrow_t&) noexcept   { return allocateAligned (size, a); }
void* operator new[] (std::size_t size, std::align_val_t a, const std::nothrow_t&) noexcept { return allocateAligned (size, a); }
void operator delete (void* p, std::align_val_t) noexcept                                   { deallocateAligned (p); }
void operator delete[] (void* p, std::align_val_t) noexcept                                 { deallocateAligned (p); }
void operator delete (void* p, std::size_t, std::align_val_t) noexcept                      { deallocateAligned (p); }
void operator delete[] (void* p, std::size_t, std::align_val_t) noexcept                    { deallocateAligned (p); }
void operator delete (void* p, std::align_val_t, const std::nothrow_t&) noexcept            { deallocateAligned (p); }
void operator delete[] (void* p, std::align_val_t, const std::nothrow_t&) noexcept          { deallocateAligned (p); }

namespace gliss::test
{

ScopedAllocationCounter::ScopedAllocationCounter() noexcept
    : allocationsAtStart (allocations),
      deallocationsAtStart (deallocations)
{
    ++countingDepth;
}

ScopedAllocationCounter::~ScopedAllocationCounter() noexcept
{
    --countingDepth;
}

long long ScopedAllocationCounter::getAllocations() const noexcept
{
    return allocations - allocationsAtStart;
}

long long ScopedAllocationCounter::getDeallocations() const noexcept
{
    return deallocations - deallocationsAtStart;
}

} // namespace gliss::test
