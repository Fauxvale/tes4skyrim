#!/usr/bin/env python3
"""Simulate a converted Say()-driven conversation and look for stalls.

The converted scenes (CharacterGen, the Daedric shrines, the SE chatter) are all
the same machine: a poller fires `Say(topic)` when `speaker == me && timer <= 0`,
PARKS the shared timer so no one else speaks over the line, and the INFO's End
fragment releases the park when the line finishes and hands `speaker` to whoever
is next.  Three things run concurrently and unsynchronised:

  * the OWNING script's countdown, on its own update interval
  * N pollers, each on its own interval
  * the engine's dialogue thread, which runs the End fragment at line end

That is a race, and it fails intermittently — which is exactly how it presents
in game ("sometimes actors play their lines and sometimes they do not").  This
module runs the interleavings deterministically at a fine tick so a regression
shows up as a reproducible stall instead of a rare in-game symptom.

Two models are provided:

  --model naive    the read-modify-write countdown (`t = t - dt`) and a flat park
  --model fixed    the snapshot-guarded countdown and a topic-sized park

What it reports per run: how many lines of the chain completed, and if it
stalled, the stage/convCount and the reason.

Usage:
    # the CharacterGen CharGenMain chain, both models, every interleaving
    python -m tools.say_timer_race_sim --lines 12 --sweep

    # one interleaving, verbose
    python -m tools.say_timer_race_sim --lines 6 --model fixed \
        --release-offset 0.05 --verbose

    # a dropped line (no INFO passes) - how long until the chain recovers?
    python -m tools.say_timer_race_sim --drop-at 3 --model fixed
"""
import argparse
import sys

# Defaults mirror the converted output (see script_convert/converter.py).
QUEST_TICK = 0.1        # CharGenQuest's RegisterForSingleUpdate
POLLER_TICK = 0.5       # the actor scripts' RegisterForSingleUpdate
THRESHOLD = 20.0        # SAY_TIMER_PARKED_THRESHOLD
FLAT_PARK = 60.0        # SAY_LINE_PARK_SECONDS
DROPPED_SLACK = 4.0     # SAY_LINE_DROPPED_TIMEOUT
SIM_TICK = 0.01         # finer than any script tick, so orderings are explicit


