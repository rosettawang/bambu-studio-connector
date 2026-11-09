---
name: bambu-color-blend
description: Make a color blend (gradient) on a multicolor Bambu print out of the filament-change purge itself — skip most of the purge for one color pair and print a chosen zone first, in an order that lays the fading old color down as a gradient. Produces a Bambu Studio project that keeps the blend when the user clicks "Slice plate", a sliced plate file, and an HTML dashboard that shows the predicted colors (Bambu Studio's preview can't). Use when the user asks to blend, fade or grade one filament color into another, to "use the purge as a gradient", to purge less between two similar colors, or to calibrate how fast their printer clears a color.
---

# Color blends from the purge

When a Bambu printer switches filament, the old color fades out of the nozzle over the next few tens of mm³. Bambu purges that into the chute and prime tower. This skill skips (most of) that purge for **one color pair** and prints a chosen **blend zone** first, ordered so the leftover color lands as a gradient: darkest where the zone should be darkest, clean where it should be clean. Every other color pair keeps its normal purge.

Claude preps; the user presses print. Never start a print.

Script: `scripts/blend_tools.py` (numpy; matplotlib for the dashboard). Run `python3 blend_tools.py -h`.

## When it works well

- Similar or dark pairs: black → navy, dark green → green, brown → orange. Into white or a pale color, leftovers show as gray or brown streaks — say so and suggest a drawn halftone instead.
- One blend per filament change. On a plate with several copies, only the first copy printed after the change gets the full blend unless extra changes are added (each costs time, far less filament than a full purge).
- The zone's lines should run **parallel to the clean edge** (set Process › Strength › Infill direction so the top surface lines do), otherwise each line spans the whole gradient.

## Workflow

