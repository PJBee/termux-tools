# power_cycle

Repeatable battery drain / cooldown / charge logger for Termux on Android.

Automates a single, reproducible battery-test cycle so that charge-curve measurements are comparable across runs:

1. **DRAIN** — Saturate every CPU core with a busy-loop until the battery reaches a target low level, logging the discharge curve.
2. **COOLDOWN** — Drop the load and wait until the cell temperature falls below a threshold. A warm battery makes the charge controller throttle, so measuring a charge curve from a hot start tells you about thermal management, not the charger. We always start charging cold.
3. **CHARGE** — Log the charge curve until the battery reaches a target level.

## Table of Contents

- [Requirements](#requirements)
- [Usage](#usage)
- [Options](#options)
- [Output](#output)
- [Design](#design)

## Requirements

- **root (`su`)** on the device — used for sysfs battery reads
- **Termux** + **Termux:API** package and app — only if you use `--torch` or `--wakelock`
- **Python 3** (ships with Termux by default)

Optional:

- **`rich`** — `pip install rich` enables the live dashboard with refreshing panel and sparklines. Without it the tool runs in plain text.

## Usage

Run a full cycle (drain → cooldown → charge) with default thresholds:

```bash
python -m power_cycle
```

Custom thresholds:

```bash
python -m power_cycle --low 15 --cool 28
```

Run only the charge phase:

```bash
python -m power_cycle --phase charge
```

Enable CSV logging:

```bash
python -m power_cycle --log
python -m power_cycle --log --logfile charge.csv
```

Add the flashlight to the drain for maximum draw:

```bash
python -m power_cycle --torch
```

Hold a wakelock for honest timing with the screen off:

```bash
python -m power_cycle --wakelock
```

Plain stdout (e.g. over ADB):

```bash
python -m power_cycle --no-dash
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--phase` | `all` | Which phase(s) to run: `all`, `drain`, `cooldown`, `charge` |
| `--low` | `20` | Stop draining at this battery % |
| `--full` | `100` | Stop charging at this battery % |
| `--cool` | `30` | Cool down to this temperature (°C) before charging |
| `--sample` | `10` | Seconds between samples |
| `--torch` | — | Turn on the flashlight during drain |
| `--wakelock` | — | Hold a Termux wakelock for honest timing with screen off |
| `--log` | — | Write each phase to a timestamped CSV |
| `--logfile` | — | Custom CSV path (implies `--log`) |
| `--no-dash` | — | Plain stdout instead of the Rich dashboard |
| `--version` | — | Print version and exit |

## Output

### Live dashboard

When `rich` is installed, the tool renders a live panel with:

- Current readings (level, current, temperature, voltage, ETA, status)
- Rolling sparklines for battery level, temperature, and current

Without `rich`, each sample prints a single compact line to stdout.

### CSV logs

Pass `--log` to write one CSV per phase to `drain_<timestamp>.csv` and `charge_<timestamp>.csv` in the current directory. Use `--logfile` to specify a custom path.

Each CSV contains:

| Column | Unit |
|---|---|
| `epoch` | Unix timestamp |
| `capacity_pct` | % |
| `current_ma` | mA (sign convention varies by device — see below) |
| `temp_c` | °C |
| `voltage_v` | V |
| `status` | Charging / Discharging / Full / ... |

> **Note on `current_ma` sign:** Different kernels report `current_now` with different sign conventions. Verify on your device:
> ```bash
> su -c 'cat /sys/class/power_supply/battery/current_now'   # unplugged
> su -c 'cat /sys/class/power_supply/battery/current_now'   # charging
> ```

## Design

### Unprivileged reads

The Python interpreter runs **unprivileged**. Every battery reading is taken by shelling out to `su -c cat <sysfs node>`. Running the whole process as root under Termux invites `$PREFIX` / library-path breakage and SELinux-label / ownership damage on `/data`, which the Termux project explicitly warns against. Per-read shell-out is cheap and sidesteps all of that.

### Multi-core load

CPU load uses `multiprocessing`, not threads. CPython's GIL means a single process can only peg one core, so the tool forks one busy-loop worker per core. The fork start mode (default on Linux/Android) makes these workers cheap to create.

### Shutdown safety

A single `Ctrl-C` is enough to interrupt and clean up. The tool guarantees:

- All busy-loop workers are terminated
- The torch is turned off (if enabled)
- The wakelock is released (if acquired)
- The previous `SIGINT` handler is restored
