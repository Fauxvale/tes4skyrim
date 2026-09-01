"""The GUI's two progress bars, driven by a run's stdout sentinel lines.

One bar makes a single 0-100% sweep per pipeline phase, combining that phase's
sub-phases; the other fills across the whole run.  Neither ever moves backwards.
`tkinter` is imported inside the widget constructor, so importing this module on
a machine without Tk still works.
See: docs/commentary/gui_progress.md#why-sentinels-on-stdout
"""
import re

from progress import PROGRESS_ENV_VAR, PhaseTracker, parse, parse_plan

__all__ = ["PROGRESS_ENV_VAR", "BANNER", "ProgressBars"]

#: A pipeline phase banner, e.g. "  Phase 6: BUILD TES5 PLUGIN".
BANNER = re.compile(r'^\s*phase\s+\d+\s*:', re.IGNORECASE)

#: Integer resolution of a determinate bar; a 0..1 fraction scales onto this.
_SCALE = 1000

#: The style whose layout carries a text element, so the bar can hold a caption.
_ITEM_STYLE = 'Item.Horizontal.TProgressbar'

_PLAIN_STYLE = 'Run.Horizontal.TProgressbar'


def _install_styles(style, colors: dict) -> None:
    """Define the two bar styles, one of which draws text inside the trough."""
    style.layout(_ITEM_STYLE, [
        ('Horizontal.Progressbar.trough',
         {'children': [('Horizontal.Progressbar.pbar',
                        {'side': 'left', 'sticky': 'ns'})],
          'sticky': 'nswe'}),
        ('Horizontal.Progressbar.label', {'sticky': 'nswe'})])
    for name, thick in ((_ITEM_STYLE, 18), (_PLAIN_STYLE, 8)):
        style.configure(name, troughcolor=colors['btn'],
                        background=colors['accent'], borderwidth=0,
                        thickness=thick, anchor='center',
                        foreground=colors['text'], font=('Segoe UI', 8))


class ProgressBars:
    """Two ttk bars plus their captions, as one griddable block.

    `line()` is the whole interface to the run's output: it returns True for a
    line the bars consumed, which the caller must then NOT render.
    """

    def __init__(self, parent, style, colors: dict, row: int):
        """Build the block into `parent`'s grid `row`, hidden until `show()`."""
        from tkinter import ttk
        self._style = style
        self._tracker = PhaseTracker()
        self._steps = 1
        self._determinate = False
        _install_styles(style, colors)
        self._frame = ttk.Frame(parent, style='Panel.TFrame')
        self._frame.grid(row=row, column=0, columnspan=2, sticky='ew',
                         padx=14, pady=(0, 6))
        self._frame.columnconfigure(0, weight=1)
        self._phase_cap = ttk.Label(self._frame, text='Current Phase',
                                    style='PanelSub.TLabel', anchor='w')
        self._phase_cap.grid(row=0, column=0, sticky='ew')
        self._phase = ttk.Progressbar(self._frame, style=_ITEM_STYLE,
                                      mode='indeterminate', maximum=_SCALE)
        self._phase.grid(row=1, column=0, sticky='ew', pady=(1, 6))
        self._run_cap = ttk.Label(self._frame, text='Overall',
                                  style='PanelSub.TLabel', anchor='w')
        self._run_cap.grid(row=2, column=0, sticky='ew')
        self._run = ttk.Progressbar(self._frame, style=_PLAIN_STYLE,
                                    mode='determinate', maximum=_SCALE)
        self._run.grid(row=3, column=0, sticky='ew', pady=(1, 0))
        self._frame.grid_remove()

    def show(self, steps_total: int = 1) -> None:
        """Reveal the block for a run of `steps_total` selected pipeline steps."""
        self._steps = max(1, int(steps_total or 1))
        self._tracker.reset()
        self._determinate = False
        self._phase.configure(mode='indeterminate')
        self._phase.start(40)
        self._run.configure(value=0)
        self._style.configure(_ITEM_STYLE, text='')
        self._phase_cap.configure(text='Current Phase')
        self._run_cap.configure(text='Overall  0%')
        self._frame.grid()

    def hide(self) -> None:
        """Stop and remove the block; the next `show()` starts a fresh run."""
        self._phase.stop()
        self._frame.grid_remove()

    def line(self, text: str) -> bool:
        """Feed one log line in; True when it was a sentinel, so do not render it.

        A phase banner is NOT consumed: it is a real log line that also starts a
        new sweep.
        """
        hit = parse(text)
        if hit:
            self._tracker.update(*hit)
            return self._redraw()
        hit = parse_plan(text)
        if hit:
            self._tracker.set_plan(hit[1])
            return self._redraw()
        if BANNER.match(text):
            self._tracker.banner()
            self._go_determinate()
            self._redraw()
        return False

    def _go_determinate(self) -> None:
        """Swap the phase bar out of its throbber, holding it at its value."""
        if self._determinate:
            return
        self._determinate = True
        self._phase.stop()
        self._phase.configure(mode='determinate', value=0)

    def _redraw(self) -> bool:
        """Repaint both bars from the tracker; always True, for `line()`."""
        self._go_determinate()
        frac = self._tracker.phase()
        run = self._tracker.overall(self._steps)
        self._phase.configure(value=int(frac * _SCALE))
        self._run.configure(value=int(run * _SCALE))
        self._phase_cap.configure(text='Current Phase  %d%%' % int(frac * 100))
        self._run_cap.configure(text='Overall  %d%%' % int(run * 100))
        self._style.configure(_ITEM_STYLE, text=self._tracker.item)
        return True
