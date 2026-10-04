#include "ProcessUtils.h"

#include <juce_core/juce_core.h>

#if JUCE_WINDOWS
 #ifndef NOMINMAX
  #define NOMINMAX
 #endif
 #ifndef WIN32_LEAN_AND_MEAN
  #define WIN32_LEAN_AND_MEAN
 #endif
 #include <windows.h>
#endif

namespace gliss
{

int currentProcessId()
{
    return (int) ::GetCurrentProcessId();
}

bool isProcessRunning (int processId)
{
    const auto handle = ::OpenProcess (PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD) processId);

    // 開けない理由が「権限が無い」なら、動いているとみなす（消してはいけない側に倒す）。
    if (handle == nullptr)
        return ::GetLastError() != ERROR_INVALID_PARAMETER;

    DWORD exitCode = 0;
    const auto queried = ::GetExitCodeProcess (handle, &exitCode) != 0;
    ::CloseHandle (handle);
    return ! queried || exitCode == STILL_ACTIVE;
}

} // namespace gliss
