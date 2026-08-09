// Minimal game struct layouts.
//
// Only the fields the bridge actually reads are declared, each with the offset
// it sits at. Anything not needed is padding -- guessing at a full layout is how
// a plugin ends up reading plausible garbage.
//
// LAYOUT WARNING: these are 1.6.629+ layouts. The plugin declares
// kVersionIndependent_StructsPost629 and refuses to load on older runtimes,
// because the same offsets silently mean different things pre-629.

#pragma once

#include <cstdint>

namespace bridge {

// BSFixedString / BSString: the engine's ref-counted string. We only ever read
// the char* out of one.
struct BSFixedString {
    const char* data;
};

struct NiPoint3 {
    float x, y, z;
};

// Console script buffer. Built by the console before handing to CompileAndRun.
// We construct one the same way rather than reimplementing its parsing.
struct ScriptBuffer;
struct Script;
struct ScriptCompiler;

// TESForm header. formID/formType are stable across 1.6.x and are all the
// bridge needs to identify a form.
struct TESForm {
    void*         vtbl;        // 0x00
    std::uint8_t  pad08[0x14]; // 0x08
    std::uint32_t formFlags;   // 0x1C
    std::uint32_t formID;      // 0x20
    std::uint16_t pad24;       // 0x24
    std::uint8_t  formType;    // 0x26
};

// Form type codes we care about (TESForm::formType).
enum FormType : std::uint8_t {
    kFormType_NPC       = 43,
    kFormType_Reference = 61,
    kFormType_Character = 62,
};

}  // namespace bridge
