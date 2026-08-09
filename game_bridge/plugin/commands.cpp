// Command dispatch.
//
// Every handler that touches game state runs inside RunOnMainThread. Handlers
// that only read plugin-local state answer directly on the pipe thread.

#include "commands.h"

#include <windows.h>

#include <atomic>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include "addresses.h"
#include "game.h"
#include "json.h"
#include "log.h"
#include "main_thread.h"

namespace bridge {

namespace {

// -------------------------------------------------------------- session ----

std::mutex               g_sessionMutex;
std::set<std::uint32_t>  g_spawned;      // refs this session created
bool                     g_bridgeStarted = false;  // we ran the coc ourselves
int                      g_spawnCap = 256;

Json Err(const char* code, const std::string& msg) {
    Json j = Json::Object();
    j.set("ok", Json(false));
    j.set("code", Json(code));
    j.set("error", Json(msg));
    return j;
}

Json Ok(Json result) {
    Json j = Json::Object();
    j.set("ok", Json(true));
    j.set("result", std::move(result));
    return j;
}

// Wraps a main-thread handler, converting marshalling failures into structured
// errors instead of letting the pipe hang.
Json OnMainThread(const std::function<Json()>& fn, unsigned timeoutMs = 5000) {
    Json out;
    bool ran = false;
    const auto status = RunOnMainThread([&] { out = fn(); ran = true; }, timeoutMs);

    switch (status) {
        case MainThreadStatus::Ok:
            return ran ? out : Err("E_INTERNAL", "handler did not produce a result");
        case MainThreadStatus::Timeout:
            return Err("E_LOADING",
                       "game did not process the request in time (loading screen?)");
        case MainThreadStatus::NoTaskInterface:
        default:
            return Err("E_INTERNAL", "task interface unavailable");
    }
}

// ------------------------------------------------------------- handlers ----

Json CmdPing(const Json&) {
    Json r = Json::Object();
    r.set("pong", Json(true));
    r.set("plugin_version", Json(kPluginVersionString));
    r.set("runtime_version", Json(RuntimeVersion()));
    r.set("game_loaded", Json(IsGameLoaded()));
    return Ok(std::move(r));
}

Json CmdCapabilities(const Json&) {
    Json caps = Json::Object();
    caps.set("console", Json(g_addr.consoleExecute != 0));
    caps.set("script_alloc", Json(g_addr.memAlloc != 0 && g_addr.scriptVtable != 0));

    Json missing = Json::Array();
    for (const auto& m : g_addr.missing) missing.push(Json(m));

    Json r = Json::Object();
    r.set("capabilities", std::move(caps));
    r.set("unresolved", std::move(missing));
    r.set("game_loaded", Json(IsGameLoaded()));
    return Ok(std::move(r));
}

Json CmdConsole(const Json& args) {
    const std::string command = args["command"].asString();
    if (command.empty()) return Err("E_INTERNAL", "missing 'command'");

    std::uint32_t ref = 0;
    if (args.has("ref")) {
        const Json& r = args["ref"];
        ref = r.isNumber() ? r.asU32() : ResolveFormRef(r.asString());
    }

    return OnMainThread([&]() -> Json {
        ConsoleResult cr = RunConsoleCommand(command, ref);
        if (!cr.ok) return Err("E_INTERNAL", cr.error.empty() ? "command failed" : cr.error);
        Json r = Json::Object();
        r.set("output", Json(cr.output));
        return Ok(std::move(r));
    });
}

Json CmdStatus(const Json&) {
    if (!IsGameLoaded()) {
        Json r = Json::Object();
        r.set("game_loaded", Json(false));
        r.set("in_main_menu", Json(true));
        return Ok(std::move(r));
    }
    return OnMainThread([&]() -> Json {
        Json r = Json::Object();
        r.set("game_loaded", Json(true));
        r.set("in_main_menu", Json(false));
        {
            std::lock_guard<std::mutex> lk(g_sessionMutex);
            r.set("tracked_spawns", Json(static_cast<int>(g_spawned.size())));
            r.set("bridge_started_session", Json(g_bridgeStarted));
        }
        return Ok(std::move(r));
    });
}

}  // namespace

// ------------------------------------------------------------- dispatch ----

void ReleaseSession() {
    std::lock_guard<std::mutex> lk(g_sessionMutex);
    if (!g_spawned.empty()) {
        Log("session: client disconnected with %zu tracked spawns; "
            "they remain in the world until cleanup or reload",
            g_spawned.size());
    }
    g_spawned.clear();
}

std::string HandleRequest(const std::string& line) {
    std::string parseErr;
    Json req = Json::Parse(line, &parseErr);
    if (!req.isObject()) {
        Json j = Err("E_INTERNAL", "malformed request: " + parseErr);
        return j.dump();
    }

    const std::string cmd = req["cmd"].asString();
    const Json& args = req["args"];

    const DWORD t0 = GetTickCount();
    Json resp;

    if (cmd == "ping")              resp = CmdPing(args);
    else if (cmd == "capabilities") resp = CmdCapabilities(args);
    else if (cmd == "console")      resp = CmdConsole(args);
    else if (cmd == "status")       resp = CmdStatus(args);
    else resp = Err("E_UNSUPPORTED", "unknown command: " + cmd);

    if (req.has("id")) resp.set("id", req["id"]);
    resp.set("elapsed_ms", Json(static_cast<int>(GetTickCount() - t0)));
    return resp.dump();
}

}  // namespace bridge
