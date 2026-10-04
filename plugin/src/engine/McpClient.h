#pragma once

#include "EngineProcess.h"
#include <atomic>
#include <condition_variable>
#include <functional>
#include <map>
#include <memory>
#include <thread>

namespace gliss
{

/** Thread-safe, synchronous MCP calls. Calls may be made concurrently from non-audio threads. */
class McpClient
{
public:
    explicit McpClient (EngineConfig config = EngineConfig::discover());
    ~McpClient();
    McpClient (const McpClient&) = delete;
    McpClient& operator= (const McpClient&) = delete;

    juce::var call (const juce::String& tool, const juce::var& arguments = juce::var (new juce::DynamicObject()),
                    int timeoutMs = 30000);
    void stop();
    bool isRunning() const;
    unsigned long processId() const;
    /** Called on the reader thread after unexpected exit; the callback must not call this client. */
    void setExitCallback (std::function<void()> callback);

    /** Unwraps a tools/call result. An engine {ok:false} remains an ordinary value. */
    static juce::var payload (const juce::var& toolResult);

private:
    struct Pending
    {
        std::mutex mutex;
        std::condition_variable ready;
        bool complete = false;
        juce::var result;
    };

    bool ensureStarted (juce::String& error);
    juce::var request (const juce::String& method, const juce::var& params, int timeoutMs);
    void readResponses();
    void failPending (const juce::String& reason);
    static juce::var failure (const juce::String& reason);

    EngineConfig config;
    EngineProcess process;
    mutable std::mutex lifecycleMutex, pendingMutex, callbackMutex;
    std::map<int, std::shared_ptr<Pending>> pending;
    std::thread reader;
    std::atomic<bool> readerActive { false }, stopping { false };
    std::atomic<int> nextId { 1 };
    std::function<void()> exitCallback;
};

} // namespace gliss
