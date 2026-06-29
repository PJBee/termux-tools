"""
dashboard — render the live view of a logging run.

Two back-ends behind one interface (`Dashboard`):
  * Rich:   a refreshing panel showing the current sample plus rolling
            sparklines for level, temperature, and current.
  * plain:  one line per sample on stdout, used with --no-dash or when the
            `rich` package isn't installed.

The caller doesn't need to know which back-end is active — it builds a
`Dashboard`, calls `.update(...)` each sample, and calls `.close()` at the end.
History (the deques feeding the sparklines) is owned by the caller and passed
in, so the dashboard stays stateless and easy to reason about.
"""

# Unicode block characters from lowest (1/8) to full height, used to draw
# sparklines. Eight levels is plenty of vertical resolution for a terminal.
_SPARK = "▁▂▃▄▅▆▇█"


def rich_available():
    """True if the optional `rich` package can be imported.

    `rich` is a *presentation-only* dependency: it powers the live dashboard
    (refreshing panel, readings table, sparklines) and nothing else. The
    measurement path — sysfs reads, load, CSV logging — is pure stdlib, so the
    tool is fully functional without it. The CLI uses this to decide whether to
    warn the user that the live view is unavailable.
    """
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


def sparkline(values, width=40, lo=None, hi=None, min_span=None):
    """Render an iterable of numbers as a unicode block sparkline.

    `None` values are skipped. If the series is longer than `width`, it is
    downsampled into `width` buckets (each column the mean of its bucket) so
    the line spans the *whole run*, not just the last `width` samples.

    Scaling:
      * `lo`/`hi` pin the value domain to a fixed range (e.g. 0–100 for a
        battery percent), so the glyph height reads as absolute magnitude.
      * When `lo`/`hi` are omitted the line auto-scales to the data's own
        min/max, showing *shape* rather than magnitude.
      * `min_span` sets a floor on the domain width. Without it, a slowly
        changing, quantized signal (integer percent, tenths of a degree)
        collapses to a 1-unit range and auto-scaling blows that single step
        up to full height — making a smooth trend look like it jumps between
        hi and lo. The floor keeps small wiggles small.

    Returns "" if there's no data.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    if len(vals) > width:
        step = len(vals) / width
        buckets = []
        for i in range(width):
            seg = vals[int(i * step):int((i + 1) * step)] or [vals[-1]]
            buckets.append(sum(seg) / len(seg))
        vals = buckets
    if lo is None:
        lo = min(vals)
    if hi is None:
        hi = max(vals)
    if min_span is not None and (hi - lo) < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    span = (hi - lo) or 1.0  # avoid divide-by-zero on a flat line
    out = []
    for v in vals:
        idx = int((v - lo) / span * (len(_SPARK) - 1))
        idx = min(len(_SPARK) - 1, max(0, idx))
        out.append(_SPARK[idx])
    return "".join(out)


def _fmt(v, suffix="", nd=1):
    """Format a possibly-None reading for display."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def _span_label(values, suffix="", nd=1):
    """A "lo–hi" range over the non-None values, or "" if there's none.

    Shown next to a sparkline so the glyph height has an absolute reference:
    a flat-looking line then reads as either genuinely steady (small range) or
    a real trend the auto-scale has zoomed into (wide range). Collapses to a
    single "v" when lo == hi."""
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if lo == hi:
        return _fmt(lo, suffix, nd)
    return f"{_fmt(lo, '', nd)}–{_fmt(hi, suffix, nd)}"


def _fmt_eta(seconds):
    """Format an ETA in seconds as H:MM:SS / MM:SS, or "—" if unknown."""
    if seconds is None:
        return "—"
    secs = int(seconds)
    hrs, rem = divmod(secs, 3600)
    mins, secs = divmod(rem, 60)
    if hrs:
        return f"~{hrs}:{mins:02d}:{secs:02d}"
    return f"~{mins:02d}:{secs:02d}"


