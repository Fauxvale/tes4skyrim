#include "addresses.h"

#include <windows.h>
#include <psapi.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>

#include "ids.h"
#include "log.h"

namespace bridge {

GameAddresses g_addr;
VersionDb     g_versionDb;

// ---------------------------------------------------------------- module ----

std::uintptr_t ModuleBase() {
    static std::uintptr_t base = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
    return base;
}

static bool TextRange(std::uintptr_t& begin, std::uintptr_t& end) {
    static std::uintptr_t b = 0, e = 0;
    if (!b) {
        auto base = ModuleBase();
        auto dos  = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
        auto nt   = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
        auto sec  = IMAGE_FIRST_SECTION(nt);
        for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i) {
            if (std::memcmp(sec[i].Name, ".text", 5) == 0) {
                b = base + sec[i].VirtualAddress;
                e = b + sec[i].Misc.VirtualSize;
                break;
            }
        }
    }
    begin = b; end = e;
    return b != 0;
}

// ------------------------------------------------------------- versionlib ----

namespace {

class Reader {
public:
    Reader(const std::uint8_t* d, size_t n) : d_(d), n_(n) {}
    bool ok() const { return !bad_; }
    size_t pos() const { return p_; }
    size_t size() const { return n_; }

    std::uint8_t  u8()  { if (p_ + 1 > n_) { bad_ = true; return 0; } return d_[p_++]; }
    std::uint16_t u16() { std::uint16_t v{}; return read(v); }
    std::uint32_t u32() { std::uint32_t v{}; return read(v); }
    std::int32_t  i32() { std::int32_t  v{}; return read(v); }
    std::uint64_t u64() { std::uint64_t v{}; return read(v); }
    void skip(size_t n) { if (p_ + n > n_) bad_ = true; else p_ += n; }

private:
    template <typename T> T read(T v) {
        if (p_ + sizeof(T) > n_) { bad_ = true; return 0; }
        std::memcpy(&v, d_ + p_, sizeof(T));
        p_ += sizeof(T);
        return v;
    }
    const std::uint8_t* d_;
    size_t n_, p_ = 0;
    bool bad_ = false;
};

// Delta decoder shared by the id and offset nibbles.
// Kinds 6 and 7 are u16/u32 -- reading them as u64 desyncs the entire stream,
// because every entry is delta-coded against the previous one.
std::uint64_t ReadKind(Reader& r, std::uint8_t kind, std::uint64_t prev) {
    switch (kind) {
        case 0: return r.u64();
        case 1: return prev + 1;
        case 2: return prev + r.u8();
        case 3: return prev - r.u8();
        case 4: return prev + r.u16();
        case 5: return prev - r.u16();
        case 6: return r.u16();
        case 7: return r.u32();
        default: return prev;
    }
}

std::string DataPluginsDir() {
    wchar_t buf[MAX_PATH]{};
    GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring w(buf);
    auto slash = w.find_last_of(L"\\/");
    std::wstring dir = (slash == std::wstring::npos) ? L"." : w.substr(0, slash);
    dir += L"\\Data\\SKSE\\Plugins\\";
    std::string out;
    for (wchar_t c : dir) out.push_back(static_cast<char>(c));
    return out;
}

}  // namespace

bool VersionDb::Load(std::uint32_t runtimeVersion) {
    // SKSE packs version as (major<<24)|(minor<<16)|(patch<<8)
    const unsigned maj   = (runtimeVersion >> 24) & 0xFF;
    const unsigned min   = (runtimeVersion >> 16) & 0xFF;
    const unsigned patch = (runtimeVersion >> 8)  & 0xFF;

    char name[128];
    std::snprintf(name, sizeof(name), "versionlib-%u-%u-%u-0.bin", maj, min, patch);
    return LoadFile(DataPluginsDir() + name);
}

