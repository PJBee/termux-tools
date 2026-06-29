#!/data/data/com.termux/files/usr/bin/env python3
"""
power_cycle.__main__ — CLI entry point, run as `python -m power_cycle`.

Runs a reproducible battery test cycle through three phases:

    DRAIN  ->  COOLDOWN  ->  CHARGE

so that charge curves are comparable run-to-run (always measured from a cool,
known starting state). See the package docstring (power_cycle/__init__.py) for
the rationale behind the unprivileged-reads / multiprocessing-load design.

By default each phase only samples and displays; pass --log to also write a
per-phase timestamped CSV.

Examples
--------
    python -m power_cycle                       # full cycle, default thresholds
    python -m power_cycle --low 15 --cool 28    # custom drain floor & cool target
    python -m power_cycle --phase charge        # only run the charge phase
    python -m power_cycle --log                 # also write per-phase CSVs
    python -m power_cycle --phase charge --logfile charge.csv  # log to a name
    python -m power_cycle --torch               # add the flashlight to the drain
    python -m power_cycle --no-dash             # plain stdout (e.g. over adb)
    python -m power_cycle --wakelock            # hold a Termux wakelock for the run

Requirements
------------
    * root (su) on the device — used for the sysfs reads
    * termux-api package + app  — only if you use --torch or --wakelock

Optional
--------
    * pip install rich  — enables the live dashboard (refreshing panel +
      sparklines). Without it the tool runs in plain text and prints a one-time
      notice at startup so you know the live view is off. Pass --no-dash to
      force plain text even when rich is installed (no notice in that case).

Run from the directory that contains the `power_cycle/` package folder, or put
that directory on your PYTHONPATH.
"""

import argparse
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
from collections import deque

# Relative imports: this module lives *inside* the package, so it pulls its
# building blocks from sibling modules rather than re-importing the package by
# name. This is what makes `python -m power_cycle` work cleanly.
from . import (
    __version__, find_battery, Load, Dashboard, rich_available,
    run_drain, run_cooldown, run_charge,
)

# How many samples the sparklines retain. The line is downsampled to the
# display width, so this is sized to cover an entire cycle rather than a
# trailing window: ~8640 samples at the default 10s cadence is 24 hours, which
# comfortably spans a full drain → cooldown → charge run. A few thousand floats
# is negligible memory.
_HISTORY_LEN = 8640

# Upper bound on the termux-api wakelock shell-outs, so an unresponsive
# Termux:API app can't make acquiring or (worse) releasing the wakelock hang.
_TERMUX_TIMEOUT = 5


def _termux(cmd):
    """Run a best-effort termux-api command; never raise, never block long.

    Used for the wakelock calls, which sit on the startup and shutdown paths
    where a hang would be especially user-hostile (a stalled shutdown is what
    makes people reach for a second Ctrl-C).
    """
    try:
        subprocess.run(cmd, capture_output=True, timeout=_TERMUX_TIMEOUT)
    except Exception:
        pass


def parse_args(argv=None):
    """Build and parse the command-line arguments."""
    ap = argparse.ArgumentParser(
        prog="python -m power_cycle",
        description="Repeatable battery drain / cooldown / charge logger.",
    )
    ap.add_argument("--phase", choices=["all", "drain", "cooldown", "charge"],
                    default="all",
                    help="which phase(s) to run (default: all)")
    ap.add_argument("--low", type=float, default=20, metavar="PCT",
                    help="stop draining at this battery %% (default: 20)")
    ap.add_argument("--full", type=float, default=100, metavar="PCT",
                    help="stop the charge log at this battery %% (default: 100)")
    ap.add_argument("--cool", type=float, default=30, metavar="DEG_C",
                    help="cool down to this temperature before charging "
                         "(default: 30 °C)")
    ap.add_argument("--sample", type=float, default=10, metavar="SEC",
                    help="seconds between samples (default: 10)")
    ap.add_argument("--torch", action="store_true",
                    help="also turn on the flashlight during the drain")
    ap.add_argument("--wakelock", action="store_true",
                    help="hold a Termux wakelock so timing stays honest when "
                         "the screen is off (needs termux-api)")
    ap.add_argument("--log", action="store_true",
                    help="write each phase to its own timestamped CSV "
                         "(default: off — sample and display only)")
    ap.add_argument("--logfile", metavar="PATH",
                    help="write the CSV to this path instead of an "
                         "auto-named one (implies --log; when more than one "
                         "phase is logged, the phase label is inserted before "
                         "the extension to keep them distinct)")
    ap.add_argument("--no-dash", action="store_true",
                    help="plain stdout instead of the Rich dashboard")
    ap.add_argument("--version", action="version",
                    version=f"power_cycle {__version__}")
    return ap.parse_args(argv)


def _resolve_logname(logfile, label, disambiguate):
    """Turn a user-supplied --logfile into a path for one phase.

    With a single logged phase the file is used verbatim; when more than one
    phase is logged we insert the label before the extension (foo.csv ->
    foo_drain.csv) so the curves don't clobber each other.
    """
    if not disambiguate:
        return logfile
    root, ext = os.path.splitext(logfile)
    return f"{root}_{label}{ext or '.csv'}"