class Sim:
    """One conversation, driven to completion or to a stall."""

    def __init__(self, model='fixed', line_len=8.33, n_pollers=4,
                 release_offset=0.0, drop_at=None, verbose=False):
        self.model = model
        self.line_len = line_len
        self.n_pollers = n_pollers
        # Where inside a quest tick the End fragment lands. This is the whole
        # race: at 0 it coincides with the countdown's read/write.
        self.release_offset = release_offset
        self.drop_at = drop_at
        self.verbose = verbose

        self.timer = 0.0
        self.speaker = 1
        self.conv_count = 0
        self.lines_done = 0
        self.t = 0.0
        # Pending line: (ends_at, speaker) or None
        self.line = None
        self.stall_reason = None
        self._snapshot = None

    def park_value(self):
        if self.model == 'naive':
            return FLAT_PARK
        return min(FLAT_PARK, THRESHOLD + self.line_len + DROPPED_SLACK)

    def log(self, msg):
        if self.verbose:
            print(f'  t={self.t:7.2f} timer={self.timer:6.2f} '
                  f'spk={self.speaker} cc={self.conv_count}  {msg}')

    def countdown(self):
        """The owning script's tick."""
        if self.model == 'naive':
            # read-modify-write: the release can land between these two lines
            if self.timer > 0:
                snap = self.timer
                self._snapshot = snap
                # (the write happens in countdown_write, after the release
                #  window, so the interleaving is explicit)
        else:
            if self.timer > 0:
                self._snapshot = self.timer

    def countdown_write(self):
        if self._snapshot is None:
            return
        snap, self._snapshot = self._snapshot, None
        if self.model == 'naive':
            # unconditional write-back: resurrects a park cleared in between
            self.timer = snap - QUEST_TICK
        else:
            # abandon the decrement if anything moved the timer meanwhile
            if snap > 0 and self.timer == snap:
                self.timer = snap - QUEST_TICK

    def poll(self):
        """Every poller's tick. Only the current speaker can act."""
        if self.line is not None or self.timer > 0:
            return
        dropped = (self.drop_at is not None
                   and self.conv_count == self.drop_at)
        self.timer = self.park_value()          # park BEFORE the Say
        if dropped:
            self.log(f'Say dropped (no INFO at convCount {self.conv_count})')
            return                              # no line, no End fragment
        self.line = (self.t + self.line_len, self.speaker)
        self.log(f'Say -> line ends at {self.line[0]:.2f}')

    def fragment(self):
        """The End fragment: release the park and hand off."""
        if self.timer > THRESHOLD:
            self.timer = 0.0
        self.conv_count += 1
        self.speaker = (self.speaker % self.n_pollers) + 1
        self.lines_done += 1
        self.log('End fragment: released + handed off')

    def run(self, target_lines, horizon=600.0):
        next_quest = 0.0
        next_poll = 0.0
        last_progress = 0.0
        while self.t < horizon and self.lines_done < target_lines:
            # quest countdown tick (read, then write, with the release window
            # in between so the interleaving is the thing under test)
            if self.t >= next_quest - 1e-9:
                self.countdown()
                fired = False
                if (self.line is not None
                        and self.t + self.release_offset >= self.line[0]):
                    self.line = None
                    self.fragment()
                    fired = True
                    last_progress = self.t
                self.countdown_write()
                if fired:
                    self.log('(release landed inside the countdown statement)')
                next_quest = self.t + QUEST_TICK
            elif self.line is not None and self.t >= self.line[0]:
                self.line = None
                self.fragment()
                last_progress = self.t

            if self.t >= next_poll - 1e-9:
                self.poll()
                next_poll = self.t + POLLER_TICK

            # a stall: nothing has advanced for longer than any legal wait
            if self.t - last_progress > FLAT_PARK + 10:
                self.stall_reason = (
                    f'no line for {self.t - last_progress:.1f}s '
                    f'(timer={self.timer:.2f}, speaker={self.speaker}, '
                    f'convCount={self.conv_count})')
                break
            self.t += SIM_TICK
        if self.stall_reason is None and self.lines_done < 1e9:
            # Ran out of horizon rather than tripping the detector: still a
            # stall if the chain did not finish, and the state says why.
            self.stall_reason = (
                f'horizon {horizon:g}s reached after {self.lines_done} lines; '
                f'idle {self.t - last_progress:.1f}s '
                f'(timer={self.timer:.2f}, speaker={self.speaker}, '
                f'convCount={self.conv_count})')
        return self


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--model', choices=('naive', 'fixed'), default='fixed')
    ap.add_argument('--lines', type=int, default=12,
                    help='how many lines the chain must complete')
    ap.add_argument('--line-len', type=float, default=8.33,
                    help="longest measured line of the topic (CharGenMain=8.33)")
    ap.add_argument('--pollers', type=int, default=4,
                    help='concurrent speaker scripts (CharacterGen has 4)')
    ap.add_argument('--release-offset', type=float, default=0.0,
                    help='where in a quest tick the End fragment lands')
    ap.add_argument('--drop-at', type=int, default=None,
                    help='convCount at which Say() produces no line')
    ap.add_argument('--sweep', action='store_true',
                    help='try every release offset across one quest tick')
    ap.add_argument('--verbose', action='store_true')
    a = ap.parse_args()

    models = ('naive', 'fixed') if a.sweep else (a.model,)
    offsets = ([i * SIM_TICK for i in range(int(QUEST_TICK / SIM_TICK))]
               if a.sweep else [a.release_offset])

    rc = 0
    for model in models:
        stalls = []
        for off in offsets:
            s = Sim(model=model, line_len=a.line_len, n_pollers=a.pollers,
                    release_offset=off, drop_at=a.drop_at,
                    verbose=a.verbose).run(a.lines)
            if s.lines_done < a.lines:
                stalls.append((off, s.lines_done, s.stall_reason))
        total = len(offsets)
        ok = total - len(stalls)
        print(f'model={model:5}  {ok}/{total} interleavings completed '
              f'{a.lines} lines')
        for off, done, why in stalls[:5]:
            print(f'    STALL at release_offset={off:.2f}: '
                  f'{done}/{a.lines} lines - {why}')
        if stalls and model == 'fixed':
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