1. **Agree the zone and the pair.** Which filament fades into which, on which layer (usually the top layer of a flat part), where it should be darkest and where clean. Paint the zone in the *second* color in the project (Bambu paint, or Claude's own paint pipeline).

2. **Prep the project** (unsliced `.3mf`):

   ```bash
   python3 blend_tools.py prep project.3mf --out project-blend.3mf \
     --layer-order 2:1,2,3,4 --no-arcs
   ```

   - `--layer-order LAYER:ORDER` (repeatable) sets that layer's filament order so the second color comes **right after** the first; otherwise another color's leftovers go into the zone. It's Bambu Studio's own per-plate setting and survives re-slicing. Two blends on one layer (A into B, then B back into A) need B first on that layer and the layer below ending on A, e.g. `--layer-order 2:2,3,4,1 --layer-order 3:2,1,3,4`.
   - `--no-arcs` turns arc fitting off; the reorder refuses to touch arc moves.
   - **Don't zero the pair's purge for the whole print** (`--zero-flush`). The same color change usually happens elsewhere (a body that switches colors every layer) and those spots get contaminated. Put `"purge": 0` in the spec instead: it trims the purge of the blended change only.

3. **Write the zone spec** (JSON, plate millimetres, filaments numbered as in Bambu Studio):

   ```json
   {"title": "Forewing black-to-blue blend", "layer_z": 0.32, "from": 1, "to": 2,
    "zones": [{"polygon": [[90,128],[125,128],[125,150],[90,150]], "group": "left"},
              {"polygon": [[131,128],[166,128],[166,150],[131,150]], "group": "right"}],
    "clean_edge": [[80,128],[176,128]], "bin": 0.5, "purge": 0}
   ```

   Several blends: `{"blends": [{...}, {...}]}` — each item takes the same keys; top-level keys are defaults. `"split_mm": 1.5` cuts lines into pieces so a zone whose lines run *across* the gradient is ordered piece by piece (costs some start/stop texture; prefer lines parallel to the clean edge). With a `key_npz`, `"key_name"`/`"group_name"` pick arrays from one file.

   Lines farthest from `clean_edge` (or `clean_point`) print first. Groups alternate inside each band so mirrored parts get the same shade. For an irregular field, pass `"key_npz"` instead: arrays `key` (higher = cleaner, NaN outside), optional `group`, and `x0`, `y0`, `px_mm` placing pixel [0,0] on the plate. The zone is pinned to the plate: if the model moves, rebuild the spec.

4. **Install the Bambu Studio hook** so the user can slice it themselves: in the project's `Metadata/project_settings.config` set `post_process` to

   ```
   /usr/bin/python3 "<skill_dir>/scripts/blend_tools.py" hook --spec "<path to spec.json>"
   ```

   and add `post_process` to `different_settings_to_system[0]`. Use a Python that has numpy (`/usr/bin/python3` on macOS does). Bambu Studio shows a security warning on open because the project runs a script; tell the user to allow it. The hook refuses to write if a safety check fails, and leaves the G-code untouched.

5. **Slice once yourself** (Bambu Studio CLI — it does *not* run post-processing scripts, so run `blend` explicitly) and build the dashboard:

   ```bash
   python3 blend_tools.py blend sliced.gcode.3mf --spec spec.json \
     --out "<name> (blend).gcode.3mf" --report "<name> blend dashboard.html"
   ```

   Exit code 2 means a safety check failed — don't hand that file over. Checks: filament total unchanged, no stray extrusion from the chute or across the plate.

6. **Show the dashboard, open the project** with `open_in_bambu_studio`. Tell the user, in these words or close:
   - Bambu Studio's preview draws the zone in the plain second color; **the dashboard is the preview of the blend.**
   - Click **Slice plate**, then **Print plate**; the script runs on every slice.
   - Don't move or rescale the model — ask Claude to rebuild instead.
   - A `.gcode.3mf` opened in Bambu Studio asks to be re-sliced, which regenerates the G-code; without the hook that throws the blend away. The sliced file can also be printed from the printer's SD card.

7. **Calibrate after the first print.** The prediction assumes a clear-out rate (fast / typical / slow are shown until calibrated). Ask which panel the print matches, then:

   ```bash
   python3 blend_tools.py calibrate --plate sliced.gcode.3mf --from 1 --to 2 --half-life 41
   ```

   Saved to `~/.bambu-studio-connector/blend-calibration.json` per printer profile and color pair; later dashboards lead with it. For a cleaner measurement, `make-strip` writes an 80 × 12 mm, two-layer strip (plain 3MF). In Bambu Studio, right-click it › Add height range modifier › from the first layer height to the top › the second filament (Bambu ignores paint in plain 3MF files, and a file that claims to be a Bambu project without the rest of one crashes the slicer). Then prep, slice, blend with `clean_point` at the far Y end, and compare the photo with the dashboard.

## Check these before blending — each one ruined or nearly ruined a real print

- **Inner-layer paint conflicts go to the lowest filament number.** On parts three or more layers thick, where top paint and underside paint both reach an inner layer, Bambu uses the lower-numbered filament. White top marks over orange underside spots came out pink because orange was filament 3 and white 4. Number the colors that must look clean (white, pale colors) lower than strong underside colors. Bambu matches filaments to AMS slots by color at print time, so the user doesn't move spools.
- **A thin top layer shows what's under it.** A 0.12 mm white or pale layer over orange reads pink. Add a layer so the top color sits on itself.
- **Purge tables in downloaded projects are often stale.** They were set for the author's colors. Run `inspect` and look at each change's `purge_mm3`, especially into white (a sparkly blue into white needed about 600 mm³, and the downloaded table had 159).
- **Size the zone to the clear-out rate.** Black into a dark blue cleared about half every 10 mm³ on an A1, so most of the change is over within about 30–40 mm³. A zone much bigger than 3–4× the rate prints mostly clean.
- Overlapping parts on the first layer (a body over a wing plate) also resolve to the lower filament number. To paint a dot on an overlap, paint it on both parts.

## What the reorder does (so you can explain it)

- Finds the change from the first to the second filament on the layer, and trims that change's purge if the spec says so (other changes keep theirs).
- Moves the prime tower that Bambu prints right after the change to *after* the blend zone (otherwise the tower eats the leftover color), restoring the nozzle height and retraction state the tower expects.
- Prints the zone's lines darkest-band first, alternating groups, then everything else in the slicer's order, re-travelling between pieces.
- Keeps wipes' retraction, tags the moved sections so Bambu's preview and time estimate stay right.
