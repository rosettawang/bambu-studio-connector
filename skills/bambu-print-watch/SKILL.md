---
name: bambu-print-watch
description: Check the Bambu printer with its camera and status feed. Use when the user asks whether the print bed is clear, wants a photo of the printer or bed, asks how a print is going (progress, time remaining, temperatures), wants to be told when it's safe to start a print, or wants scheduled printer checks. Triggers include "is the bed clear", "check the printer", "how's the print going", "print status", "watch the print", "schedule a check", "can I print now".
---

# Bambu print watch

Monitor the printer via the `bambu-printer` MCP tools (camera snapshot + live status over the local network). The user starts prints themself in Bambu Studio — this skill's job is everything up to and after that click.

## Hard rules

- **Never start, queue, or resume a print, and never heat the nozzle/bed, unless the user explicitly asks in this conversation.** The agreed workflow is: Claude checks and reports; the user presses "Print plate" in Bambu Studio. Do not call print-start or upload tools even if available.
- Bed-clear judgments are advisory. State what the photo shows and let the user decide; if the image is dark, blurry, or partially blocked, say so rather than guessing.

## Configuration

Printer settings live in `printer-config.json` (in the user's connected folder or `~/.bambu-studio-connector/`). **Preferred setup — Bambu account sign-in** (tools on the `bambu-studio` server):

1. Ask for the user's Bambu account email → call `bambu_account_request_code`.
2. User reads the code from their email → call `bambu_account_login` with it. This stores a token locally (~3 months), fills in serial + access code from the account, and discovers the printer's IP on the network automatically. Never ask for their password unless code login fails.
3. If tools later fail with auth errors (access code changed): call `bambu_account_sync` — it re-fetches the code and IP. Then the user disables/re-enables the plugin so the printer server restarts with new settings.
4. `bambu_cloud_printer_status` gives basic online/task status via the cloud — useful when the Mac isn't on the printer's network.

Manual fallback: ask the user for IP, access code, and serial (printer touchscreen: Settings > WLAN / Device) and write them into the config file.

The printer stays in normal cloud mode (no Developer Mode). Status reads and camera snapshots work there; control commands (pause/resume/stop, temperatures, uploads) may be rejected by recent firmware with auth errors (e.g. 84033543). If that happens, explain the Developer Mode limitation once — do not retry in a loop.

## Notifying the user (phone push)

Use the `send_push` tool (on the `bambu-studio` server) to reach the user on their phone via ntfy when they need to act — clear the bed, press Print for a scheduled print, or a print finished/failed. Requires `ntfy_topic` in printer-config.json and the ntfy app subscribed to it; if the tool reports no topic set, tell the user to add one and subscribe.

Guidance:
- Only push when action is genuinely needed or a result is worth knowing. Silence is fine — don't push "still printing" every check.
- Keep it short and specific with a title and next step, e.g. title "Chungus: bed not clear", message "Hive base still on the plate — clear it, then press Print in Bambu Studio."
- Use `priority: "high"` for action-needed alerts, default for FYI (print finished).
- This is the primary alert channel for scheduled tasks, since the user isn't watching chat when those run.

## Bed check ("is the bed clear?")

1. Optionally call `set_light` to turn on the work light if a previous snapshot came back dark (this may fail without Developer Mode — fall back to asking the user to check lighting).
2. Call `camera_snapshot` with `save_path` set to a Mac path inside the connected folder, e.g. `<connected folder>/camera/bed-<timestamp>.jpg`. A1/P1 printers use the TCP-6000 path (no ffmpeg needed); X1/H2 need ffmpeg installed.
3. Read the saved JPEG and assess: finished print or purged filament left on the plate, stray debris, plate seated correctly, toolhead position blocking the view.
4. Share the photo with the user (present_files) with a one-line verdict: clear / not clear / can't tell, and why.
5. If clear and a sliced plate is ready: tell the user it's safe to press "Print plate" in Bambu Studio. If not clear: describe what needs removing.

## Print status

Call `get_printer_status` and summarize conversationally: state, progress %, current layer, time remaining, nozzle/bed temperature. For "watch the print" requests, pair a snapshot with the status so the user sees both numbers and reality — the camera catches failures (detached prints, spaghetti) that status numbers miss.

## Scheduled checks

Offer Cowork scheduled tasks for anything recurring. Useful patterns:

- Evening readiness: daily snapshot + bed verdict + "ready for you to press print" message.
- Print watch: while a long print runs, check every 30-60 min; alert only if the snapshot looks wrong or status shows an error/stall.
- Morning report: status + photo of the finished overnight print.

When creating the task, write the prompt so it uses this skill and preserves the never-start-prints rule.

## Related

For finding models, identifying parts, and loading the bed, use the `bambu-print-prep` skill. A typical full flow: prep parts → user slices in Studio → bed check here → user presses print → scheduled watch → morning report.
