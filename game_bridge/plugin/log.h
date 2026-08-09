// Plugin log, written to Documents\My Games\Skyrim Special Edition\SKSE\
// TESGameBridge.log so it sits alongside the Papyrus logs the project already
// reads for diagnosis.

#pragma once

namespace bridge {

void OpenLog();
void Log(const char* fmt, ...);
void CloseLog();

}  // namespace bridge
