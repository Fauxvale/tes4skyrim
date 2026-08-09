#pragma once

#include <string>

namespace bridge {

constexpr const char* kPluginName = "TESGameBridge";
constexpr const char* kPluginVersionString = "0.1.0";
constexpr unsigned    kPluginVersion = 1;

// Parses one JSON request line and returns one JSON response line.
std::string HandleRequest(const std::string& line);

// Called when a client disconnects, to drop per-session state.
void ReleaseSession();

}  // namespace bridge
