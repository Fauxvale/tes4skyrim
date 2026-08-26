"""Tests for tools/live/quest_labtest.py.

Focused on the VM ring cursor, because its failure mode is SILENCE: a wrong
cursor produces a recording that looks healthy and contains nothing, and the
mistake is only visible after a 30-minute playthrough has already been wasted.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.live.quest_labtest import _vm_new_lines, _VM_GAP_MARKER  # noqa: E402


def test_first_poll_returns_everything():
    fresh, tail = _vm_new_lines(['a', 'b', 'c'], [])
    assert fresh == ['a', 'b', 'c']
    assert tail == ['a', 'b', 'c']


def test_growing_ring():
    _, tail = _vm_new_lines(['a', 'b'], [])
    fresh, _ = _vm_new_lines(['a', 'b', 'c'], tail)
    assert fresh == ['c']


def test_full_ring_advance_yields_only_new_lines():
    """The regression this file exists for.

    `vmlog` has no sequence number and its ring pins at the limit once full, so
    a `len(lines)` cursor stops advancing and emits NOTHING for the rest of the
    session. Measured live: the ring was already full at 200 lines before the
    recording even started.
    """
    ring1 = [str(i) for i in range(200)]
    _, tail = _vm_new_lines(ring1, [])
    ring2 = [str(i) for i in range(2, 202)]      # same length, advanced by 2
    fresh, _ = _vm_new_lines(ring2, tail)
    assert fresh == ['200', '201']


def test_no_advance_yields_nothing():
    ring = [str(i) for i in range(200)]
    _, tail = _vm_new_lines(ring, [])
    fresh, _ = _vm_new_lines(ring, tail)
    assert fresh == []


def test_repeated_spam_does_not_produce_phantom_lines():
    """Identical lines must not read as a huge advance.

    Other mods emit the same OnInit line hundreds of times. Preferring the
    LARGEST overlap makes 200 identical lines align at zero and report the whole
    ring as new every poll; the smallest consistent advance is correct.
    """
    spam = ['x'] * 200
    _, tail = _vm_new_lines(spam, [])
    fresh, _ = _vm_new_lines(spam, tail)
    assert fresh == []


def test_spam_followed_by_real_advance():
    _, tail = _vm_new_lines(['x'] * 200, [])
    fresh, _ = _vm_new_lines(['x'] * 197 + ['y', 'y', 'y'], tail)
    assert fresh == ['y', 'y', 'y']


def test_complete_wrap_is_reported_not_hidden():
    """Lost output must be stated. A gap silently swallowed is a lie."""
    _, tail = _vm_new_lines([str(i) for i in range(200)], [])
    fresh, _ = _vm_new_lines([str(i) for i in range(1000, 1200)], tail)
    assert fresh[0] == _VM_GAP_MARKER
    assert fresh[1:] == [str(i) for i in range(1000, 1200)]


def test_empty_poll_keeps_previous_tail():
    _, tail = _vm_new_lines(['a', 'b'], [])
    fresh, new_tail = _vm_new_lines([], tail)
    assert fresh == []
    assert new_tail == tail
