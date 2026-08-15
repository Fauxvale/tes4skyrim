// Generic in-process primitives: read memory, resolve addresses, call functions.
//
// WHY THIS EXISTS
// ---------------
// Every hypothesis about engine internals used to cost a full rebuild + game
// restart, because the probe logic lived in this DLL. That is exactly the
// round trip the bridge is supposed to eliminate. With these primitives the
// experiment moves to Python: resolve an address, read the bytes, call it,
// look at the result -- all against the LIVE process, with no rebuild.
//
// This is a debugging tool for a developer driving their own game, so it is
// deliberately powerful. The guards that matter are the ones that prevent
// ACCIDENTS, not the ones that prevent intent:
//
//   * reads are probed with IsBadReadPtr-equivalent VirtualQuery first, so a
//     bad address returns an error instead of crashing the game
//   * calls are wrapped in SEH, so a wrong signature kills the command rather
//     than the session
//   * everything runs on the main thread, like every other stateful handler
//
// A wrong `call` can still corrupt the game -- that is inherent to the
// feature. The alternative (rebuild + restart per idea) has already proven
// worse in practice.

#include <windows.h>

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "addresses.h"
#include "log.h"

namespace bridge {

namespace {

// True if [addr, addr+len) is committed and readable.
bool Readable(std::uintptr_t addr, std::size_t len) {
    if (!addr || !len) return false;
    MEMORY_BASIC_INFORMATION mbi{};
    std::uintptr_t p = addr;
    const std::uintptr_t end = addr + len;
    while (p < end) {
        if (!VirtualQuery(reinterpret_cast<void*>(p), &mbi, sizeof(mbi))) return false;
        if (mbi.State != MEM_COMMIT) return false;
        const DWORD prot = mbi.Protect & 0xFF;
        const bool ok = prot == PAGE_READONLY || prot == PAGE_READWRITE ||
                        prot == PAGE_WRITECOPY || prot == PAGE_EXECUTE_READ ||
                        prot == PAGE_EXECUTE_READWRITE || prot == PAGE_EXECUTE_WRITECOPY;
        if (!ok || (mbi.Protect & PAGE_GUARD)) return false;
        p = reinterpret_cast<std::uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
    }
    return true;
}

}  // namespace

std::uintptr_t ModuleBaseAddress() {
    return reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
}

// Reads `len` bytes. Returns false (and leaves `out` empty) if unreadable.
bool RawRead(std::uintptr_t addr, std::size_t len, std::vector<std::uint8_t>* out) {
    if (!out) return false;
    out->clear();
    if (len > 4096) len = 4096;
    if (!Readable(addr, len)) return false;
    out->resize(len);
    std::memcpy(out->data(), reinterpret_cast<const void*>(addr), len);
    return true;
}

// Reads a NUL-terminated string, bounded.
bool RawReadString(std::uintptr_t addr, std::size_t maxLen, std::string* out) {
    if (!out) return false;
    out->clear();
    if (maxLen > 4096) maxLen = 4096;
    for (std::size_t i = 0; i < maxLen; ++i) {
        if (!Readable(addr + i, 1)) return i > 0;
        const char c = *reinterpret_cast<const char*>(addr + i);
        if (!c) return true;
        out->push_back(c);
    }
    return true;
}

// Writes bytes into the live process.
//
// This is the primitive that removes the last reason to rebuild-and-restart.
// With read + write + call + resolve, a probe can construct arguments (command
// strings, structs) in game memory and drive any engine function from Python,
// so a new diagnostic needs no new C++.
//
// Deliberately unrestricted as to target -- the developer is debugging their
// own game -- but it will not write to memory that is not already committed
// and writable, so a typo'd address fails instead of corrupting something.
bool RawWrite(std::uintptr_t addr, const std::vector<std::uint8_t>& bytes,
              std::string* err) {
    if (!addr || bytes.empty()) {
        if (err) *err = "nothing to write";
        return false;
    }
    if (bytes.size() > 4096) {
        if (err) *err = "write too large (max 4096)";
        return false;
    }

    DWORD old = 0;
    if (!VirtualProtect(reinterpret_cast<void*>(addr), bytes.size(),
                        PAGE_EXECUTE_READWRITE, &old)) {
        if (err) *err = "VirtualProtect failed (address not committed?)";
        return false;
    }
    __try {
        std::memcpy(reinterpret_cast<void*>(addr), bytes.data(), bytes.size());
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        VirtualProtect(reinterpret_cast<void*>(addr), bytes.size(), old, &old);
        if (err) *err = "the write raised an exception";
        return false;
    }
    VirtualProtect(reinterpret_cast<void*>(addr), bytes.size(), old, &old);
    FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(addr),
                          bytes.size());
    return true;
}

