// Script object construction for console execution.
//
// The console builds a Script the same way every time:
//   1. allocate 0x80 bytes from the game heap
//   2. run Script::ctor, which stores the vtable and zeroes the members
//   3. call Script::SetText(cmd), which allocates and stores the text at +0x38
//   4. hand it to the console executor
//
// We reproduce that, with one deliberate substitution: instead of calling
// Script::ctor we store the vtable and zero the block ourselves. Script::ctor's
// stable ID (21874) is present in the 1.6.659 Address Library but ABSENT from
// 1.6.1170, so resolving it on the user's actual runtime yields 0 -- and
// calling through it would jump to address 0 and take the game down.
//
// Everything else here goes through engine functions, so no field layout is
// assumed beyond the text pointer at +0x38 (verified by disassembling
// Script::SetText, which writes exactly there).

#include <windows.h>

#include <cstring>
#include <string>

#include "addresses.h"
#include "ids.h"
#include "log.h"

namespace bridge {

namespace {

using MemAlloc_t     = void* (*)(void* heap, std::size_t size, std::size_t align, bool aligned);
using ScriptSetText_t = void  (*)(void* script, const char* text);

}  // namespace

void* AllocScriptObject(const std::string& text) {
    if (!g_addr.memAlloc || !g_addr.scriptVtable || !g_addr.scriptSetText) return nullptr;

    auto alloc = reinterpret_cast<MemAlloc_t>(g_addr.memAlloc);

    // Matches the console's own allocation: 0x80 bytes, unaligned.
    void* mem = alloc(nullptr, ids::kScriptSize, 0, false);
    if (!mem) return nullptr;

    std::memset(mem, 0, ids::kScriptSize);
    *reinterpret_cast<std::uintptr_t*>(mem) = g_addr.scriptVtable;

    auto setText = reinterpret_cast<ScriptSetText_t>(g_addr.scriptSetText);
    __try {
        setText(mem, text.c_str());
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // Leak the 0x80 block rather than risk freeing a half-built object.
        Log("script: SetText raised an exception");
        return nullptr;
    }

    return mem;
}

void FreeScriptObject(void* script) {
    if (!script) return;
    // Release through the engine's own destructor path so the text buffer and
    // any compiled data are freed the way the engine expects.
    if (g_addr.scriptDtor) {
        using Dtor_t = void (*)(void*);
        __try {
            reinterpret_cast<Dtor_t>(g_addr.scriptDtor)(script);
            return;
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            Log("script: destructor raised an exception; leaking object");
            return;
        }
    }
    // No destructor resolved: leaking one 0x80 block per command is strictly
    // better than freeing an object the engine still holds pointers into.
}

}  // namespace bridge
