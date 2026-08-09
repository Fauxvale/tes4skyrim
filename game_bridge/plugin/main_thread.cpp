#include "main_thread.h"

#include <atomic>
#include <memory>

namespace bridge {

SKSETaskInterface* g_task = nullptr;
bool g_taskPumpLive = false;

namespace {

// Shared state between the waiting pipe thread and the task running on the
// main thread. Refcounted because the task may still be alive (about to be
// Disposed) after the waiter has given up on a timeout -- freeing this from the
// waiter would then be a use-after-free on the game thread.
struct Shared {
    std::mutex              m;
    std::condition_variable cv;
    bool                    done = false;
    std::function<void()>   fn;
};

class Task : public TaskDelegate {
public:
    explicit Task(std::shared_ptr<Shared> s) : s_(std::move(s)) {}

    void Run() override {
        // Never let an exception escape into the game's task pump.
        try {
            if (s_->fn) s_->fn();
        } catch (...) {
        }
        {
            std::lock_guard<std::mutex> lk(s_->m);
            s_->done = true;
        }
        s_->cv.notify_all();
    }

    void Dispose() override { delete this; }

private:
    std::shared_ptr<Shared> s_;
};

}  // namespace

MainThreadStatus RunOnMainThread(std::function<void()> fn, unsigned timeoutMs) {
    if (!g_task || !g_taskPumpLive) return MainThreadStatus::NoTaskInterface;

    auto shared = std::make_shared<Shared>();
    shared->fn = std::move(fn);

    g_task->AddTask(new Task(shared));

    std::unique_lock<std::mutex> lk(shared->m);
    const bool ran = shared->cv.wait_for(
        lk, std::chrono::milliseconds(timeoutMs), [&] { return shared->done; });

    return ran ? MainThreadStatus::Ok : MainThreadStatus::Timeout;
}

}  // namespace bridge
