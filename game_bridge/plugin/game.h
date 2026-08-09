// Thin wrappers over the engine functions the bridge drives.
//
// Everything here MUST be called on the main thread (see main_thread.h). The
// only exception is IsGameLoaded()/RuntimeVersion(), which read a flag.
//
// No RVA is hardcoded. Each entry point is resolved once via versionlib stable
// ID with a signature fallback, and a failed resolve degrades to a reported
// E_UNSUPPORTED instead of a call through a null or wrong pointer.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace bridge {

// ---------------------------------------------------------------- state ----

bool IsGameLoaded();       // a save/new game is active (not sitting at the main menu)
bool IsInLoadingScreen();
std::uint32_t RuntimeVersion();

// ---------------------------------------------------------------- console ----

struct ConsoleResult {
    bool        ok = false;
    std::string output;   // captured console print output
    std::string error;
};

// Runs a console command line exactly as if typed, optionally with `ref`
// selected as the implicit "this" (the console's prid/selected reference).
//
// This uses the game's own console compiler, so we inherit its argument parsing
// for every command rather than reimplementing it per command.
ConsoleResult RunConsoleCommand(const std::string& command, std::uint32_t refFormId = 0);

// ------------------------------------------------------------------ forms ----

struct FormInfo {
    bool          found = false;
    std::uint32_t formId = 0;
    std::string   editorId;
    std::string   name;
    std::string   type;       // 4-char record signature, e.g. "NPC_"
    std::uint8_t  formType = 0;
};

FormInfo LookupByFormId(std::uint32_t formId);
FormInfo LookupByEditorId(const std::string& editorId);

// Resolve "0x1234", "1234", or an editor ID to a form id. 0 on failure.
std::uint32_t ResolveFormRef(const std::string& text);

// --------------------------------------------------------------- player ----

struct PlayerState {
    bool        valid = false;
    float       x = 0, y = 0, z = 0;
    float       angleZ = 0;
    std::uint32_t cellFormId = 0;
    std::string cellEditorId;
    std::string worldspace;
};

PlayerState GetPlayerState();

// ------------------------------------------------------------- readback ----

struct BoneXform {
    std::string name;
    float world[3]{};
    float rot[9]{};
};

// What the engine actually built for a reference. This is the payload the whole
// bridge exists for: `hasRagdoll=false` / `rigidBodies=0` on an actor that looks
// correct on screen is the silent-binding signature that offline validators miss.
struct RagdollInfo {
    bool has3D = false;
    bool graphBound = false;
    bool hasRagdoll = false;
    int  rigidBodies = 0;
    int  constraints = 0;
    bool inWorld = false;      // ragdoll actually raised into the physics world
    std::string skeletonPath;
    std::vector<BoneXform> bones;
    std::string note;
};

RagdollInfo ProbeRagdoll(std::uint32_t refFormId, bool includeBones);

struct AnimInfo {
    bool        has3D = false;
    bool        graphBound = false;
    std::string projectName;
    std::string activeClip;
    std::string stateMachineNode;
    int         clipCount = 0;
    std::vector<std::string> clips;
    std::string note;
};

AnimInfo ProbeAnim(std::uint32_t refFormId);

// ------------------------------------------------------------ hot reload ----

struct ReloadResult {
    bool ok = false;
    int  reloaded = 0;
    int  refreshedRefs = 0;
    std::string note;
};

// Drops a cached model so the next load re-reads it from disk, then refreshes
// 3D on loaded references using it.
ReloadResult ReloadModel(const std::string& relativePath);

// Forces a reference to rebuild its 3D from the (possibly just-reloaded) model.
ReloadResult Refresh3D(std::uint32_t refFormId);

}  // namespace bridge
