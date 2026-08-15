// Papyrus VM output capture.
//
// Hooks SkyrimScript::Logger::Log, the sink every Papyrus message passes
// through on its way to Papyrus.0.log -- Debug.Trace, Debug.Notification, VM
// errors ("cannot be bound", stack overflows, "has no property"), and anything
// an injected script prints.
//
// WHY THIS AND NOT JUST TAILING THE LOG FILE
// ------------------------------------------
// Tailing the file works and is kept (tools/papyrus_tail.py), but it cannot
// answer "what did MY injected script just print?" without racing the game's
// buffered writes and guessing which lines belong to you. Hooking the sink
// gives an exact, ordered, per-request slice: arm a capture, run the script,
// read back precisely the lines the VM emitted in between.
//
// The two are complementary and both are exposed:
//   file tail -> history, and anything emitted while nothing was armed
//   VM hook   -> exactly what happened during one injection
//
// FINDING THE TARGET (RTTI, the same route as console_capture.cpp):
//   TypeDescriptor ".?AVLogger@SkyrimScript@@"  (rva 0x1f12078 on 1.6.659)
//     -> CompleteObjectLocator -> vtable        (rva 0x17b5b70)
//     -> vtable[1] = Log                        (rva 0x945290, id 53551)
//
// Signature: Log(this, const char* message, ...) -- verified from the
// prologue, which saves rdx (arg2, the message) into rdi before touching
// anything else.

#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>

#include "addresses.h"
#include "detour.h"
#include "game.h"
#include "ids.h"
#include "log.h"

namespace bridge {

namespace {

using LogFn = void (*)(void* self, const char* msg, ...);

LogFn g_original = nullptr;
bool  g_installed = false;
volatile long g_hits = 0;

std::mutex              g_mutex;
bool                    g_armed = false;
std::deque<std::string> g_lines;      // captured while armed
std::deque<std::string> g_recent;     // always-on ring, for after-the-fact reads

// Bounded so a runaway script cannot grow these without limit. The VM can emit
// thousands of lines a second; an unbounded buffer would be a memory leak that
// only shows up during exactly the long debugging session this tool is for.
constexpr std::size_t kMaxArmed  = 4000;
constexpr std::size_t kMaxRecent = 400;

void Record(const char* text) {
    if (!text || !*text) return;
    std::lock_guard<std::mutex> lk(g_mutex);

    g_recent.emplace_back(text);
    if (g_recent.size() > kMaxRecent) g_recent.pop_front();

    if (g_armed && g_lines.size() < kMaxArmed) g_lines.emplace_back(text);
}

void __cdecl LogDetour(void* self, const char* msg, ...) {
    // Format first: callers pass printf-style arguments, and capturing the
    // unformatted template would record "%s" instead of the actual value.
    //
    // vsnprintf returns the length it WOULD have written, so an oversized
    // message is detected rather than silently truncated -- a truncated
    // capture that differs from what the game logged would quietly mislead
    // exactly when a long line (a stack dump) is the interesting one.
    char stackBuf[2048];
    std::string heapBuf;
    const char* text = stackBuf;

    va_list args;
    va_start(args, msg);
    va_list copy;
    va_copy(copy, args);
    const int n = std::vsnprintf(stackBuf, sizeof(stackBuf), msg ? msg : "", args);
    va_end(args);

    if (n >= static_cast<int>(sizeof(stackBuf))) {
        heapBuf.resize(static_cast<std::size_t>(n) + 1);
        std::vsnprintf(heapBuf.data(), heapBuf.size(), msg ? msg : "", copy);
        heapBuf.resize(static_cast<std::size_t>(n));
        text = heapBuf.c_str();
    } else if (n < 0) {
        text = "";
    } else {
        stackBuf[n] = '\0';
    }
    va_end(copy);

    InterlockedIncrement(&g_hits);
    if (*text) Record(text);

    // Always call through: the game's own log file must stay complete, both so
    // papyrus_tail.py keeps working and so nothing is hidden from the user.
    //
    // Pass the ALREADY-FORMATTED text through "%s" so a '%' inside a script's
    // own output cannot be reinterpreted as a format specifier (which would
    // read garbage off the stack).
    if (g_original) g_original(self, "%s", text);
}

// SkyrimScript::Logger::Log prologue, read from the disassembly:
//   40 57              push rdi
//   48 83 EC 40        sub  rsp,0x40
//   48 C7 44 24 30 FE FF FF FF   mov qword [rsp+30],-2
// = 15 bytes, all position-independent (no rip-relative operand), ending on an
// instruction boundary. Verified on GOG 1.6.659 at 0x945290.
constexpr std::uint8_t kExpectedPrologue[] = {
    0x40, 0x57,
    0x48, 0x83, 0xEC, 0x40,
    0x48, 0xC7, 0x44, 0x24, 0x30, 0xFE, 0xFF, 0xFF, 0xFF,
};

}  // namespace

bool InstallPapyrusCapture(std::uintptr_t logAddr) {
    if (g_installed) return true;
    if (!logAddr) {
        Log("papyrus: no Logger::Log address; VM output capture disabled");
        return false;
    }
    void* orig = nullptr;
    if (!InstallDetour(logAddr, reinterpret_cast<void*>(&LogDetour),
                       kExpectedPrologue, sizeof(kExpectedPrologue),
                       &orig, "Papyrus Logger::Log")) {
        return false;
    }
    g_original = reinterpret_cast<LogFn>(orig);
    g_installed = true;
    return true;
}

bool PapyrusCaptureInstalled() { return g_installed; }
long PapyrusCaptureHits() { return g_hits; }

void PapyrusCaptureArm() {
    std::lock_guard<std::mutex> lk(g_mutex);
    g_lines.clear();
    g_armed = true;
}

std::vector<std::string> PapyrusCaptureTake() {
    std::lock_guard<std::mutex> lk(g_mutex);
    std::vector<std::string> out(g_lines.begin(), g_lines.end());
    g_lines.clear();
    g_armed = false;
    return out;
}

std::vector<std::string> PapyrusCaptureRecent(std::size_t limit) {
    std::lock_guard<std::mutex> lk(g_mutex);
    std::vector<std::string> out;
    const std::size_t n = (limit && limit < g_recent.size()) ? limit : g_recent.size();
    out.reserve(n);
    for (auto it = g_recent.end() - static_cast<std::ptrdiff_t>(n); it != g_recent.end(); ++it)
        out.push_back(*it);
    return out;
}

}  // namespace bridge
