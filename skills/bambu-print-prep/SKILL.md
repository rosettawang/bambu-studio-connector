---
name: bambu-print-prep
description: Prepare 3D models and load them onto the Bambu Studio print bed. Use when the user wants to open or put a model (STL/3MF) on the print bed, print only a specific piece of a multi-part model ("print just the base", "only the roof"), see what parts are inside a model file, separate a model into its pieces, or find a model file on their Mac. Triggers include "put it on the print bed", "open in Bambu Studio", "print just the...", "what's in this 3mf/stl", "which piece is the...".
---

# Bambu print prep

Load 3D models onto the Bambu Studio print bed, including only the specific parts the user asks for. Geometry work (inspect/render/extract) runs in the sandbox with the bundled script; the actual app launch runs on the user's Mac via the `bambu-studio` MCP tools.

## Path rules (critical)

Two path spaces are in play:

- MCP tools (`open_in_bambu_studio`, `find_model_files`, `bambu_studio_status`) run on the Mac — pass real Mac paths (`/Users/...`).
- The bundled script runs in the sandbox via bash — pass sandbox mount paths (`/sessions/.../mnt/...`).

The user's connected folder is visible in both spaces; use the system prompt's path mapping to translate. Any file that must end up in Bambu Studio has to exist on the Mac, so write extracted parts into the connected folder (never the sandbox-only outputs scratchpad), then open them with the Mac path.

The script lives at `scripts/model_tools.py` under this skill's directory. Translate the skill directory to its sandbox mount to run it; if it isn't reachable from bash, Read it and Write a copy under outputs, then run that copy.

## Workflow

1. **Locate the model.** Check the user's connected folder first (Glob for `**/*.stl`, `**/*.3mf`). If not there, use `find_model_files` to search the Mac. If several candidates match, list them and ask.

2. **Inspect before opening** whenever the user refers to a specific piece, or the file might contain several:

   ```bash
   python3 "<skill_dir>/scripts/model_tools.py" inspect "<sandbox path to model>"
   ```

   Returns JSON: part index, name (3MF object names, including Bambu's `model_settings.config` names), bounding box in mm, triangle count, approximate volume. Multi-shell STLs are split into `-shell-N` parts automatically.

3. **Identify which part is which.** Use every signal:
   - Names from the 3MF when present — often definitive.
   - Dimensions and shape: e.g. for a Langstroth hive, the telescopic roof is the widest footprint with a shallow height; the base is wide and flat with a low profile; brood boxes are tall open rectangles; frames are thin and numerous.
   - When names are missing or ambiguous, render and look:

     ```bash
     pip list 2>/dev/null | grep -qi numpy || pip install numpy matplotlib --break-system-packages
     python3 "<skill_dir>/scripts/model_tools.py" render "<model>" --out "<outputs>/preview.png"
     ```

     Read the PNG. The first panel shows all parts placed together (colored per part); the rest show each part with its index, name, and size. Reason about the geometry visually and explain the identification to the user.
   - If still ambiguous, show the user the preview (present_files) and ask which piece they mean rather than guessing.

4. **Extract the requested parts** (skip if the user wants the whole file):

   ```bash
   python3 "<skill_dir>/scripts/model_tools.py" extract "<model>" --parts "base" \
     --out "<connected folder sandbox path>/<model-name>-base.stl"
   ```

   `--parts` accepts indices and/or name substrings, comma-separated. Options:
   - `--scale 0.2` — uniform scaling (20%)
   - `--out file.3mf` — multi-object output: each part stays a separate object, auto-arranged on the plate with `--gap` spacing within `--bed` mm (default 380). Prefer this when extracting several parts.
   - `--lay-flat` — rotate each part in 90° steps to its flattest orientation before arranging (3MF output only). Use it whenever source parts stand upright.
   - `--out file.stl` — single merged STL, recentered, bottom at z=0. Use for one part.

   Studio will note a plain 3MF is "not from Bambu Lab, geometry data only" — expected and harmless (no print settings embedded).

5. **Load onto the print bed.** Call `open_in_bambu_studio` with the Mac path(s). Notes:
   - Opening a `.3mf` project loads the whole project (Bambu Studio may ask about a new window). To put only some parts on the bed, extract to STL first — do not open the full project.
   - Opening several STLs at once may prompt "load as single object?" — tell the user to answer No to keep pieces separate.
   - Multi-shell STLs can be split inside Bambu Studio (right-click → Split to objects), but extracting only what's needed is cleaner.

6. **Confirm.** Tell the user what was loaded, which part(s) it corresponds to, the dimensions, and any dialog they need to click in Bambu Studio. Remind them to check orientation/supports before slicing — part orientation in the source file is not always print-ready.

## Caveats

- Volumes assume closed meshes; treat as rough. Sizes are axis-aligned bounding boxes in the file's placed orientation.
- The script reads `.stl` and `.3mf` only. For `.step`/`.obj`, open the file directly in Bambu Studio and do the part work there.
- If `open_in_bambu_studio` reports the app missing, ask the user to confirm Bambu Studio is installed in /Applications.
