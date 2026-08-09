// Standalone test for the versionlib parser.
//
// The parser is the foundation every resolved address rests on, and its failure
// mode is silent: a desynced stream still produces plausible-looking numbers,
// which would then be called as function pointers. So it is verified against
// known-good values produced by tools/address_lib.py before shipping.
//
// Build and run:
//   cl /nologo /EHsc /std:c++17 /DBRIDGE_TEST_MAIN test_versiondb.cpp addresses.cpp log.cpp
//   test_versiondb.exe "<path to versionlib-1-6-1170-0.bin>" <runtimeVersion>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "addresses.h"

using namespace bridge;

namespace {

struct Expect {
    std::uint64_t id;
    std::uint64_t rva;
    const char*   label;
};

// Values cross-checked with:
//   python tools/address_lib.py --id <id> --from 1.6.1170
const Expect kExpected1170[] = {
    {21954,  0x3412d0,  "ConsoleExecute"},
    {21964,  0x343c20,  "CompileAndRun"},
    {21883,  0x33cf80,  "Script::SetText"},
    {68115,  0xcc40c0,  "MemAlloc"},
    {191694, 0x17c1618, "Script::vtable"},
    {368092, 0x1ff2d60, "compilerNameTable"},
    {107327, 0x14e00c0, "AddressLib smoke test"},
};

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf("usage: %s <path-to-versionlib.bin>\n", argv[0]);
        return 2;
    }

    const std::string dbPath = argv[1];
    std::printf("versionlib parser test\n  %s\n", dbPath.c_str());

    VersionDb db;
    if (!db.LoadFile(dbPath)) {
        std::printf("FAIL  could not parse the database\n");
        std::printf("      the parser refuses a desynced stream on purpose --\n"
                    "      a partial parse would yield wrong-but-plausible RVAs.\n");
        return 1;
    }

    std::printf("  %zu entries\n\n", db.count());

    // Entry count from the Python reference parser (tools/address_lib.py).
    if (db.count() != 428461) {
        std::printf("FAIL  expected 428461 entries for 1.6.1170, got %zu\n", db.count());
        return 1;
    }

    int failures = 0;
    const auto base = ModuleBase();  // 0 in this harness; we compare RVAs

    for (const auto& e : kExpected1170) {
        const auto addr = db.Get(e.id);
        const auto rva  = addr ? (addr - base) : 0;
        const bool ok = (rva == e.rva);
        if (!ok) ++failures;
        std::printf("  %-5s id %-7llu %-22s got 0x%llx  want 0x%llx\n",
                    ok ? "PASS" : "FAIL",
                    static_cast<unsigned long long>(e.id), e.label,
                    static_cast<unsigned long long>(rva),
                    static_cast<unsigned long long>(e.rva));
    }

    std::printf("\n%s (%d failure(s))\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