class Dashboard:
    """Live view abstraction with a Rich back-end and a plain fallback.

    Plain text is the baseline. The Rich live panel is used only when both
    (a) the caller asks for it (`use_rich=True`) and (b) the `rich` package is
    importable. If Rich is requested but unavailable, this falls back to plain
    output *silently* — surfacing that to the user is the CLI's job (see
    `rich_available()`), so library callers aren't spammed with print().

    Parameters
    ----------
    use_rich : bool
        If True, use the Rich dashboard when `rich` is installed; otherwise
        fall back to plain output. If False, always use plain output.
    """

    def __init__(self, use_rich=True):
        self._live = None
        if use_rich and rich_available():
            # Imported lazily so the package works without Rich installed.
            from rich.live import Live
            self._live = Live(auto_refresh=False, screen=False)
            self._live.start()

    def update(self, phase, s, history, elapsed, logname, eta_s=None,
               level_range=(0, 100)):
        """Render one sample.

        Parameters
        ----------
        phase : str        current phase label ("drain"/"cooldown"/"charge")
        s : dict           a sample() dict
        history : dict     {"cap": deque, "temp": deque, "cur": deque}
        elapsed : float    seconds since the phase started
        logname : str      path of the CSV being written (or a placeholder)
        eta_s : float|None  estimated seconds to the target capacity, or None
                            when there's no target (cooldown) or no estimate yet
        level_range : (lo, hi)  domain the level sparkline is pinned to. Defaults
                            to the full 0–100% gauge; the drain phase narrows the
                            floor to its target so the descent fills the height.
        """
        mins, secs = divmod(int(elapsed), 60)
        clock = f"{mins:02d}:{secs:02d}"

        if self._live is None:
            # plain back-end: one compact line per sample
            print(f"[{phase}] {clock}  "
                  f"lvl={_fmt(s['capacity_pct'], '%')}  "
                  f"cur={_fmt(s['current_ma'], 'mA')}  "
                  f"temp={_fmt(s['temp_c'], '°C')}  "
                  f"v={_fmt(s['voltage_v'], 'V', nd=3)}  "
                  f"eta={_fmt_eta(eta_s)}  "
                  f"{s['status'] or '?'}")
            return

        # Rich back-end: build a panel with a readings grid + sparkline grid.
        from rich.table import Table
        from rich.panel import Panel
        from rich.console import Group

        readings = Table.grid(padding=(0, 2))
        readings.add_column(justify="right", style="bold cyan")
        readings.add_column()
        readings.add_row("phase", f"[bold yellow]{phase}[/]  "
                                  f"({s['status'] or '?'})")
        readings.add_row("elapsed", clock)
        readings.add_row("level", _fmt(s["capacity_pct"], "%"))
        readings.add_row("current", _fmt(s["current_ma"], " mA"))
        readings.add_row("temp", _fmt(s["temp_c"], " °C"))
        readings.add_row("voltage", _fmt(s["voltage_v"], " V", nd=3))
        readings.add_row("eta", _fmt_eta(eta_s))
        readings.add_row("log", logname)

        sparks = Table.grid(padding=(0, 2))
        sparks.add_column(justify="right", style="dim")
        sparks.add_column(style="green")
        # level: pinned to `level_range` (default the full 0–100% gauge; drain
        # narrows the floor to its target). temp: auto-scaled so genuine small
        # trends fill the height, with a small floor to keep sensor jitter from
        # being amplified, plus a numeric range so the magnitude is explicit.
        # current: swings widely under load, so plain auto-scale reads best.
        lvl_lo, lvl_hi = level_range
        sparks.add_row("level", sparkline(history["cap"], lo=lvl_lo, hi=lvl_hi))
        sparks.add_row("temp",
                       sparkline(history["temp"], min_span=2) + "  "
                       + _span_label(history["temp"], " °C"))
        sparks.add_row("current",
                       sparkline(history["cur"]) + "  "
                       + _span_label(history["cur"], " mA", nd=0))

        self._live.update(
            Panel(Group(readings, "", sparks),
                  title="power_cycle", border_style="blue")
        )
        self._live.refresh()

    def close(self):
        """Tear down the Rich live view (no-op for the plain back-end)."""
        if self._live is not None:
            self._live.stop()
