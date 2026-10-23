# In-game key check

Goal: confirm the replacement file names ("keys") the game really uses for room backgrounds, so room
replacements can be installed with confidence. Menu images are already exact; this check confirms them too.

A fresh workspace starts with every room key marked *predicted*, and Install refuses rooms until this check has
been imported once. Menu and title images install without it.

**Time needed:** about 15 minutes.
**What you need:**
- the install target (normally the game folder itself, e.g. `C:\GOG Games\Dino Crisis 2`)
- a save that starts somewhere you can walk through several rooms
- Sysinternals Process Monitor

**Why it works:** each time a background appears, GOG's `ddraw.dll` computes the image's key and tries to
open `Data\<key>.dat`. Process Monitor records those attempts, so we read the real names straight from the game.
It also records which camera of which stage was being loaded at that moment, so the tool can tell whether a
prediction was right or wrong.

---

## 1. Get Process Monitor (one time)
1. Download **Process Monitor** from Microsoft Sysinternals:
   https://learn.microsoft.com/sysinternals/downloads/procmon
2. Unzip it, e.g. to `C:\Tools\ProcessMonitor\`.
3. Run `Procmon64.exe`. It asks for administrator rights; that's normal, because it watches file activity.
4. Accept the licence.

## 2. Set the filter
Process Monitor starts capturing everything immediately. First press **Ctrl+E** to pause it, then **Ctrl+X** to
clear what it caught.

Open **Filter → Filter…** (Ctrl+L). Add these four rules, clicking **Add** after each:

| Column | Relation | Value | Action |
|---|---|---|---|
| Process Name | is | `Dino2.exe` | Include |
| Path | contains | `\Data\` | Include |
| Operation | is | `CreateFile` | Include |
| Operation | is | `ReadFile` | Include |

Click **OK**.

> **Do not** filter on *Result*. The tool needs the successful reads of the `ST*.DBS` files as well as the
> `NAME NOT FOUND` lookups.

Recommended: **Filter → Drop Filtered Events**, so Process Monitor doesn't use memory for everything else on your
PC.

## 3. Check the game settings (once)
In the game's options, keep the **32-bit colour (HAL / Direct3D)** mode you normally use (the setting is stored in
`dino2.cfg`). In Software Render mode no replacement is used at all.

## 4. Record
1. In Process Monitor, press **Ctrl+E** to start capturing. The magnifier icon loses its red X.
2. Start `Dino2.exe` from the install target. If you install into a separate copy of the game, start it from that
   copy, not from the GOG launcher.
3. Wait for the title screen.
4. Load a save and walk through at least **10 different camera angles**, ideally across 3 or more rooms. More is
   better: the tool needs **5 rooms where the two predictions differ**, and roughly 3 in 4 rooms qualify.
   - Doors and stairs count; each new room and each camera cut is a new background.
   - Revisiting the same angle doesn't add evidence.
5. Quit the game normally.
6. In Process Monitor, press **Ctrl+E** to stop capturing.

You should now see rows like these:

```
Dino2.exe  ReadFile    C:\GOG Games\Dino Crisis 2\Data\ST100.DBS       SUCCESS          Offset: 69.632, Length: ...
Dino2.exe  CreateFile  C:\GOG Games\Dino Crisis 2\Data\1A2B3C4D.dat    NAME NOT FOUND
```

If there are **no** `XXXXXXXX.dat` lines, check that the game was started from the install target and that the
filter matches the table above.

## 5. Save the log as CSV
1. **File → Save…**
2. Choose **Events displayed using current filter**.
3. Choose the format **Comma-Separated Values (CSV)**.
4. Save it outside the game folder, e.g. `Documents\dc2_keycheck.csv`.

## 6. Import it
**In the app:** open the **Install** tab, then **Confirm replacement keys in game → Import Process Monitor log
(CSV)…**, and read the result.

**Or from the command line** (from the project folder; add `--workspace <folder>` if yours isn't the default):
```powershell
python keycheck_cli.py path\to\dc2_keycheck.csv            # analyse only
python keycheck_cli.py path\to\dc2_keycheck.csv --apply    # record the result
```

## 7. What the results mean
| Result | Meaning | What happens on **Apply** |
|---|---|---|
| **Confirmed (primary)** | ≥ 5 rooms matched the main prediction, none the alternative, no contradictions | Seen rooms become *seen in game*; **all other rooms become confirmed**. |
| **Confirmed (alternative)** | Same, but for the alternative prediction | Every room switches to its alternative key, then as above. |
| **Not enough evidence** | Fewer than 5 rooms where the predictions differ were seen | Only the images actually seen are marked; those marks add up across imports. To confirm *all* rooms, one imported log must on its own contain at least 5 such rooms, so record a longer session. |
| **Inconsistent / contradicted** | Some rooms match neither prediction, or both models appear | Only the images seen are trusted. The rest would need a fallback (a small logging hook that records each camera's real key while you play), which isn't built yet. |

Menu images seen during the session (title, Capcom logo, warnings…) are marked *seen in game* as well.

Re-running the extraction later keeps the outcome: it's stored in the workspace catalog. A new workspace needs the
log imported again (the same CSV works).

## 8. After a successful check
**Reference result (GOG English build, 2026-10-13):** 107 rooms were seen in game, 86 of them where the two
predictions differ, all on the primary prediction, so all room keys were confirmed.

Approved rooms then install like menu images. Install also generates each room's **HD mask** automatically. The mask
keys were reproduced exactly from that same log: 84 of 84 tiled cameras matched.
