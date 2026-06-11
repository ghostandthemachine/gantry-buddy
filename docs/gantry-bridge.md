# Gantry Bridge

`tools/gantry_bridge.py` streams Gantry state to Gantry Buddy over USB serial.
It reads:

- `~/.gantry/runs.jsonl` for model phases, completions, and token usage.
- `~/.gantry/activities.jsonl` for tool activity titles and terminal status.

The bridge emits the same heartbeat JSON shape the firmware already accepts:

```json
{"total":1,"running":1,"waiting":0,"msg":"model stream opened","entries":["model: Model stream opened"],"tokens":126,"tokens_today":126}
```

## Run

With the M5Stick plugged in:

```bash
python3 tools/gantry_bridge.py --port /dev/cu.usbserial-7952330CF0
```

The bridge depends on `pyserial`. On this Mac, PlatformIO's bundled Python
already has it:

```bash
/opt/homebrew/Cellar/platformio/6.1.19_2/libexec/bin/python tools/gantry_bridge.py --port /dev/cu.usbserial-7952330CF0
```

For a quick no-device check:

```bash
python3 tools/gantry_bridge.py --once --dry-run
```

To push a short one-shot message from Gantry or a shell hook:

```bash
python3 tools/gantry_bridge.py --once --message "deploy finished"
```

## Integration Shape

The bridge is intentionally host-side. Firmware stays compatible with Claude's
Hardware Buddy BLE bridge, while Gantry can add richer signals by calling this
tool or by reusing the JSON schema directly:

- `running > 0` makes the stick enter the busy state.
- `waiting > 0` makes the stick enter the attention state.
- `entries` populate the transcript page.
- `tokens_today` feeds the daily/token display.

Later Gantry app integration should wrap this script or move the same logic
into a sidecar process. The serial payload should stay newline-delimited JSON
so the existing firmware parser continues to work over both USB and BLE.
