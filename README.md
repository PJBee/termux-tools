# termux-tools

A collection of small, focused utilities for Termux on Android.

## Tools

| Tool | Description |
|---|---|
| [**power_cycle**](power_cycle/) | Repeatable battery drain / cooldown / charge logger for measuring discharge and charge curves under consistent conditions. |

## Requirements

All tools share these base requirements:

- **Termux** + **Python 3** (ships with Termux by default)
- **root (`su`)** — required by tools that read sysfs nodes

Optional per-tool:

- **Termux:API** package and app — required by tools that use `termux-*` commands
- **`rich`** — `pip install rich` enables live dashboards where available

## Using a tool

Each tool lives in its own directory. Run it from the folder that contains it:

```bash
cd power_cycle
python -m power_cycle
```

Or add the parent directory to `PYTHONPATH`:

```bash
export PYTHONPATH="$PWD"
python -m power_cycle
```

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
