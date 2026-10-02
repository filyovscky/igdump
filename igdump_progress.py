"""Plain-text progress bars that also work in saved console logs."""


def format_progress(stage: str, current: int, total: int | None = None, detail: str = "") -> str:
    if total is None or total <= 0:
        counter = f"[...] {current}"
    else:
        current = max(0, min(current, total))
        width = 24
        filled = width * current // total
        counter = f"[{'#' * filled}{'-' * (width - filled)}] {current}/{total} ({100 * current // total}%)"
    return f"{stage} {counter}" + (f" — {detail}" if detail else "")


import logging
import sys
import time
from collections import deque
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.text import Text

_ACTIVE = None
_LAST_LOG = (None, 0.0)


class TerminalUI(logging.Handler):
    """Fixed bottom status panel; logs remain in the upper part of the screen."""

    def __init__(self, quiet=False, console=None):
        super().__init__()
        self.console = console or Console(stderr=True)
        self.enabled = self.console.is_terminal and not quiet
        self.logs = deque(maxlen=200)
        self.requests = 0
        self.started = time.monotonic()
        self.progress = Progress(SpinnerColumn(), TextColumn("[bold]{task.description}"), BarColumn(),
                                 TaskProgressColumn(), MofNCompleteColumn(), TimeElapsedColumn(), console=self.console)
        self.task = self.progress.add_task("Подготовка", total=None)
        self.layout = Layout()
        self.layout.split_column(Layout(name="logs", ratio=1), Layout(name="status", size=6))
        self.live = None

    def __enter__(self):
        global _ACTIVE
        if self.enabled:
            _ACTIVE = self
            self.render()
            self.live = Live(self.layout, console=self.console, auto_refresh=True, refresh_per_second=4,
                             screen=False, vertical_overflow="crop")
            self.live.start()
        return self

    def __exit__(self, *args):
        global _ACTIVE
        if self.live:
            self.live.stop()
        _ACTIVE = None

    def emit(self, record):
        if self.enabled:
            color = "red" if record.levelno >= logging.ERROR else "yellow" if record.levelno >= logging.WARNING else "dim"
            self.logs.append(Text(record.getMessage(), style=color))
            self.render()
        else:
            self.console.print(f"{record.levelname} {record.getMessage()}", markup=False)

    def render(self):
        rows = max(1, self.console.size.height - 9)
        self.layout["logs"].update(Panel(Group(*list(self.logs)[-rows:]), title="igdump", border_style="dim", padding=(0,1)))
        self.layout["status"].update(Panel(Group(self.progress, Text(f"HTTP-запросов: {self.requests}  •  сохранение прогресса включено", style="dim")),
                                         title="Прогресс", border_style="cyan"))

    def update(self, stage, current, total=None, detail=""):
        self.progress.update(self.task, description=stage + (f" · {detail}" if detail else ""), total=total or None, completed=current)
        self.render()


def update_progress(stage, current, total=None, detail=""):
    global _LAST_LOG
    if _ACTIVE:
        _ACTIVE.update(stage,current,total,detail)
    else:
        now = time.monotonic()
        if _LAST_LOG[0] == stage and now - _LAST_LOG[1] < 2 and current != total:
            return
        _LAST_LOG = (stage, now)
        logging.getLogger("igdump").info("%s", format_progress(stage,current,total,detail))


def record_request():
    if _ACTIVE:
        _ACTIVE.requests += 1
        _ACTIVE.render()


def prompt_input(message):
    """Pause the redraw while the user types a login confirmation or cookie."""
    live = _ACTIVE.live if _ACTIVE else None
    if live:
        live.stop()
    try:
        return input(message)
    finally:
        if live:
            live.start()
