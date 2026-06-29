"""
power_cycle — repeatable battery drain / cooldown / charge logging for Termux.

This package automates a single, reproducible battery-test cycle so that
charge-curve measurements are comparable across runs:

    1. DRAIN     Saturate every CPU core with a busy-loop until the battery
                 reaches a target low level, logging the discharge curve.
    2. COOLDOWN  Drop the load and wait until the cell temperature falls below
                 a threshold. This is the crux of the whole exercise: a warm
                 battery makes the charge controller throttle, so measuring a
                 charge curve from a hot start tells you about thermal
                 management, not the charger. We always start charging cold.
    3. CHARGE    Log the charge curve until the battery reaches a target level.

Design constraints that shaped the module layout
-------------------------------------------------
* The interpreter runs UNPRIVILEGED. Every battery reading is taken by
  shelling out to `su -c cat <sysfs node>` (see `battery.py`). Running the
  whole Python process as root under Termux invites $PREFIX / library-path
  breakage and SELinux-label / ownership damage on /data, which the Termux
  project explicitly warns against. Per-read shell-out is cheap and sidesteps
  all of that.
* CPU load uses `multiprocessing`, not threads. CPython's GIL means a single
  process can only peg one core, so we fork one busy-loop worker per core
  (see `load.py`).

Module map
----------
    battery    Locate the battery sysfs node, read raw values via root, and
               normalize them into sane units (%, mA, °C, V).
    load       Start/stop the multi-core busy-loop and the optional torch.
    dashboard  Render a live Rich panel (or plain stdout) of the current
               sample plus rolling sparklines.
    phases     The drain / cooldown / charge loops and their CSV logging.

The CLI entry point lives in `__main__.py`; run the tool as
`python -m power_cycle`.
"""

__version__ = "1.0.0"

# Public API re-exports, so callers can do `from power_cycle import sample`
# without caring which submodule a thing lives in.
from .battery import find_battery, sample, CSV_FIELDS          # noqa: F401
from .load import Load                                          # noqa: F401
from .dashboard import Dashboard, sparkline, rich_available     # noqa: F401
from .phases import run_drain, run_cooldown, run_charge         # noqa: F401

__all__ = [
    "__version__",
    "find_battery",
    "sample",
    "CSV_FIELDS",
    "Load",
    "Dashboard",
    "sparkline",
    "rich_available",
    "run_drain",
    "run_cooldown",
    "run_charge",
]