def main(argv=None):
    args = parse_args(argv)

    # --logfile implies --log. When it's set and more than one phase will be
    # logged (only drain and charge write CSVs; cooldown never does), the
    # per-phase names must be disambiguated so they don't overwrite each other.
    log_enabled = args.log or args.logfile is not None
    logged_phases = [p for p in ("drain", "charge")
                     if args.phase in ("all", p)]
    disambiguate = len(logged_phases) > 1

    # Locate the battery node up front; bail clearly if we can't.
    batt = find_battery()
    if not batt:
        print("No battery sysfs node found. Inspect what's available with:\n"
              "    su -c 'ls /sys/class/power_supply/'\n"
              "then add the correct path to CANDIDATE_NODES in "
              "power_cycle/battery.py.")
        return 1
    print(f"Using battery node: {batt}")

    # Optional wakelock for honest timing with the screen off.
    if args.wakelock:
        _termux(["termux-wake-lock"])

    # Full-run history shared across phases. `sparkline` downsamples to fit the
    # display width, so retaining the whole run lets the level line show the
    # complete start→target curve instead of a trailing ~20-minute slice.
    history = {
        "cap": deque(maxlen=_HISTORY_LEN),
        "temp": deque(maxlen=_HISTORY_LEN),
        "cur": deque(maxlen=_HISTORY_LEN),
    }

    # Decide on the dashboard. Plain text is the baseline; the Rich live view
    # is a bonus that activates only when `rich` is installed. If the user
    # didn't opt out (--no-dash) but rich is missing, tell them what they're
    # missing — but stay quiet if they explicitly chose plain.
    want_dash = not args.no_dash
    if want_dash and not rich_available():
        print(
            "Note: the optional 'rich' package isn't installed, so the live\n"
            "      dashboard is disabled. You'll still get per-sample readings\n"
            "      (and CSVs if you pass --log) — just without the refreshing\n"
            "      panel, table, and sparklines.\n"
            "      Enable it with:  pip install rich\n"
        )
    dashboard = Dashboard(use_rich=want_dash, temp_floor=args.cool)
    load = Load(torch=args.torch)

    # `clock` gives each phase its own elapsed-time origin.
    def make_clock():
        start = time.time()
        return lambda: time.time() - start

    # Make a single Ctrl-C enough. The first SIGINT raises KeyboardInterrupt to
    # unwind whatever phase is running into the finally: block below, and at the
    # same moment switches SIGINT to SIG_IGN so any *further* Ctrl-C is ignored
    # while we clean up. Without this, a second Ctrl-C landing during shutdown
    # could abort it half-done (CPU load still pegged, torch still on); and if a
    # cleanup step ever blocked, the user would have to hit Ctrl-C again to get
    # out. Paired with the bounded termux/join timeouts, this guarantees the
    # cleanup always runs to completion on the first Ctrl-C.
    def _on_sigint(signum, frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    prev_sigint = signal.signal(signal.SIGINT, _on_sigint)
    try:
        if args.phase in ("all", "drain"):
            n = load.start()
            print(f"DRAIN: busy-loop on {n} cores -> {args.low}%")
            logname = (_resolve_logname(args.logfile, "drain", disambiguate)
                       if args.logfile else None)
            log = run_drain(batt, args.low, sample_s=args.sample,
                            history=history, dashboard=dashboard,
                            clock=make_clock(), log_to_file=log_enabled,
                            logname=logname)
            load.stop()
            if log:
                print(f"\nDrain log: {log}")

        if args.phase in ("all", "cooldown"):
            print(f"COOLDOWN: waiting until temp <= {args.cool} °C")
            run_cooldown(batt, args.cool, sample_s=args.sample,
                         history=history, dashboard=dashboard,
                         clock=make_clock())
            print("\nCool enough — plug in the charger now.")

        if args.phase in ("all", "charge"):
            print(f"CHARGE: logging until {args.full}% (plug in the charger)")
            logname = (_resolve_logname(args.logfile, "charge", disambiguate)
                       if args.logfile else None)
            log = run_charge(batt, args.full, sample_s=args.sample,
                             history=history, dashboard=dashboard,
                             clock=make_clock(), log_to_file=log_enabled,
                             logname=logname)
            if log:
                print(f"\nCharge log: {log}")

    except KeyboardInterrupt:
        print("\nInterrupted — shutting down cleanly.")
    finally:
        # These must always run: a stray core-pegging worker or a left-on
        # torch/wakelock would be a nasty thing to leave behind. Each step is
        # bounded (worker joins and termux calls all have timeouts) so cleanup
        # can't hang. Restore the previous SIGINT handler last, once there's
        # nothing left that a stray Ctrl-C could corrupt.
        load.stop()
        dashboard.close()
        if args.wakelock:
            _termux(["termux-wake-unlock"])
        signal.signal(signal.SIGINT, prev_sigint)

    return 0


if __name__ == "__main__":
    # `fork` keeps the busy-loop workers cheap and is the Linux/Android default;
    # set it explicitly so behavior is stable if that default ever changes.
    try:
        mp.set_start_method("fork")
    except RuntimeError:
        pass  # already set (e.g. when re-imported)
    sys.exit(main())
