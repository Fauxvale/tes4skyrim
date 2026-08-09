// Address Library stable IDs.
//
// Every ID here was derived by locating the target in the GOG/AE 1.6.659 build
// (the only non-DRM-packed copy, so the only one that disassembles statically)
// and inverting its RVA through versionlib-1-6-659-0.bin. The same ID then
// resolves on ANY build shipping a versionlib -- including the DRM-packed Steam
// 1.6.1170 the user actually plays. That is why nothing here is a raw RVA.
//
// VERIFIED translations (659 -> 1170), checked against both databases:
//   21964  CompileAndRun        0x303cf0  -> 0x343c20
//   368092 compilerNameTable    0x1e5cc70 -> 0x1ff2d60
//   386860 hkClass hkpRigidBody 0x1e8f798 -> 0x2025e78
//   387372 hkClass hkaRagdoll   0x1e90798 -> 0x2026e78
//
// Re-deriving one: find the address in the GOG exe, then
//   python tools/address_lib.py --rva <rva> --from 1.6.659
//
// !! Do NOT add "IDs" for globals like g_thePlayer / g_console by taking the
// nearest-below versionlib entry. Those live inside one enormous .data symbol
// (id 414365 on 1.6.640) at offsets of hundreds of KB, and that offset does not
// survive a rebuild -- it resolves to a plausible-looking wrong pointer, which
// is the worst failure mode this plugin can have. Reach such state through the
// console/Papyrus instead, or discover it via RTTI at runtime.

#pragma once

#include <cstdint>

namespace bridge::ids {

// Script::CompileAndRun(ScriptCompiler*, ScriptBuffer*, Script*)
//
// The inner compile+run step. Callable, but it expects a ScriptBuffer the
// caller has already built -- see kConsoleExecute for why we do not do that.
constexpr std::uint64_t kCompileAndRun = 21964;

// The console's full command executor:
//   (rcx = Script*, rdx = TESObjectREFR* target, r8 = ?, r9d = compilerType)
//
// r9d indexes the compiler-name table; 1 == "SysWindowCompileAndRun", the
// console's own compiler. This function builds the ScriptBuffer, selects the
// compiler and calls CompileAndRun itself -- both call sites of
// kCompileAndRun live inside it.
//
// Preferring this over CompileAndRun is the whole point: we hand over the
// command text and the engine constructs every intermediate structure, so the
// bridge never has to match a ScriptBuffer layout. A wrong layout would not
// fail loudly; it would corrupt memory or read garbage.
constexpr std::uint64_t kConsoleExecute = 21954;

// Table of compiler-type name pointers. Index 1 is "SysWindowCompileAndRun",
// the compiler the console itself uses.
constexpr std::uint64_t kCompilerNameTable = 368092;

// Havok class registry entries, used by the ragdoll probe to identify objects
// by their hkClass pointer rather than by guessing at struct layout.
constexpr std::uint64_t kHkClassRigidBody      = 386860;
constexpr std::uint64_t kHkClassRagdollInstance = 387372;

// Script object construction.
//
// The engine allocates 0x80 bytes and calls Script::ctor, which stores the
// vtable and zeroes the members. We do the same thing using the vtable directly
// rather than calling the constructor, because Script::ctor's stable ID (21874)
// exists in the 1.6.659 database but NOT in 1.6.1170 -- calling through a
// missing ID would jump to address 0.
//
// Both of these DO resolve on 1.6.1170 (verified against both databases).
constexpr std::uint64_t kScriptVtable = 191694;  // 0x166e680 -> 0x17c1618
constexpr std::uint64_t kMemAlloc     = 68115;   // 0xc390a0  -> 0xcc40c0
constexpr std::size_t   kScriptSize   = 0x80;    // from the console's own alloc

// Script::SetText(const char*) -- allocates a copy and stores it at script+0x38.
// (Verified by disassembly: `mov [rbp+0x38], rdi` after the alloc.)
constexpr std::uint64_t kScriptSetText = 21883;  // 0x2fd170 -> 0x33cf80

// Offset of the script text pointer. The only struct offset the plugin assumes,
// and it is read straight out of Script::SetText's own store instruction.
constexpr std::size_t kScriptTextOffset = 0x38;

// ---------------------------------------------------------------------------
// Signature fallbacks. Used only when versionlib has no entry (e.g. a game
// update landed before the Address Library was refreshed). A target that
// resolves through neither path is reported as E_UNSUPPORTED and never called.
// ---------------------------------------------------------------------------

// Script::CompileAndRun prologue on 1.6.x:
//   mov [rsp+8],rbx / mov [rsp+10],rbp / mov [rsp+18],rsi / push rdi
//   sub rsp,20 / mov rbx,r8 / mov rsi,rdx / mov rbp,rcx / test rdx,rdx / je
//
// Verified against GOG 1.6.659: exactly ONE match in .text, at 0x140303cf0.
// (Uniqueness matters more than length here -- a signature that matches twice
// resolves to whichever comes first, silently.)
constexpr const char* kSigCompileAndRun =
    "48 89 5C 24 08 48 89 6C 24 10 48 89 74 24 18 57 48 83 EC 20 "
    "49 8B D8 48 8B F2 48 8B E9 48 85 D2 0F 84";

// Console executor prologue. Verified unique in .text on GOG 1.6.659
// (exactly one match, at 0x1403013a0).
constexpr const char* kSigConsoleExecute =
    "48 8B C4 57 48 81 EC C0 00 00 00 48 C7 44 24 20 FE FF FF FF "
    "48 89 58 08 48 89 70 10 41 8B D9 48 8B FA";

}  // namespace bridge::ids
