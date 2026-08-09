// Runtime address resolution.
//
// Two independent mechanisms, tried in order:
//   1. Address Library (versionlib-<ver>.bin) stable ID -> RVA. Authoritative
//      and already deployed in this install for 1.6.1170.
//   2. Signature scan of .text. Survives a game update that ships before a new
//      versionlib does.
//
// Nothing here hardcodes an RVA. A wrong address in a plugin does not fail
// loudly -- it reads plausible garbage, which is the worst outcome for a tool
// whose entire job is to be trusted about what the engine decided.

#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace bridge {

// Address Library v2 database reader.
class VersionDb {
public:
    // Loads Data/SKSE/Plugins/versionlib-<maj>-<min>-<patch>-0.bin, resolved
    // relative to the running executable.
    bool Load(std::uint32_t runtimeVersion);

    // Loads a specific file. Used by the offline parser test, which runs
    // outside the game and so cannot derive the path from the module.
    bool LoadFile(const std::string& path);

    // stable ID -> absolute address, or 0.
    std::uintptr_t Get(std::uint64_t id) const;

    bool loaded() const { return loaded_; }
    const std::string& path() const { return path_; }
    size_t count() const { return map_.size(); }

private:
    std::unordered_map<std::uint64_t, std::uint64_t> map_;  // id -> rva
    bool        loaded_ = false;
    std::string path_;
};

// Byte-pattern scanner over the main module's .text.
// Pattern syntax: "48 8B 05 ?? ?? ?? ?? 48 85 C0"
std::uintptr_t ScanSignature(const char* pattern);

// Module base / .text bounds of SkyrimSE.exe.
std::uintptr_t ModuleBase();

// Resolve by stable ID, falling back to a signature. Returns 0 if both fail;
// callers must treat 0 as "capability unavailable" and report E_UNSUPPORTED
// rather than calling through it.
std::uintptr_t Resolve(const char* debugName,
                       std::uint64_t stableId,
                       const char* signature = nullptr);

// Everything the bridge calls into. Populated once at DataLoaded.
struct GameAddresses {
    // Console command execution. We drive the console's own executor, which
    // builds the compiler/buffer/script internally, so the plugin never has to
    // match a ScriptBuffer layout.
    std::uintptr_t consoleExecute    = 0;   // id 21954
    std::uintptr_t compileAndRun     = 0;   // id 21964 (inner step; unused by default)

    // Script object construction.
    std::uintptr_t scriptVtable      = 0;   // id 191694
    std::uintptr_t scriptSetText     = 0;   // id 21883
    std::uintptr_t scriptDtor        = 0;
    std::uintptr_t memAlloc          = 0;   // id 68115

    // Form lookup.
    std::uintptr_t lookupFormByID    = 0;

    // Asset caches, for hot reload.
    std::uintptr_t modelDb           = 0;

    bool Init(std::uint32_t runtimeVersion);

    // Which optional capabilities resolved. Reported by the `capabilities`
    // command so the client can degrade gracefully instead of guessing.
    std::vector<std::string> missing;
};

extern GameAddresses g_addr;
extern VersionDb     g_versionDb;

}  // namespace bridge
