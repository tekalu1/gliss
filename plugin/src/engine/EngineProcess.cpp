#include "EngineProcess.h"
#include <map>
#include <vector>

namespace gliss
{
namespace
{
void close (HANDLE& handle)
{
    if (handle != nullptr && handle != INVALID_HANDLE_VALUE)
        ::CloseHandle (handle);
    handle = nullptr;
}

std::wstring quote (const juce::String& value)
{
    std::wstring out = L"\"";
    unsigned slashCount = 0;
    for (auto c : std::wstring (value.toWideCharPointer()))
    {
        if (c == L'\\') { ++slashCount; continue; }
        if (c == L'"')
            out.append (slashCount * 2 + 1, L'\\');
        else
            out.append (slashCount, L'\\');
        slashCount = 0;
        out += c;
    }
    out.append (slashCount * 2, L'\\');
    return out + L'"';
}

struct LessIgnoreCase
{
    bool operator() (const std::wstring& a, const std::wstring& b) const { return ::_wcsicmp (a.c_str(), b.c_str()) < 0; }
};

std::vector<wchar_t> environmentBlock (const juce::StringPairArray& overlay)
{
    std::map<std::wstring, std::wstring, LessIgnoreCase> entries;
    if (auto* block = ::GetEnvironmentStringsW())
    {
        for (auto* p = block; *p != 0; p += std::wcslen (p) + 1)
        {
            const std::wstring entry (p);
            const auto eq = entry.find (L'=', entry[0] == L'=' ? 1 : 0);
            if (eq != std::wstring::npos)
                entries[entry.substr (0, eq)] = entry.substr (eq + 1);
        }
        ::FreeEnvironmentStringsW (block);
    }
    for (const auto& key : overlay.getAllKeys())
        entries[std::wstring (key.toWideCharPointer())] = std::wstring (overlay[key].toWideCharPointer());
    std::vector<wchar_t> block;
    for (const auto& [key, value] : entries)
    {
        const auto entry = key + L"=" + value;
        block.insert (block.end(), entry.begin(), entry.end());
        block.push_back (0);
    }
    block.push_back (0);
    return block;
}
} // namespace

EngineProcess::~EngineProcess() { stop(); finishReader(); }

bool EngineProcess::start (const EngineConfig& config, juce::String& error)
{
    stop();
    finishReader();
    if (! config.isValid())
    {
        error = "Engine executable or working directory was not found";
        return false;
    }

    SECURITY_ATTRIBUTES attrs { sizeof (SECURITY_ATTRIBUTES), nullptr, TRUE };
    HANDLE childInput = nullptr, childOutput = nullptr, childError = INVALID_HANDLE_VALUE;
    auto fail = [&] (const juce::String& reason)
    {
        error = reason + " (Win32 " + juce::String ((int) ::GetLastError()) + ")";
        close (childInput); close (childOutput); close (childError);
        stop();
        return false;
    };
    if (! ::CreatePipe (&childInput, &input, &attrs, 0)
        || ! ::SetHandleInformation (input, HANDLE_FLAG_INHERIT, 0)
        || ! ::CreatePipe (&output, &childOutput, &attrs, 0)
        || ! ::SetHandleInformation (output, HANDLE_FLAG_INHERIT, 0))
        return fail ("Could not create engine pipes");

    childError = ::CreateFileW (L"NUL", GENERIC_WRITE, FILE_SHARE_WRITE | FILE_SHARE_READ,
                                &attrs, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (childError == INVALID_HANDLE_VALUE)
        return fail ("Could not open engine stderr sink");

    job = ::CreateJobObjectW (nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits {};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (job == nullptr || ! ::SetInformationJobObject (job, JobObjectExtendedLimitInformation, &limits, sizeof (limits)))
        return fail ("Could not create engine job");

    std::wstring command = quote (config.executable.getFullPathName());
    for (const auto& arg : config.arguments)
        command += L" " + quote (arg);
    auto childEnvironment = config.environment;
    childEnvironment.set ("GLISS_CLIENT", "ara");
    // 外部の AI の中継の記録（%APPDATA%\Gliss\ara-sessions）に DAW のプロセスを書くため（.venv の python は間に起動役が入る）
    childEnvironment.set ("GLISS_ARA_HOST_PID", juce::String ((juce::int64) ::GetCurrentProcessId()));
    childEnvironment.set ("PYTHONIOENCODING", "utf-8");
    auto block = environmentBlock (childEnvironment);
    STARTUPINFOW startup { sizeof (STARTUPINFOW) };
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = childInput;
    startup.hStdOutput = childOutput;
    startup.hStdError = childError;
    PROCESS_INFORMATION info {};
    const auto executable = std::wstring (config.executable.getFullPathName().toWideCharPointer());
    const auto cwd = std::wstring (config.workingDirectory.getFullPathName().toWideCharPointer());
    const bool created = ::CreateProcessW (executable.c_str(), command.data(), nullptr, nullptr, TRUE,
                                           CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                           block.data(), cwd.c_str(), &startup, &info) != 0;
    close (childInput); close (childOutput); close (childError);
    if (! created)
        return fail ("Could not start engine");
    process = info.hProcess;
    if (! ::AssignProcessToJobObject (job, process))
    {
        ::TerminateProcess (process, 1);
        close (info.hThread);
        return fail ("Could not assign engine to job");
    }
    ::ResumeThread (info.hThread);
    close (info.hThread);
    return true;
}

bool EngineProcess::writeLine (const juce::String& line)
{
    std::lock_guard guard (writeMutex);
    if (input == nullptr)
        return false;
    auto bytes = line.toStdString() + "\n";
    size_t offset = 0;
    while (offset < bytes.size())
    {
        DWORD written = 0;
        if (! ::WriteFile (input, bytes.data() + offset, (DWORD) (bytes.size() - offset), &written, nullptr) || written == 0)
            return false;
        offset += written;
    }
    return true;
}

bool EngineProcess::readLine (juce::String& line)
{
    for (;;)
    {
        const auto end = unread.find ('\n');
        if (end != std::string::npos)
        {
            auto bytes = unread.substr (0, end);
            unread.erase (0, end + 1);
            if (! bytes.empty() && bytes.back() == '\r') bytes.pop_back();
            line = juce::String::fromUTF8 (bytes.data(), (int) bytes.size());
            return true;
        }
        DWORD available = 0;
        if (! ::PeekNamedPipe (output, nullptr, 0, nullptr, &available, nullptr))
            return false;
        if (available == 0)
        {
            if (! isRunning()) return false;
            ::Sleep (10);
            continue;
        }
        char buffer[4096];
        DWORD count = 0;
        if (! ::ReadFile (output, buffer, juce::jmin ((DWORD) sizeof (buffer), available), &count, nullptr) || count == 0)
            return false;
        unread.append (buffer, count);
        if (unread.size() > 64 * 1024 * 1024)
            return false;
    }
}

void EngineProcess::stop()
{
    {
        std::lock_guard guard (writeMutex);
        close (input);
    }
    {
        std::lock_guard guard (stateMutex);
        if (process != nullptr)
            ::WaitForSingleObject (process, 2000);
        close (job); // KILL_ON_JOB_CLOSE also kills grandchildren.
        close (process);
    }
}

void EngineProcess::finishReader()
{
    close (output);
    unread.clear();
}

bool EngineProcess::isRunning() const
{
    std::lock_guard guard (stateMutex);
    return process != nullptr && ::WaitForSingleObject (process, 0) == WAIT_TIMEOUT;
}

unsigned long EngineProcess::processId() const
{
    std::lock_guard guard (stateMutex);
    return process != nullptr ? ::GetProcessId (process) : 0;
}

} // namespace gliss
