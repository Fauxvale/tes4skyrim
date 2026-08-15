#include "log.h"

#include <windows.h>
#include <shlobj.h>

#include <share.h>

#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <string>

namespace bridge {

namespace {
FILE*      g_file = nullptr;
std::mutex g_mutex;
}  // namespace

void OpenLog() {
    std::lock_guard<std::mutex> lk(g_mutex);
    if (g_file) return;

    PWSTR docs = nullptr;
    std::wstring path;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &docs))) {
        path = docs;
        CoTaskMemFree(docs);
        path += L"\\My Games\\Skyrim Special Edition\\SKSE\\";
        CreateDirectoryW(path.c_str(), nullptr);
        path += L"TESGameBridge.log";
    } else {
        path = L"TESGameBridge.log";
    }

    // _SH_DENYNO, not _wfopen_s. _wfopen_s takes the file EXCLUSIVELY, so the
    // log could not be read while the game was running -- precisely when it is
    // needed, since it is the only record of why a capability failed to
    // resolve. (2026-08-14: diagnosing an unresolved Address Library meant
    // asking the user to quit the game first.) _wfsopen keeps the same
    // fflush-per-line behaviour but lets other processes read concurrently.
    g_file = _wfsopen(path.c_str(), L"w", _SH_DENYNO);
    if (g_file) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        std::fprintf(g_file, "[%02d:%02d:%02d] TESGameBridge log opened\n",
                     st.wHour, st.wMinute, st.wSecond);
        std::fflush(g_file);
    }
}

void Log(const char* fmt, ...) {
    std::lock_guard<std::mutex> lk(g_mutex);
    if (!g_file) return;

    SYSTEMTIME st;
    GetLocalTime(&st);
    std::fprintf(g_file, "[%02d:%02d:%02d] ", st.wHour, st.wMinute, st.wSecond);

    va_list args;
    va_start(args, fmt);
    std::vfprintf(g_file, fmt, args);
    va_end(args);

    std::fputc('\n', g_file);
    std::fflush(g_file);  // flush every line: a crash must not lose the tail
}

void CloseLog() {
    std::lock_guard<std::mutex> lk(g_mutex);
    if (g_file) {
        std::fclose(g_file);
        g_file = nullptr;
    }
}

}  // namespace bridge
