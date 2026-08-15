// Generic, Python-driven hooking: install a detour at any address at runtime.
//
// Exists so that trying a new hook is an experiment rather than a rebuild plus
// a game restart. See generic_hook.cpp for the rationale and the safety rules.

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace bridge {

// The first four integer/pointer arguments of one intercepted call.
struct HookCall {
    std::uintptr_t a1 = 0, a2 = 0, a3 = 0, a4 = 0;
};

struct HookInfo {
    int            index = -1;
    std::uintptr_t target = 0;
    std::string    label;
    long           hits = 0;
    std::size_t    stolen = 0;
    std::vector<HookCall> calls;
};

// How many bytes can safely be stolen from `addr`, or 0 with a reason in
// `why`. Walks real instruction boundaries and refuses anything
// position-dependent (rip-relative operands, relative jumps, `mov r64,rsp`).
// Callers must never guess a length: guessing cut an instruction in half and
// crashed the game once already.
std::size_t AnalyzePrologue(std::uintptr_t addr, std::string* why);

// Installs a counting/argument-recording detour. Returns the slot index, or -1
// with a reason. Observing only -- the original is always called and its
// return value passed through unchanged.
int InstallGenericHook(std::uintptr_t target, const std::string& label,
                       std::size_t keep, std::string* err);

// Restores the original bytes. Without removal a wrong hook is permanent for
// the session, which makes hooking unusable for experiments.
bool RemoveGenericHook(int idx, std::string* err);

bool GetGenericHook(int idx, HookInfo* out);
int  GenericHookCount();

}  // namespace bridge
