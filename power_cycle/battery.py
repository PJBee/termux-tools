"""
battery — locate the battery sysfs node and read normalized samples from it.

Everything in this module that touches hardware goes through `su -c cat`,
because the interpreter runs unprivileged (see the package docstring for why).
Reads are best-effort: a missing or unreadable node yields `None` for that
field rather than raising, so a single flaky read never kills a logging run.

Raw sysfs units and the conversions we apply
--------------------------------------------
    capacity        integer percent                      -> kept as-is (%)
    current_now     microamps (µA)                       -> milliamps (mA)
    temp            tenths of a degree Celsius (0.1 °C)   -> degrees (°C)
    voltage_now     microvolts (µV)                      -> volts (V)
    status          string: Charging/Discharging/Full/…  -> kept as-is

IMPORTANT — current_now sign convention is NOT universal.
Many Qualcomm kernels report `current_now` as *negative while discharging*
and positive while charging; others invert this, and some report magnitude
only. Verify on your specific device once:

    su -c 'cat /sys/class/power_supply/battery/current_now'   # unplugged
    su -c 'cat /sys/class/power_supply/battery/current_now'   # charging

Then interpret the logged `current_ma` column accordingly. We deliberately do
NOT guess or auto-correct the sign, because guessing wrong silently corrupts
every curve.
"""

import subprocess
import time

# Candidate sysfs power-supply nodes, in priority order. Different SoCs/ROMs
# expose the main battery under different names; `find_battery()` probes these.
# If none match on your device, list what's actually there with:
#     su -c 'ls /sys/class/power_supply/'
# and prepend the correct path here.
CANDIDATE_NODES = [
    "/sys/class/power_supply/battery",
    "/sys/class/power_supply/bms",
    "/sys/class/power_supply/Battery",
]

# Column order for the per-phase CSV logs. Kept here (next to `sample()`) so
# the writer and the reader can't drift out of sync.
CSV_FIELDS = ["epoch", "capacity_pct", "current_ma", "temp_c",
              "voltage_v", "status"]

# Timeout (seconds) for each root shell-out. Generous enough for a cold `su`
# prompt, short enough that a wedged read won't hang the whole loop.
_SU_TIMEOUT = 5


def su_cat(path):
    """Read a single sysfs file as root.

    Returns the stripped file contents as a str, or None if the read failed
    for any reason (no root, missing node, timeout). Never raises — callers
    treat None as "this field is unavailable this sample".
    """
    try:
        out = subprocess.run(
            ["su", "-c", f"cat {path}"],
            capture_output=True, text=True, timeout=_SU_TIMEOUT,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        # subprocess.TimeoutExpired, FileNotFoundError (no `su`), etc.
        pass
    return None


def _su_is_dir(path):
    """True if `path` exists and is a directory, tested via root."""
    try:
        r = subprocess.run(
            ["su", "-c", f"[ -d {path} ] && echo y"],
            capture_output=True, text=True, timeout=_SU_TIMEOUT,
        )
        return r.stdout.strip() == "y"
    except Exception:
        return False


def find_battery():
    """Return the first existing battery sysfs node, or None if none found.

    Probes CANDIDATE_NODES in order. The caller should treat None as fatal and
    tell the user to inspect /sys/class/power_supply/ manually.
    """
    for node in CANDIDATE_NODES:
        if _su_is_dir(node):
            return node
    return None


def _to_float(s):
    """Parse a sysfs string to float, or None if it isn't a number.

    sysfs values are usually clean integers, but guarding against junk keeps a
    single bad read from poisoning the curve.
    """
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def sample(batt):
    """Take one normalized reading from the battery node `batt`.

    Returns a dict with keys matching CSV_FIELDS. Any field whose underlying
    sysfs node is unreadable comes back as None. Units are normalized per the
    module docstring (mA, °C, V); `capacity_pct` and `status` are raw.
    """
    cap = _to_float(su_cat(f"{batt}/capacity"))      # %
    cur = _to_float(su_cat(f"{batt}/current_now"))   # µA
    temp = _to_float(su_cat(f"{batt}/temp"))         # 0.1 °C
    volt = _to_float(su_cat(f"{batt}/voltage_now"))  # µV
    status = su_cat(f"{batt}/status")                # str

    return {
        "epoch": time.time(),
        "capacity_pct": cap,
        "current_ma": (cur / 1000.0) if cur is not None else None,
        "temp_c": (temp / 10.0) if temp is not None else None,
        "voltage_v": (volt / 1_000_000.0) if volt is not None else None,
        "status": status,
    }