bool VersionDb::LoadFile(const std::string& path) {
    map_.clear();
    loaded_ = false;
    path_ = path;

    std::ifstream f(path_, std::ios::binary);
    if (!f) return false;
    std::vector<std::uint8_t> data((std::istreambuf_iterator<char>(f)),
                                    std::istreambuf_iterator<char>());
    if (data.empty()) return false;

    Reader r(data.data(), data.size());
    if (r.i32() != 2) return false;          // format
    for (int i = 0; i < 4; ++i) r.i32();     // build quad
    const std::int32_t nameLen = r.i32();
    if (nameLen < 0) return false;
    r.skip(static_cast<size_t>(nameLen));
    const std::int32_t ptrSize = r.i32();
    const std::int32_t count   = r.i32();
    if (ptrSize <= 0 || count < 0 || !r.ok()) return false;

    map_.reserve(static_cast<size_t>(count));
    std::uint64_t prevId = 0, prevOff = 0;

    for (std::int32_t i = 0; i < count && r.ok(); ++i) {
        const std::uint8_t ctl = r.u8();
        const std::uint8_t lo  = ctl & 0x0F;
        const std::uint8_t hi  = (ctl >> 4) & 0x0F;

        const std::uint64_t id = ReadKind(r, lo, prevId);

        // Bit 3 of the high nibble scales against ptr_size: the PREVIOUS offset
        // is divided before the delta and the result multiplied after. It is
        // not a plain "scale the delta" -- that yields wrong RVAs for kinds
        // 0/6/7, which are absolute.
        const bool scaled = (hi & 0x08) != 0;
        const std::uint64_t base = scaled ? (prevOff / static_cast<std::uint64_t>(ptrSize)) : prevOff;
        std::uint64_t off = ReadKind(r, hi & 0x07, base);
        if (scaled) off *= static_cast<std::uint64_t>(ptrSize);

        map_[id] = off;
        prevId = id;
        prevOff = off;
    }

    // A desynced stream yields plausible-but-wrong addresses, so refuse it
    // outright rather than resolving garbage.
    if (!r.ok() || r.pos() != r.size()) {
        map_.clear();
        return false;
    }

    loaded_ = true;
    return true;
}

std::uintptr_t VersionDb::Get(std::uint64_t id) const {
    auto it = map_.find(id);
    if (it == map_.end()) return 0;
    return ModuleBase() + static_cast<std::uintptr_t>(it->second);
}

// ------------------------------------------------------------- signatures ----

std::uintptr_t ScanSignature(const char* pattern) {
    std::vector<int> bytes;  // -1 == wildcard
    for (const char* p = pattern; *p;) {
        if (*p == ' ') { ++p; continue; }
        if (*p == '?') { bytes.push_back(-1); while (*p == '?') ++p; continue; }
        bytes.push_back(static_cast<int>(std::strtoul(p, nullptr, 16)));
        while (*p && *p != ' ') ++p;
    }
    if (bytes.empty()) return 0;

    std::uintptr_t begin = 0, end = 0;
    if (!TextRange(begin, end)) return 0;

    const size_t n = bytes.size();
    for (std::uintptr_t a = begin; a + n <= end; ++a) {
        const auto* m = reinterpret_cast<const std::uint8_t*>(a);
        bool hit = true;
        for (size_t i = 0; i < n; ++i) {
            if (bytes[i] >= 0 && m[i] != static_cast<std::uint8_t>(bytes[i])) { hit = false; break; }
        }
        if (hit) return a;
    }
    return 0;
}

std::uintptr_t Resolve(const char* debugName, std::uint64_t stableId, const char* signature) {
    std::uintptr_t a = 0;
    if (stableId && g_versionDb.loaded()) a = g_versionDb.Get(stableId);
    if (!a && signature) a = ScanSignature(signature);
    if (!a) g_addr.missing.emplace_back(debugName);
    return a;
}

bool GameAddresses::Init(std::uint32_t runtimeVersion) {
    missing.clear();

    if (!g_versionDb.Load(runtimeVersion)) {
        Log("addresses: no Address Library database for this runtime; "
            "falling back to signature scans only");
    } else {
        Log("addresses: loaded %s (%zu entries)",
            g_versionDb.path().c_str(), g_versionDb.count());
    }

    consoleExecute = Resolve("ConsoleExecute", ids::kConsoleExecute, ids::kSigConsoleExecute);
    compileAndRun  = Resolve("CompileAndRun",  ids::kCompileAndRun,  ids::kSigCompileAndRun);
    scriptSetText  = Resolve("Script::SetText", ids::kScriptSetText, nullptr);
    memAlloc       = Resolve("MemAlloc",       ids::kMemAlloc,       nullptr);

    // A data address, not code -- signature scanning does not apply.
    scriptVtable   = Resolve("Script::vtable", ids::kScriptVtable,   nullptr);

    for (const auto& m : missing) Log("addresses: UNRESOLVED %s", m.c_str());

    // Console execution is the bridge's core capability. Everything else can
    // degrade to a reported E_UNSUPPORTED, but without these four the plugin
    // has nothing useful to offer, so say so loudly rather than half-loading.
    const bool core = consoleExecute && scriptSetText && memAlloc && scriptVtable;
    Log("addresses: core console capability %s", core ? "OK" : "UNAVAILABLE");
    return core;
}

}  // namespace bridge
