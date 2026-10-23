# DC2 Upscale Manager

A local web app for building an HD background mod for the GOG release of **Dino Crisis 2** (PC).

Every background in Dino Crisis 2 is a pre-rendered 320x240 image. That covers the title and menu screens and all
1,169 room camera views. On a modern screen they look very soft. GOG's release ships a `ddraw.dll` wrapper that can
load replacement textures, and this tool uses it in four steps:

1. **Extract.** It pulls every background out of the game (read-only) and works out the file name the wrapper will
   look for when that image is drawn.
2. **Upscale.** It runs batches through [Upscayl](https://upscayl.org)'s AI models (4x, so 1280x960), headless.
3. **Review.** You compare each result against the original with a drag slider, then approve it or reject it and
   retry with another model.
4. **Install.** It writes the approved art into the game as replacement files. For rooms it also writes matching HD
   masks, the pieces drawn over characters who walk behind objects. Install is strictly additive and fully
   uninstallable.

Everything runs on your own PC. The web UI only listens on `127.0.0.1`, and nothing goes online.

> **No game files are included.** You need your own copy of Dino Crisis 2 (GOG). This is a fan project, not
> affiliated with or endorsed by Capcom or GOG.

## Requirements

| What | Details |
|---|---|
| **Windows** | Uses Windows file dialogs, NTFS hard links and Upscayl's Windows engine. |
| **Dino Crisis 2, GOG English build** | All replacement keys were derived from this build. The app checks `Dino2.exe`'s SHA-1. On any other build it warns you: extraction still works, but the keys may not match. |
| **32-bit colour mode in game** | The GOG wrapper only applies replacements in the HAL / Direct3D modes; Software Render ignores them. 16-bit colour mode is supported if you tick the option on the Install tab. |
| **Python 3** with **Flask**, **numpy**, **Pillow** | Developed and tested on Python 3.14 (Flask 3.1.3, numpy 2.4.2, Pillow 12.1.1). The *Browse* buttons need `tkinter`, which the python.org installer includes. |
| **[Upscayl](https://upscayl.org)** (desktop app) | Provides `upscayl-bin.exe` and the AI models. It's found automatically in `Program Files\Upscayl` or `%LOCALAPPDATA%\Programs\Upscayl`. Needs a GPU with Vulkan support. Check each model's licence; some (e.g. Ultrasharp) are non-commercial. |
| **Sysinternals [Process Monitor](https://learn.microsoft.com/sysinternals/downloads/procmon)** (once) | For the one-time [in-game key check](docs/KEY_CHECK_PROCEDURE.md) that unlocks installing room backgrounds. Not needed for menu and title images. |

Install the Python packages:

```powershell
python -m pip install -r requirements.txt
```

Packages are installed per Python version. If you add a newer Python and it becomes the `python` on your PATH, install
the packages again for it, or the app stops with `No module named 'numpy'`.

## Getting started

1. Double-click **`start.bat`**, or run `python app.py [--port 8765] [--no-browser]`. Your browser opens at
   `http://127.0.0.1:8765/`. Close the console window to stop the app.
   - Use plain `python`, not `python -I`, if your packages were installed with `pip --user`, because isolated mode
     hides per-user packages.
2. On the **Setup** tab:
   1. Pick the game folder, a workspace folder (anywhere outside `C:\GOG Games`), the install target (normally the
      game folder itself) and `upscayl-bin.exe`.
   2. Click **Save settings**, then **Run self-check**.
   3. Click **Extract / refresh**. It takes about 30 s.
3. On the **Library** tab, select images, pick a model and click **Upscale**.
4. On the **Review** tab, approve or reject the results.
5. On the **Install** tab, click **Install / update game**. Room backgrounds also need the one-time
   [key check](docs/KEY_CHECK_PROCEDURE.md) first.

Settings are saved in `config.json` next to `app.py`, which git ignores. The workspace holds all extracted and
upscaled images plus the catalog. It defaults to `C:\Claude\DinoCrisis2ModWork\upscale_ws`; change it on the Setup tab.

## Using the app

### Setup tab
- Choose the game folder, workspace, install target and `upscayl-bin.exe` (the Browse buttons open normal Windows
  dialogs), then **Save settings**.
- **Run self-check** tests Upscayl.
- **Extract / refresh** reads every background from the game into the workspace.
- **Disk usage:** size and file count of every workspace area. Run input folders are hard links to the originals,
  so they show 0 bytes.
- **Clean up selected:** each category first shows how many files and MB it would free:
  - rejected upscales kept on disk
  - run copies of approved upscales (`approved\` keeps its own copy)
  - input links of finished runs
  - the review preview cache
  - install staging
- **Clean-up never touches** originals, images waiting for review, approved images, install backups, or anything
  belonging to a queued or running job. Clean-up is refused while a job runs.

### Library tab
- A thumbnail grid of all 1,192 images.
- Filters: menus / rooms, stage, status, models already tried, and a search box. Duplicates and blank placeholders
  are hidden unless ticked.
- Click to select, Shift+click to select a range, or use "Select all shown" / "Select 'to do' shown".
- Pick a model, scale, workers and (optionally) a GPU, then click **Upscale N images**.
- Only "to do" items are sent; anything else is skipped and reported.
- **Reopen N approved** appears when the selection contains approved images. It sends them all back to "to do" and
  keeps them selected, so you can pick another model and click Upscale straight away.
- Badges per image:
  - status (to do / queued / upscaling / in review / approved)
  - replacement key: **✓ key** confirmed in game, **key** computed exactly, **~ key** predicted (rooms)
  - models already tried
- **Double-click an approved or in-review image** to open a full-size comparison popup:
  - **Replace with edited file…** swaps the approved image for a hand-fixed version (it must keep the 4:3 shape and
    be at least 320x240).
  - **Reopen** sends it back to "to do".

### Bottom bar and Runs tab
- **Bottom bar:** shows the running job (images done, failures, current %, elapsed, ETA) with **Cancel**. You can
  queue more runs while one is running; they run one after another. A toast pops up when a run or extraction
  finishes.
- **Runs tab:** history with state, counts and duration, plus a log for failed runs.
  - **Cancel** removes a run that's still waiting.
  - **Resume** re-queues the leftovers of an interrupted or cancelled run.
- **If the app was closed mid-run:** on the next start, finished images are kept and a toast points to Resume.

### Review tab
Everything "in review", in one of two modes:
- **One by one:**
  - **The comparison view:** original (left) vs upscaled (right) with a draggable divider. The mouse wheel zooms
    around the cursor, dragging pans when zoomed, and double-click fits. The Fit, 1:1 and −/+ buttons do the same.
  - **Keys:** <kbd>A</kbd> approve, <kbd>R</kbd> reject, <kbd>←</kbd>/<kbd>→</kbd> previous/next, <kbd>Space</kbd> flip
    between full original and full upscaled, <kbd>P</kbd> pixelated/smooth original.
  - **Side panel:** model, run, key and models tried, plus a thumbnail strip of everything waiting.
- **Grid:** hover a card to compare (the mouse position is the divider). Click to select, then **Approve selected** /
  **Reject selected**. Double-click opens the card in one-by-one mode.
- **Approve** copies the upscaled image to `approved\` and marks the item approved ("done").
- **Reject** sends the item back to "to do", tagged with the model, so you can pick another one. The upscaled file is
  deleted unless you untick "delete rejected files".
- **When a run finishes:** a banner with **Review now**, a desktop notification (the browser asks permission the
  first time you click Upscale) and a chime. Both can be switched off in the Review toolbar.
- The current tab is kept in the address (e.g. `#review`), so a refresh stays put.

### Install tab
**Install / update game** syncs the install target with your approved images.
- **Where:** the game folder itself (`C:\GOG Games\Dino Crisis 2`, the default) or a separate copy of it. A folder
  inside the game, or any other folder under `C:\GOG Games`, is refused.
- **What it writes:** for every approved image with a trusted key, `Data\<key32>.dat` for the game's normal 32-bit
  colour mode. Rooms also get their HD mask file.
- **16-bit colour mode** needs a second file per image (`<key16>.dat`). The tickbox *also install files for 16-bit
  colour mode* (off by default) adds them. If you untick it and click Install again, they're removed (they make up
  about 40% of the install). The tab shows the colour depth the game is set to (from `dino2.cfg`) and warns if it's
  16-bit while the box is off.
- **How each file is built:** the approved PNG is resized with Lanczos to a power-of-two canvas (1280x960 →
  2048x1024), then encrypted with GOG's cipher. The result is byte-identical to the title file proven in game.
- **The record:** everything installed is listed in `dc2_upscaler_installed.json` in the target folder (next to
  `Dino2.exe`). The game ignores it. The record travels with the folder, and extraction skips the listed files in
  `Data\`.
- **Strictly additive:** it never changes the game's own files and never overwrites a file it didn't create (those
  are left untouched and reported). GOG's fix files `9A8B79FC.DAT` / `C49FC969.DAT` are never touched.
- **It's a sync:** files for images you reopened are removed again.
- **Uninstall everything** removes exactly the recorded files and then the record, leaving the folder as GOG
  installed it. A file you changed by hand afterwards is left in place (and stays listed).
- **After a GOG update** that removes the files, just click Install again.
- **Older versions** kept the record in `<workspace>\install_manifest.json`. It is moved automatically into the folder
  it describes, so that copy can still be cleaned up by pointing the target at it and clicking Uninstall.

### Confirming room keys in game
Room keys are *predicted* until checked, and Install refuses rooms until then.
- **Recording:** follow [docs/KEY_CHECK_PROCEDURE.md](docs/KEY_CHECK_PROCEDURE.md): record about 15 minutes of play
  with Process Monitor and save the log as CSV.
- **Importing:** **Import Process Monitor log (CSV)…** on the Install tab shows which keys the game really looked
  up, which camera each belonged to, and a verdict.
- **Apply** marks what was seen as *seen in game*. If one prediction is consistently right (≥ 5 discriminating
  rooms, no contradictions), it marks every room *confirmed*. If the alternative prediction won, all rooms switch to
  the alternative keys.
- **Command line:** `python keycheck_cli.py <log.csv> [--apply]`.
- **Persistence:** the outcome is stored in the workspace catalog, so re-extraction keeps it. A new workspace needs
  the log imported again (the same CSV works).
- **Reference result:** on the GOG English build (2026-10-13), 107 rooms were seen and the primary prediction was
  right for all of them.

### HD masks
When a room is installed, Install also writes its HD mask, with no extra steps.
- **What masks are:** the pieces the game draws over characters who walk behind objects (crates, fences, plants).
- **How each one is built:**
  1. The game's 512x512 mask texture is rebuilt at 4x (2048x2048, transparent PNG).
  2. Every piece is re-cut from your approved HD image at its screen position, so it matches the new art exactly.
  3. Each piece's transparency is scaled up with soft edges.
- **The key** is computed exactly (CRC of the game's A8R8G8B8 mask texture). It was verified against the in-game log
  for all 84 tiled cameras visited.
- **Duplicate cameras** (same background, different pieces, e.g. ST105 cams 6/9) get their own mask, cut from the
  shared approved image.
- **Fallback:** 0.15% of pieces (33 cameras) don't match their background in the original game. For those, an
  enlarged copy of the original piece is used, and Install reports which rooms.
- **Preview a room's mask before installing:** `python tools/mask_preview.py "ST101.DBS#0"` writes a check image and
  the atlas to `<workspace>\mask_previews\`.

## Command line
Everything the web UI does can also be run from the project folder. These tools take `--workspace <folder>`
(default `C:\Claude\DinoCrisis2ModWork\upscale_ws`); they don't read `config.json`.

### Extract
```powershell
python extract_cli.py --game "C:\GOG Games\Dino Crisis 2"
python extract_cli.py --status                                         # counts per kind/status
```
- The game folder is **only read**. The tool refuses to write anywhere inside it, or under `C:\GOG Games`.
- Re-running is safe and fast: unchanged images are skipped, and every item keeps its workflow state.
- If a source image ever changes, its item is reset to `todo`.

Result on the GOG English build: 23 full-screen images plus 1,169 room views (89 `.DBS` files), in about 30 s.
- **Duplicates:** 13 identical images are linked with `dup_of`, so you only upscale them once:
  - the four F/G/I/S warning screens
  - `CAPLOGO.PXL` and `CAPLOGO.DAT`
  - 8 room views
- **Blank placeholders:** 4 (`ST001` cam 0, `ST80A` cams 0–1, `ST905` cam 0). They're flagged so the library can hide
  them.

### Upscale
```powershell
python run_cli.py check                                         # find upscayl-bin, verify its flags, list models
python run_cli.py run --model ultrasharp-4x --items "TITLE.DAT#5" "ST100.DBS#0"
python run_cli.py run --model remacri-4x --kind room --stage ST100 --limit 10
python run_cli.py run --model ultrasharp-4x --kind fullscreen   # all todo full-screen images
python run_cli.py runs                                          # list runs
python run_cli.py resume <run_id>                               # leftovers of an interrupted/cancelled run
```
- **Headless.** Runs Upscayl's own engine (`upscayl-bin.exe`, auto-detected and remembered) with no window.
- **What gets picked.** Only `todo` items. Duplicates and blank placeholders are skipped.
- **Where results go.** Results land in `runs\<id>\out\`. Each one is checked (it must open and be exactly 4x the
  size), recorded as a candidate, and its item moves to `review`.
- **Failures and cancel.**
  - Failed images go back to `todo` with a note.
  - Ctrl+C cancels: finished images are kept, the rest return to `todo`.
- **Crash recovery.** If the program dies mid-run, the next start marks the run `interrupted`, keeps finished images,
  and tells you to `resume`.
- **Speed:** about 1.4 s per image with `ultrasharp-4x` on an RTX 3050 Laptop GPU. All 1,159 rooms take roughly
  25–30 minutes.
- **Parallelism.** By default one process per GPU is fed the whole folder. `--workers 2` splits the batch over two
  processes, and `--gpu` / `--threads` pass straight through to upscayl-bin.

## How it works

### Workspace layout
```
<workspace>\
  catalog.sqlite                 items / runs / candidates / settings
  originals\fullscreen\*.png     320x240, exactly as the game draws them (5-bit value << 3)
  originals\rooms\*.png          320x240, room JPEG decoded at full 8-bit precision (Pillow)
  thumbs\{fullscreen,rooms}\*.jpg  160x120 previews
  runs\<id>\in\w<k>\             inputs per worker process (hard links to the originals, no extra disk)
  runs\<id>\out\                 upscaled candidates
  runs\<id>\items.json           file name -> item id
  approved\                      approved HD images
```
Note: room originals can be up to ~3% brighter than in game, because the game shows them at 15-bit colour.

### Replacement keys (`key32` / `key16` per item)
The GOG wrapper names each replacement file after a CRC32 of the texture the game uploads. This tool computes or
predicts that key for every image.

| key_status | Meaning | Items |
|---|---|---|
| `verified` | Seen working in the game | TITLE (`ED666D62`), plus anything seen in an imported key-check log |
| `computed` | Exact offline formula, cross-checked against Rex-HD's file names | the other full-screen images; all rooms after a successful key check |
| `predicted` | From the decoder model in `core\data\room_keys.json`. `key32_alt`/`key16_alt` hold the second FPU-precision guess. | all rooms in a new workspace (install stays blocked until the key check) |

`core\data\room_keys.json` ships prebuilt. It was generated by `tools\build_room_keys.py` from reverse-engineering
research outputs that aren't part of this repository.

### Known limitation
29 cameras use a moving overlay sprite instead of a mask piece: boats in ST000/ST004/ST005, a fence in ST101 cam 3
and a deck in ST205. Their backgrounds install normally, but that sprite stays at original resolution for now.

## Project layout
```
app.py / start.bat    web UI (Flask): setup, library, runs, review, install
static\               plain HTML/CSS/JS, no build step: app.js (library/runs/setup), review.js (review tab,
                      notifications, detail popup), install.js (install tab), maint.js (disk usage), compare.js (drag-slider viewer)
core\review.py        approve / reject / reopen / replace-with-edited-file, candidate previews
core\install.py       install / sync / uninstall of approved images as encrypted .dat files (manifest, backups)
core\masks.py         room mask data, exact mask keys, HD (4x) mask atlases
core\keycheck.py      in-game key check from a Process Monitor CSV
core\maintenance.py   workspace disk usage + safe clean-up
core\jobs.py          settings (config.json) + background job queue (extract / run)
core\runner.py        upscayl-bin discovery/self-check, run prepare/execute/cancel, crash recovery, resume
core\paths.py         install validation (GOG English exe SHA-1) + write guards
core\catalog.py       SQLite catalog and status transitions
core\extract.py       full-screen + room extraction
core\dc2\             Dino Crisis 2 format tools: dc2codec (LZSS chunk codec), dc2pack (DAT/DBS/TEX containers),
                      gogtex (GOG wrapper cipher + CRC keys)
core\data\            room key table
extract_cli.py        extraction command line
run_cli.py            upscale command line
keycheck_cli.py       key check command line
tools\                maintenance scripts (mask preview, room key table builder)
docs\                 in-game key check procedure
tests\                unit and integration tests
```

## Tests
```powershell
python -m unittest discover -s tests -v
```
80 tests, about 55 s with everything available. `tests\fake_upscayl.py` stands in for Upscayl in most of them. Tests
that need the real game install, a real Upscayl install or the original research data are skipped automatically when
those aren't present.
