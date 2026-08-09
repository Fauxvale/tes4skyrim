#include "game.h"

#include <windows.h>

#include <cstdio>
#include <cstring>
#include <mutex>

#include "addresses.h"
#include "ids.h"
#include "game_types.h"
#include "log.h"

namespace bridge {

// ---------------------------------------------------------------- console ----
//
// Execution strategy.
//
// The bridge runs console commands through the game's OWN console pipeline
// rather than reimplementing argument parsing on top of the ObScript table.
// Two reasons: every command's parser comes for free (including the ones with
// irregular argument grammars), and behaviour matches what the user sees when
// they type the same line.
//
// Output capture: console commands report through the console print path, which
// writes to the console log. We install a capture buffer around the call so
// `getpos`, `getav` etc. return their text instead of only painting the screen.

namespace {

std::mutex        g_consoleMutex;
std::string       g_captureBuffer;
bool              g_capturing = false;

// Set by the console-print hook while a captured command is running.
void CaptureLine(const char* text) {
    if (!g_capturing || !text) return;
    g_captureBuffer += text;
    g_captureBuffer.push_back('\n');
}

}  // namespace

void ConsoleCaptureAppend(const char* text) { CaptureLine(text); }

ConsoleResult RunConsoleCommand(const std::string& command, std::uint32_t refFormId) {
    ConsoleResult out;

    if (!g_addr.compileAndRun) {
        out.error = "CompileAndRun unavailable on this runtime";
        return out;
    }
    if (command.empty()) {
        out.error = "empty command";
        return out;
    }

    std::lock_guard<std::mutex> lk(g_consoleMutex);

    g_captureBuffer.clear();
    g_capturing = true;

    // The actual invocation is filled in by console_exec.cpp, which owns the
    // ScriptBuffer/Script construction. Kept separate so the risky struct-layout
    // work is isolated from the rest of the plugin.
    extern bool ConsoleExecute(const std::string& cmd, std::uint32_t refFormId, std::string* err);

    std::string err;
    const bool ok = ConsoleExecute(command, refFormId, &err);

    g_capturing = false;
    out.ok = ok;
    out.output = g_captureBuffer;
    out.error = err;
    return out;
}

// ----------------------------------------------------------------- state ----

std::uint32_t g_runtimeVersion = 0;
bool          g_gameLoaded = false;   // updated from SKSE messages

bool IsGameLoaded() { return g_gameLoaded; }
std::uint32_t RuntimeVersion() { return g_runtimeVersion; }

bool IsInLoadingScreen() {
    // Conservative: without a verified UI singleton we cannot read the loading
    // flag directly. Callers use wait_ready, which polls a cheap console probe
    // instead of trusting a guessed pointer.
    return false;
}

// ----------------------------------------------------------------- forms ----

std::uint32_t ResolveFormRef(const std::string& text) {
    if (text.empty()) return 0;
    if (text.size() > 2 && text[0] == '0' && (text[1] == 'x' || text[1] == 'X'))
        return static_cast<std::uint32_t>(std::strtoul(text.c_str() + 2, nullptr, 16));

    bool allHex = true;
    for (char c : text) {
        if (!std::isxdigit(static_cast<unsigned char>(c))) { allHex = false; break; }
    }
    if (allHex && text.size() >= 6)
        return static_cast<std::uint32_t>(std::strtoul(text.c_str(), nullptr, 16));

    return 0;  // editor-ID resolution goes through the console
}

}  // namespace bridge
