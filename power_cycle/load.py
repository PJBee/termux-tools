"""
load — generate sustained CPU load to drain the battery.

Why multiprocessing and not threads
------------------------------------
CPython's Global Interpreter Lock means only one thread executes Python
bytecode at a time, so a thread-based busy-loop pegs exactly one core no
matter how many threads you spawn. To saturate the whole SoC we fork one
*process* per core, each running a tight `while True: pass`. `fork` start
mode (the default on Linux/Android) makes these workers cheap to create.

The optional torch (flashlight) stacks a high, steady extra draw on top of the
CPU load via `termux-torch`, for when you want the fastest possible drain.

Shutdown safety
---------------
Workers install SIG_IGN for SIGINT so that a Ctrl-C at the terminal is handled
once, by the parent, instead of each child racing to die and printing
tracebacks. The parent is responsible for calling `stop()` (the CLI does this
from a `finally:` block) so a stray core-pegging process can never outlive the
run.
"""

import multiprocessing as mp
import os
import signal
import subprocess


def _busy_worker():
    """Peg one CPU core until the process is terminated.

    SIGINT is ignored here so the parent owns shutdown; the parent calls
    `terminate()` on each worker, which arrives as SIGTERM and ends the loop.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        pass


class Load:
    """Manages the pool of busy-loop workers and the optional torch.

    Typical use:
        load = Load(torch=False)
        n = load.start()      # returns number of workers spawned
        ...                   # drain phase runs
        load.stop()           # idempotent; safe to call in finally:
    """

    def __init__(self, torch=False):
        """If `torch` is True, the device flashlight is switched on for the
        duration of the load (requires the termux-api package + app)."""
        self._procs = []
        self._torch = torch

    def start(self):
        """Spawn one busy-loop worker per CPU core; optionally light the torch.

        Returns the number of workers started so the caller can report it.
        """
        n = os.cpu_count() or 4  # fall back to 4 if the count is unavailable
        for _ in range(n):
            p = mp.Process(target=_busy_worker, daemon=True)
            p.start()
            self._procs.append(p)
        if self._torch:
            # capture_output keeps termux-torch's chatter off our dashboard;
            # failure (e.g. termux-api not installed) is non-fatal.
            subprocess.run(["termux-torch", "on"], capture_output=True)
        return n

    def stop(self):
        """Terminate all workers and turn the torch off. Idempotent."""
        for p in self._procs:
            p.terminate()
        for p in self._procs:
            p.join(timeout=2)
        self._procs = []
        if self._torch:
            subprocess.run(["termux-torch", "off"], capture_output=True)

    @property
    def running(self):
        """True while at least one worker process is alive."""
        return any(p.is_alive() for p in self._procs)
