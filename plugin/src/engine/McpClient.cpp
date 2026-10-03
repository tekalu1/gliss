#include "McpClient.h"
#include <chrono>

namespace gliss
{
namespace
{
juce::var object (std::initializer_list<std::pair<juce::Identifier, juce::var>> properties)
{
    auto* value = new juce::DynamicObject();
    for (const auto& [key, property] : properties)
        value->setProperty (key, property);
    return juce::var (value);
}
} // namespace

McpClient::McpClient (EngineConfig c) : config (std::move (c)) {}
McpClient::~McpClient() { stop(); }

juce::var McpClient::failure (const juce::String& reason)
{
    return object ({ { "ok", false }, { "error", reason } });
}

void McpClient::failPending (const juce::String& reason)
{
    std::map<int, std::shared_ptr<Pending>> waiting;
    {
        std::lock_guard guard (pendingMutex);
        waiting.swap (pending);
    }
    for (auto& [id, item] : waiting)
    {
        {
            std::lock_guard guard (item->mutex);
            item->result = failure (reason);
            item->complete = true;
        }
        item->ready.notify_all();
    }
}

void McpClient::readResponses()
{
    juce::String line;
    while (process.readLine (line))
    {
        const auto message = juce::JSON::parse (line);
        const auto idValue = message.getProperty ("id", {});
        if (idValue.isVoid() || ! (idValue.isInt() || idValue.isInt64()))
            continue;
        const auto id = (int) idValue;
        std::shared_ptr<Pending> item;
        {
            std::lock_guard guard (pendingMutex);
            auto found = pending.find (id);
            if (found != pending.end())
            {
                item = found->second;
                pending.erase (found);
            }
        }
        if (item != nullptr)
        {
            {
                std::lock_guard guard (item->mutex);
                item->result = message;
                item->complete = true;
            }
            item->ready.notify_all();
        }
    }
    readerActive = false;
    failPending ("Engine process exited");
    if (! stopping)
    {
        std::function<void()> callback;
        { std::lock_guard guard (callbackMutex); callback = exitCallback; }
        if (callback)
            try { callback(); }
            catch (...) { /* A host callback must not terminate the DAW. */ }
    }
}

juce::var McpClient::request (const juce::String& method, const juce::var& params, int timeoutMs)
{
    const int id = nextId++;
    auto item = std::make_shared<Pending>();
    {
        std::lock_guard guard (pendingMutex);
        pending[id] = item;
    }
    const auto message = object ({ { "jsonrpc", "2.0" }, { "id", id }, { "method", method }, { "params", params } });
    if (! process.writeLine (juce::JSON::toString (message, true)))
    {
        std::lock_guard guard (pendingMutex);
        pending.erase (id);
        return failure ("Could not write to engine");
    }
    std::unique_lock waitLock (item->mutex);
    if (! item->ready.wait_for (waitLock, std::chrono::milliseconds (juce::jmax (1, timeoutMs)), [&] { return item->complete; }))
    {
        waitLock.unlock();
        std::lock_guard guard (pendingMutex);
        pending.erase (id);
        return failure ("Engine response timed out");
    }
    return item->result;
}

bool McpClient::ensureStarted (juce::String& error)
{
    std::lock_guard guard (lifecycleMutex);
    if (readerActive && process.isRunning())
        return true;
    stopping = true;
    process.stop();
    if (reader.joinable()) reader.join();
    process.finishReader();
    readerActive = false;
    stopping = false;
    if (! process.start (config, error))
        return false;
    readerActive = true;
    reader = std::thread ([this] { readResponses(); });
    const auto init = request ("initialize", object ({
        { "protocolVersion", "2025-03-26" },
        { "capabilities", object ({}) },
        { "clientInfo", object ({ { "name", "gliss-ara" }, { "version", "0.1.0" } }) }
    }), 10000);
    if (init.getProperty ("result", {}).isVoid())
    {
        error = init.getProperty ("error", "Engine initialization failed").toString();
        stopping = true;
        process.stop();
        if (reader.joinable()) reader.join();
        process.finishReader();
        readerActive = false;
        stopping = false;
        return false;
    }
    process.writeLine (juce::JSON::toString (object ({ { "jsonrpc", "2.0" }, { "method", "notifications/initialized" } }), true));
    return true;
}

juce::var McpClient::payload (const juce::var& toolResult)
{
    auto structured = toolResult.getProperty ("structuredContent", {});
    auto direct = structured.getProperty ("result", {});
    if (! direct.isVoid()) return direct;
    if (auto* items = toolResult.getProperty ("content", {}).getArray())
        for (const auto& item : *items)
        {
            auto text = item.getProperty ("text", {}).toString();
            if (text.isNotEmpty())
            {
                auto parsed = juce::JSON::parse (text);
                if (! parsed.isVoid()) return parsed;
            }
        }
    return toolResult;
}

juce::var McpClient::call (const juce::String& tool, const juce::var& arguments, int timeoutMs)
{
    juce::String error;
    if (! ensureStarted (error)) return failure (error);
    const auto params = object ({ { "name", tool }, { "arguments", arguments } });
    const auto response = request ("tools/call", params, timeoutMs);
    if (! response.getProperty ("result", {}).isVoid())
        return payload (response.getProperty ("result", {}));
    const auto rpcError = response.getProperty ("error", {});
    if (rpcError.isObject())
        return failure (rpcError.getProperty ("message", "MCP request failed").toString());
    return response.getProperty ("ok", {}).isVoid() ? failure ("MCP response was empty") : response;
}

void McpClient::stop()
{
    std::lock_guard guard (lifecycleMutex);
    stopping = true;
    process.stop();
    if (reader.joinable()) reader.join();
    process.finishReader();
    readerActive = false;
    failPending ("Engine stopped");
    stopping = false;
}

bool McpClient::isRunning() const
{
    std::lock_guard guard (lifecycleMutex);
    return readerActive && process.isRunning();
}

unsigned long McpClient::processId() const
{
    std::lock_guard guard (lifecycleMutex);
    return process.processId();
}
void McpClient::setExitCallback (std::function<void()> callback)
{
    std::lock_guard guard (callbackMutex);
    exitCallback = std::move (callback);
}

} // namespace gliss
