"""
phases — the three stages of a test cycle and their CSV logging.

Each logged phase (`run_drain`, `run_charge`) opens its own timestamped CSV,
samples the battery on a fixed cadence, updates the dashboard, appends each
row, and stops when the capacity crosses a target. `run_cooldown` is the odd
one out: it takes no log (nothing is being measured, we're just waiting) and
blocks until the cell is cool enough to start a meaningful charge curve.

All three share the rolling `history` deques so the dashboard's sparklines
carry continuously across phase boundaries within a single run.

A note on sample cadence and Android doze
-----------------------------------------
With the screen off, the kernel may suspend between samples, stretching the
real gap well beyond `sample_s`. The busy-loop load mostly prevents this
during DRAIN, but COOLDOWN and CHARGE have no load — so for honest timing,
hold a wakelock for the run (the CLI does `termux-wake-lock` when asked) or
keep the screen on.
"""

import contextlib
import csv
from datetime import datetime

from .battery import sample, CSV_FIELDS


def _timestamp():
    """Filename-safe local timestamp, e.g. 20260628_153012."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _push_history(history, s):
    """Append a sample's numeric fields to the rolling history deques."""
    if s["capacity_pct"] is not None:
        history["cap"].append(s["capacity_pct"])
    if s["temp_c"] is not None:
        history["temp"].append(s["temp_c"])
    if s["current_ma"] is not None:
        history["cur"].append(s["current_ma"])


def _estimate_eta(epoch_start, cap_start, epoch_now, cap_now, target_pct):
    """Estimate seconds until capacity reaches `target_pct`, or None.

    Uses the average rate since the phase started: simple and stable, at the
    cost of reacting slowly if the discharge/charge rate changes mid-run. We
    deliberately return None (shown as "—") rather than a misleading number
    whenever the estimate can't be trusted:

      * any capacity reading missing,
      * not enough elapsed time / no capacity movement yet (rate ~ 0), or
      * the battery is momentarily moving the *wrong* way (e.g. a blip up
        during drain), which would yield a negative ETA.
    """
    if cap_start is None or cap_now is None:
        return None
    dt = epoch_now - epoch_start
    dcap = cap_now - cap_start
    if dt <= 0 or dcap == 0:
        return None
    rate = dcap / dt                      # %/sec, signed
    # cap(t) = cap_now + rate*t; solve cap(t) == target. Positive for both
    # drain (rate<0, target<cap_now) and charge (rate>0, target>cap_now);
    # negative means the battery is moving away from the target.
    eta = (target_pct - cap_now) / rate
    return eta if eta > 0 else None


def _run_logged(batt, label, target_pct, stop_when, *,
                sample_s, history, dashboard, clock, log_to_file, logname=None):
    """Shared body for drain/charge: sample -> log -> dashboard -> check.

    Parameters
    ----------
    batt : str            battery sysfs node path
    label : str           phase label, also the CSV filename prefix
    target_pct : float    capacity threshold to stop at
    stop_when : callable  (level, target) -> bool; True means "stop now"
    sample_s : float      seconds between samples
    history : dict         rolling deques for the sparklines
    dashboard : Dashboard  live view to update each sample
    clock : callable      returns elapsed seconds since the run started
    log_to_file : bool    write a per-phase CSV; if False, sample and display
                          only and write nothing to disk
    logname : str | None  explicit CSV path; when None (and logging is on),
                          a timestamped `{label}_{stamp}.csv` is generated

    Returns the path of the CSV that was written, or None if logging was off.
    """
    # Anchor the ETA on the first usable (epoch, capacity) pair so the average
    # rate spans the whole phase. Set on the first sample that has both.
    epoch_start = cap_start = None
    # When logging is on, open the CSV (explicit name or a per-phase timestamped
    # default); otherwise run the same sample/dashboard loop but write nothing
    # to disk. ExitStack lets the file (when present) close on exit without
    # duplicating the loop body.
    with contextlib.ExitStack() as stack:
        if log_to_file:
            if logname is None:
                logname = f"{label}_{_timestamp()}.csv"
            # newline="" lets csv handle line endings itself (documented).
            fh = stack.enter_context(open(logname, "w", newline=""))
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
        else:
            logname = None
            fh = writer = None

        while True:
            s = sample(batt)
            if writer is not None:
                writer.writerow(s)
                fh.flush()  # flush each row so a killed run leaves good data
            _push_history(history, s)

            level = s["capacity_pct"]
            if cap_start is None and level is not None:
                epoch_start, cap_start = s["epoch"], level
            eta_s = _estimate_eta(epoch_start, cap_start,
                                  s["epoch"], level, target_pct)
            dashboard.update(label, s, history, clock(),
                             logname or "(no log)", eta_s=eta_s)

            if level is not None and stop_when(level, target_pct):
                break
            _sleep(sample_s)

    return logname


# Indirection so tests can monkeypatch sleeping if ever needed; also keeps the
# import local and obvious.
def _sleep(seconds):
    import time
    time.sleep(seconds)


def run_drain(batt, target_low, *, sample_s, history, dashboard, clock,
              log_to_file=False, logname=None):
    """Log the discharge curve until capacity <= `target_low` percent.

    Assumes the caller has already started the CPU load; this function only
    samples and (when `log_to_file`) logs. Returns the CSV path, or None.
    """
    return _run_logged(
        batt, "drain", target_low,
        stop_when=lambda level, target: level <= target,
        sample_s=sample_s, history=history, dashboard=dashboard, clock=clock,
        log_to_file=log_to_file, logname=logname,
    )


def run_charge(batt, target_full, *, sample_s, history, dashboard, clock,
               log_to_file=False, logname=None):
    """Log the charge curve until capacity >= `target_full` percent.

    The user must plug in the charger; logging simply records whatever the
    battery reports, including the initial not-yet-charging samples. Returns
    the CSV path, or None if logging was off.
    """
    return _run_logged(
        batt, "charge", target_full,
        stop_when=lambda level, target: level >= target,
        sample_s=sample_s, history=history, dashboard=dashboard, clock=clock,
        log_to_file=log_to_file, logname=logname,
    )


def run_cooldown(batt, cool_c, *, sample_s, history, dashboard, clock):
    """Block until the battery temperature falls to `cool_c` °C or below.

    Takes no log — nothing is being measured, we're only waiting so the
    subsequent charge curve starts from a known-cool state and isn't distorted
    by thermal throttling. Updates the dashboard so the user can watch the
    temperature fall.
    """
    while True:
        s = sample(batt)
        _push_history(history, s)
        dashboard.update("cooldown", s, history, clock(), "(no log)")

        temp = s["temp_c"]
        if temp is not None and temp <= cool_c:
            return
        _sleep(sample_s)
