// A minimal, deliberately strict x64 detour.
//
// Shared by console_capture.cpp and papyrus_capture.cpp so there is exactly one
// implementation of the risky part.
//
// WHY IT IS STRICT
// ----------------
// Relocating an instruction that is rip-relative silently changes what it
// reads: the copy in the trampoline computes its target from the TRAMPOLINE's
// address, not the original's. Nothing crashes -- the game just reads the wrong
// memory, which surfaces later as behaviour indistinguishable from a conversion
// bug. That is the single most expensive failure mode for this project, so the
// caller must state exactly which bytes it expects to steal, and the install
// aborts if the prologue does not match byte for byte.
//
// This is not a general-purpose hooking library and must not become one; it
// works only for prologues that have been read in a disassembler first.

#pragma once

#include <cstdint>
#include <cstddef>

namespace bridge {

// Installs an absolute-jump detour at `target`.
//
//   target       function to hook (its first `stolenLen` bytes are replaced)
//   detour       replacement function
//   expected     the exact bytes the prologue must currently contain
//   stolenLen    how many bytes to relocate; must end on an instruction
//                boundary and must be >= 12
//   outOriginal  receives a callable trampoline: the stolen instructions
//                followed by a jump back into the original
//   what         label used in log messages
//
// Returns false (and changes nothing) if the prologue does not match, if the
// length is too small, or if memory could not be allocated/protected.
bool InstallDetour(std::uintptr_t target,
                   void*          detour,
                   const std::uint8_t* expected,
                   std::size_t    stolenLen,
                   void**         outOriginal,
                   const char*    what);

// Follows a `jmp rel32` at or shortly after `addr`, returning its destination.
// Used for thunk-style entry points that tail-call the real implementation.
// Returns 0 if no jump is found within `maxScan` bytes.
std::uintptr_t FollowTailJump(std::uintptr_t addr, std::size_t maxScan = 32);

}  // namespace bridge
