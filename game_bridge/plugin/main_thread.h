// Main-thread marshalling.
//
// The pipe server runs on its own thread. Touching a form, reference, cell, or
// Havok object from that thread races the game loop and produces intermittent
// crashes and torn reads. Those failures look exactly like conversion bugs,
// which makes them the most expensive possible defect in this tool -- the whole
// point of the bridge is to be trustworthy about what the engine decided.
//
// So: every handler that reads or mutates game state runs through RunOnMainThread,
// which queues a task via SKSETaskInterface and blocks the caller until it has
// executed. Handlers that touch only plugin-local state may skip it.

#pragma once

#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>

#include "skse_abi.h"

namespace bridge {

extern SKSETaskInterface* g_task;

// Set true once the game is far enough along that tasks are actually pumped.
// Before this, AddTask would enqueue work that never runs and every call would
// hit its timeout.
extern bool g_taskPumpLive;

enum class MainThreadStatus { Ok, Timeout, NoTaskInterface };

// Runs `fn` on the game's main thread and waits for it.
//
// timeoutMs guards against a hang if the game is in a load screen or otherwise
// not pumping tasks; a timeout is reported as an error rather than blocking the
// pipe forever. Returns Ok only if `fn` actually ran to completion.
MainThreadStatus RunOnMainThread(std::function<void()> fn, unsigned timeoutMs = 5000);

}  // namespace bridge
