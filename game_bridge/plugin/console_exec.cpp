// Console command execution.
//
// The riskiest file in the plugin: the only place that reaches into engine
// internals to make something happen. Isolated so a mistake here cannot spread.
//
// STRATEGY
// --------
// We call the console's own executor (ids::kConsoleExecute) rather than
// building a ScriptBuffer and calling CompileAndRun ourselves. That function
// takes the command text, then constructs the compiler, the buffer and the
// script internally -- both call sites of CompileAndRun live inside it.
//
// This matters because a mismatched ScriptBuffer layout does not fail loudly.
// It corrupts memory or reads garbage, and the resulting symptom looks like a
// conversion bug -- the single most expensive failure mode for this project.
// Handing the engine a string and letting it build its own structures removes
// that entire class of risk, and keeps working across game updates.
//
// The script object we pass is created by the engine's own script factory, so
// we never guess at its size or field layout either.

#include <windows.h>

#include <cstdio>
#include <string>

#include "addresses.h"
#include "game.h"
#include "ids.h"
#include "log.h"

namespace bridge {

namespace {

// (Script* self, TESObjectREFR* target, void* unk, int compilerType)
using ConsoleExecute_t = bool (*)(void*, void*, void*, std::uint32_t);

// Compiler-name table index 1 == "SysWindowCompileAndRun".
constexpr std::uint32_t kCompilerTypeConsole = 1;

}  // namespace

// Implemented in script_object.cpp: allocates a Script via the engine's factory
// and sets its command text, without the plugin knowing the layout.
void* AllocScriptObject(const std::string& text);
void  FreeScriptObject(void* script);

namespace {

// Runs one command line with no selected reference.
bool ExecOne(const std::string& cmd, std::string* err) {
    void* script = AllocScriptObject(cmd);
    if (!script) {
        if (err) *err = "could not allocate script object";
        return false;
    }

    bool ok = false;
    // A malformed command can raise a SEH exception inside the engine. Catching
    // it keeps one bad command from taking down a long-lived test session.
    __try {
        auto fn = reinterpret_cast<ConsoleExecute_t>(g_addr.consoleExecute);
        ok = fn(script, nullptr, nullptr, kCompilerTypeConsole);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        if (err) *err = "engine raised an exception executing the command";
        ok = false;
    }

    FreeScriptObject(script);
    return ok;
}

}  // namespace

bool ConsoleExecute(const std::string& cmd, std::uint32_t refFormId, std::string* err) {
    if (!g_addr.consoleExecute) {
        if (err) *err = "console executor unavailable on this runtime";
        return false;
    }

    // Reference selection goes through the console's own `prid`, rather than
    // resolving a TESObjectREFR* ourselves and passing it as the target
    // argument. Same end state, but it costs no struct-layout assumptions and
    // no form-table walking -- and it is exactly what a human typing into the
    // console would do.
    if (refFormId) {
        char sel[32];
        std::snprintf(sel, sizeof(sel), "prid %08X", refFormId);
        if (!ExecOne(sel, err)) {
            if (err && err->empty()) *err = "could not select reference";
            return false;
        }
    }

    return ExecOne(cmd, err);
}

}  // namespace bridge
