#!/data/data/com.termux/files/usr/bin/env python3
"""
power_cycle.__main__ — CLI entry point, run as `python -m power_cycle`.

Runs a reproducible battery test cycle and logs each phase to its own CSV:

    DRAIN  ->  COOLDOWN  ->  CHARGE

so that charge curves are comparable run-to-run (always measured from a cool,
known starting state). See the package docstring (power_cycle/__init__.py) for
the rationale behind the unprivileged-reads / multiprocessing-load design.

Examples
--------
    python -m power_cycle                       # full cycle, default thresholds
    python -m power_cycle --low 15 --cool 28    # custom drain floor & cool target
    python -m power_cycle --phase charge        # only log a charge curve
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

# How many recent samples the sparklines retain. ~120 samples at the default
# 10s cadence is 20 minutes of visible history.
_HISTORY_LEN = 120


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
    ap.add_argument("--no-dash", action="store_true",
                    help="plain stdout instead of the Rich dashboard")
    ap.add_argument("--version", action="version",
                    version=f"power_cycle {__version__}")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

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
        subprocess.run(["termux-wake-lock"], capture_output=True)

    # Rolling history shared across phases so sparklines stay continuous.
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
            "      dashboard is disabled. You'll still get full CSV logging\n"
            "      and per-sample readings — just without the refreshing\n"
            "      panel, table, and sparklines.\n"
            "      Enable it with:  pip install rich\n"
        )
    dashboard = Dashboard(use_rich=want_dash)
    load = Load(torch=args.torch)

    # `clock` gives each phase its own elapsed-time origin.
    def make_clock():
        start = time.time()
        return lambda: time.time() - start

    try:
        if args.phase in ("all", "drain"):
            n = load.start()
            print(f"DRAIN: busy-loop on {n} cores -> {args.low}%")
            log = run_drain(batt, args.low, sample_s=args.sample,
                            history=history, dashboard=dashboard,
                            clock=make_clock())
            load.stop()
            print(f"\nDrain log: {log}")

        if args.phase in ("all", "cooldown"):
            print(f"COOLDOWN: waiting until temp <= {args.cool} °C")
            run_cooldown(batt, args.cool, sample_s=args.sample,
                         history=history, dashboard=dashboard,
                         clock=make_clock())
            print("\nCool enough — plug in the charger now.")

        if args.phase in ("all", "charge"):
            print(f"CHARGE: logging until {args.full}% (plug in the charger)")
            log = run_charge(batt, args.full, sample_s=args.sample,
                             history=history, dashboard=dashboard,
                             clock=make_clock())
            print(f"\nCharge log: {log}")

    except KeyboardInterrupt:
        print("\nInterrupted — shutting down cleanly.")
    finally:
        # These must always run: a stray core-pegging worker or a left-on
        # torch/wakelock would be a nasty thing to leave behind.
        load.stop()
        dashboard.close()
        if args.wakelock:
            subprocess.run(["termux-wake-unlock"], capture_output=True)

    return 0


if __name__ == "__main__":
    # `fork` keeps the busy-loop workers cheap and is the Linux/Android default;
    # set it explicitly so behavior is stable if that default ever changes.
    try:
        mp.set_start_method("fork")
    except RuntimeError:
        pass  # already set (e.g. when re-imported)
    sys.exit(main())