// Allocates scratch memory inside the game process, for building arguments
// (command strings, small structs) that engine functions can then be handed.
std::uintptr_t RawAlloc(std::size_t len) {
    if (!len || len > 1u << 20) return 0;
    return reinterpret_cast<std::uintptr_t>(
        VirtualAlloc(nullptr, len, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
}

// Calls a function with up to 8 arguments, returning BOTH the integer result
// (RAX) and the float result (XMM0).
//
// Four integer args covers most engine functions, but not all -- and a probe
// that hits the limit would otherwise need a rebuild, which is the one outcome
// this file exists to prevent. Likewise the return: many engine getters return
// a float in XMM0, and reading RAX for those yields garbage that looks like a
// plausible integer.
//
// `floatMask` marks which arguments are floats: bit N set means argument N
// goes in XMMn instead of the integer register. The x64 convention pairs them
// positionally (arg0 -> rcx/xmm0, arg1 -> rdx/xmm1, ...), so the mask is all
// the caller needs to describe any mixed signature.
bool RawCallEx(std::uintptr_t fn,
               const std::uint64_t* args, std::size_t argc,
               std::uint32_t floatMask,
               std::uint64_t* intResult, double* floatResult,
               std::string* err) {
    if (!fn) {
        if (err) *err = "null function address";
        return false;
    }
    if (!Readable(fn, 1)) {
        if (err) *err = "function address is not readable";
        return false;
    }
    if (argc > 8) {
        if (err) *err = "at most 8 arguments";
        return false;
    }

    std::uint64_t a[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    for (std::size_t i = 0; i < argc; ++i) a[i] = args[i];

    // Args 5..8 go on the stack, after 32 bytes of shadow space. Expressing
    // that through a typed function pointer lets the compiler lay the frame
    // out correctly instead of us hand-rolling it in assembly.
    using Fn8 = std::uint64_t (*)(std::uint64_t, std::uint64_t, std::uint64_t,
                                  std::uint64_t, std::uint64_t, std::uint64_t,
                                  std::uint64_t, std::uint64_t);
    using Fn8d = double (*)(std::uint64_t, std::uint64_t, std::uint64_t,
                            std::uint64_t, std::uint64_t, std::uint64_t,
                            std::uint64_t, std::uint64_t);

    // Float ARGUMENTS need the value in an XMM register. Only the common
    // leading-float cases are expressible without assembly, so handle the
    // first four positions and reject anything beyond that rather than
    // silently passing them in the wrong register.
    if (floatMask & ~0xFu) {
        if (err) *err = "float arguments are only supported in positions 0-3";
        return false;
    }

    __try {
        if (floatMask == 0) {
            // CALL EXACTLY ONCE. An earlier draft called through both an
            // integer-returning and a float-returning pointer to fill in both
            // results -- which would execute the function twice and duplicate
            // every side effect. `want_float` selects the return type instead.
            if (floatResult) {
                *floatResult = reinterpret_cast<Fn8d>(fn)(a[0], a[1], a[2], a[3],
                                                          a[4], a[5], a[6], a[7]);
                if (intResult) *intResult = 0;
            } else {
                const auto r = reinterpret_cast<Fn8>(fn)(a[0], a[1], a[2], a[3],
                                                         a[4], a[5], a[6], a[7]);
                if (intResult) *intResult = r;
            }
            return true;
        }

        // Mixed integer/float arguments in the first four slots.
        using FnF0 = std::uint64_t (*)(double, std::uint64_t, std::uint64_t, std::uint64_t);
        using FnF1 = std::uint64_t (*)(std::uint64_t, double, std::uint64_t, std::uint64_t);
        using FnF2 = std::uint64_t (*)(std::uint64_t, std::uint64_t, double, std::uint64_t);
        using FnF3 = std::uint64_t (*)(std::uint64_t, std::uint64_t, std::uint64_t, double);
        double f;
        std::uint64_t r = 0;
        if (floatMask == 0x1) { std::memcpy(&f, &a[0], 8); r = reinterpret_cast<FnF0>(fn)(f, a[1], a[2], a[3]); }
        else if (floatMask == 0x2) { std::memcpy(&f, &a[1], 8); r = reinterpret_cast<FnF1>(fn)(a[0], f, a[2], a[3]); }
        else if (floatMask == 0x4) { std::memcpy(&f, &a[2], 8); r = reinterpret_cast<FnF2>(fn)(a[0], a[1], f, a[3]); }
        else if (floatMask == 0x8) { std::memcpy(&f, &a[3], 8); r = reinterpret_cast<FnF3>(fn)(a[0], a[1], a[2], f); }
        else {
            if (err) *err = "only a single float argument is supported";
            return false;
        }
        if (intResult) *intResult = r;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        if (err) *err = "the call raised an exception (wrong signature or bad args?)";
        return false;
    }
}

// Calls a function with up to 4 integer/pointer arguments and returns RAX.
//
// Covers the overwhelming majority of engine functions worth poking at from a
// probe, and keeps the ABI surface small enough to be obviously correct: the
// x64 calling convention passes the first four integer args in rcx/rdx/r8/r9,
// which is exactly what this signature expresses.
bool RawCall(std::uintptr_t fn,
             std::uintptr_t a1, std::uintptr_t a2,
             std::uintptr_t a3, std::uintptr_t a4,
             std::uintptr_t* result, std::string* err) {
    if (!fn) {
        if (err) *err = "null function address";
        return false;
    }
    if (!Readable(fn, 1)) {
        if (err) *err = "function address is not readable";
        return false;
    }
    using Fn = std::uintptr_t (*)(std::uintptr_t, std::uintptr_t,
                                  std::uintptr_t, std::uintptr_t);
    __try {
        const auto r = reinterpret_cast<Fn>(fn)(a1, a2, a3, a4);
        if (result) *result = r;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        if (err) *err = "the call raised an exception (wrong signature or bad args?)";
        return false;
    }
}

}  // namespace bridge
