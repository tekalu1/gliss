#pragma once

#include "EngineConfig.h"
#include <mutex>
#include <string>

#ifndef NOMINMAX
 #define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
 #define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace gliss
{

/** Owns one Windows process tree and its UTF-8 stdio pipes. */
class EngineProcess
{
public:
    EngineProcess() = default;
    ~EngineProcess();
    EngineProcess (const EngineProcess&) = delete;
    EngineProcess& operator= (const EngineProcess&) = delete;

    bool start (const EngineConfig&, juce::String& error);
    bool writeLine (const juce::String& line);
    bool readLine (juce::String& line);
    void stop();
    /** Close the read pipe after its reader thread has joined. */
    void finishReader();
    bool isRunning() const;
    unsigned long processId() const;

private:
    HANDLE job = nullptr, process = nullptr, input = nullptr, output = nullptr;
    mutable std::mutex writeMutex, stateMutex;
    std::string unread;
};

} // namespace gliss
