# Bambu Studio connector

A Claude plugin for 3D printing with Bambu Lab printers and Bambu Studio on macOS. Claude finds your model files, understands what's inside them, preps plates, and watches your printer — **you press print**.

## What it does

Ask Claude things like:

- "What parts are in this hive model?" — lists every piece with dimensions, renders a labeled preview, and identifies which piece is which
- "Print just the base" — extracts only that part into a clean file and loads it onto the print bed
- "Scale it to 20%, lay everything flat, and arrange it" — separates a model into objects, scales, rotates parts flat, and pre-arranges them on the plate
- "Is the print bed clear?" — grabs a camera snapshot and gives a verdict with the photo
- "How's the print going?" — progress, temps, time remaining; can run on a schedule

By design, this plugin **never starts a print, heats anything, or sends control commands**. It preps and watches; the "Print plate" click in Bambu Studio stays yours. This also means it works with the printer in normal cloud mode — no Developer Mode, and the Bambu Handy app keeps working.

## Install

From the marketplace (Claude Code):

```
/plugin marketplace add <your-github-user>/bambu-studio-connector
/plugin install bambu-studio-connector@rosetta-plugins
```

In Claude Cowork: Settings → Capabilities → add the marketplace repo URL, or install a packaged `.plugin` file directly.

## Requirements

- macOS with [Bambu Studio](https://bambulab.com/en/download/studio) in /Applications
- Node.js 18+
- Python 3 (for part inspection/extraction; `numpy` + `matplotlib` for previews)
- A Bambu Lab printer on your network (for the monitoring features)

## Setup

Easiest: tell Claude "sign into my Bambu account". You'll get a verification code by email (your password never enters the chat); Claude then pulls your printer's serial + access code from your account, discovers its IP on the LAN, and writes `printer-config.json` itself. Token lives in `~/.bambu-studio-connector/` (~3 months).

Manual: create `printer-config.json` in `~/.bambu-studio-connector/` (or a Cowork project folder) with `host`, `access_code`, `serial`, `model` — all shown on the printer's touchscreen.

## Components

- `server/index.js` — dependency-free MCP server: open files in Bambu Studio, search for model files, Bambu account sign-in/sync (cloud API + SSDP discovery)
- `server/printer-launcher.js` — runs [bambu-printer-mcp](https://github.com/DMontgomery40/bambu-printer-mcp) via npx for camera snapshots and live printer status
- `skills/bambu-print-prep` — workflow + `model_tools.py`: inspect/render/extract/scale/lay-flat/arrange parts from 3MF/STL, no dependencies for inspection
- `skills/bambu-print-watch` — bed-clear camera checks, status reports, scheduled monitoring; hard rule against starting prints

## Caveats

- The Bambu account sign-in and sync use Bambu's cloud API as documented by the community ([OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)); Bambu has no official public API and could change it at any time.
- Camera snapshots and status reads work in cloud mode on current firmware, but Bambu's firmware updates have repeatedly tightened third-party access — behavior varies by model and firmware.
- Plain 3MF files exported by the prep skill contain geometry only; Bambu Studio will note they're "not from Bambu Lab" and apply your current presets. Expected.
- Lay-flat uses 90° axis-aligned rotations — great for boxy parts, not a general optimal-orientation solver.
- Always check orientation and supports in Bambu Studio before slicing.

## Credits

- [DMontgomery40/bambu-printer-mcp](https://github.com/DMontgomery40/bambu-printer-mcp) — printer monitoring server (fetched via npx, not bundled)
- [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) — cloud API and SSDP protocol documentation

## License

MIT — see [LICENSE](LICENSE). Not affiliated with or endorsed by Bambu Lab.
