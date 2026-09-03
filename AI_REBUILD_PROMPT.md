# Rebuild prompt for dbImageViewer / SynTemp Vision Data Viewer

Paste everything below into an AI coding assistant (Claude Code, etc.) with
access to this repo to rebuild this tool from scratch if the source is ever
lost. It captures every deliberate design decision made while building it -
follow them exactly, don't "improve" on them without asking first.

---

## What to build

A Windows desktop GUI tool called **dbImageViewer.py** (window title/branding:
"SynTemp Vision Data Viewer") that connects directly via `pymysql` (no SSH,
no mysqldump) to a **shared server database** where many cameras' rows are
all populated into the same tables (`system_setup`, `scene_setup`,
`image_data`), distinguished by `system_id` and `scene_id`. This is
different from the older per-camera tool (`scansceneAnaylze/scandb_dump_gui.py`
/ `ScanDBDumpTool.exe`), which SSHes into one camera's own local `scandb`.

Purpose: pick a camera (system), pick a scene on that camera, pick a time
range, pull the thermal image blobs for that scene from `image_data`, decode
and render them to PNGs, and save them to disk - reusing the **existing,
already-tested** decode/render pipeline in `scansceneAnaylze/view_blob.py`
rather than reimplementing it.

## Hard rules - do not deviate

1. **Read-only.** Never write to the database. Only `SELECT`.
2. **Reuse `view_blob.py`, don't duplicate it.** Import
   `process_blob_bytes` and `write_summary_csv` from
   `scansceneAnaylze/view_blob.py` (a sibling folder) via a `sys.path.insert`
   pointing at `Path(__file__).resolve().parent.parent / "scansceneAnaylze"`.
   Do not copy/reimplement PGM decoding, temperature conversion, or PNG
   rendering logic here.
3. **Output format for what's *inside* a label folder must never change:**
   ```
   <out_root>/<label>/<timestamp>/img1_viewable.png
   <out_root>/<label>/<timestamp>/img1_color.png
   <out_root>/<label>/<timestamp>/img1_color_200_600.png
   <out_root>/<label>/summary.csv
   ```
   No raw blob dump, no extra subfolders, no additional CSVs
   (a `scene_data.csv` was tried during development and explicitly removed).
   A coworker's separate downstream program consumes this exact structure -
   confirmed by matching it against a real working example at
   `scansceneAnaylze/output/central_1009/`.
4. **The outer `<label>` folder name** is `<db_server_ip>_<scene_id>_<n>`,
   where `<n>` is an auto-incrementing iteration number
   (`_1`, `_2`, `_3`, ...) - the first one not already used as a folder or a
   `.zip` under the chosen output directory. This is deliberate: every
   download gets its own never-reused folder so two downloads for the same
   system/scene can never collide or overwrite each other. (An earlier
   "merge into the existing folder" approach was tried and reverted - too
   complex, and it had a real bug: `Path.with_suffix(".zip")` mangles an
   IP-address-containing name because `with_suffix()` treats everything
   after the *last* dot as an extension. Don't reintroduce that.)
5. **Don't over-engineer.** No shared module refactor between this tool and
   `scansceneAnaylze/server_scan_dump_gui.py` (an earlier, untouched copy)
   even though they duplicate some code - keep them as independent plain
   files.
6. **Never query `image_data` directly by `scene_id`/`captured_dt`.** See
   "Query performance" section below - this is not a style preference, it's
   the difference between a 2-3 second query and one that hangs for
   10-25+ minutes on real production data. Route through `scene_data` first.

## Real schema (confirmed against a live practice DB)

- `system_setup` - one row per camera: `system_id` (zero-padded 6-digit
  int), `camera_name`, `loc_desc`, `camera_ip`, `server_ip`,
  `last_scene_id` (NULL if that system has never captured a scene).
- `scene_setup` - has `system_id`, `scene_id`, `scene_name`, `active`.
- **`system_id` and `scene_id` are independent numbering - never assume
  they line up** (confirmed: system_id 001020's `last_scene_id` was 1050).
- `image_data` - has `scene_id` but **no `system_id` column** - filter by
  `scene_id` alone once a system's scene is picked. Columns used:
  `image_data_id`, `image_id`, `captured_dt`, `img1` (BLOB, queried as
  `HEX(img1)` then decoded with `bytes.fromhex`).
  - Also has an `image_dt` column (NOT NULL, auto-updates) vs `captured_dt`
    (nullable). This tool filters/sorts on `captured_dt` - that's fine as
    long as real-world testing keeps showing it's populated; if rows ever
    show up missing from a time-range query that should have them, check
    for NULL `captured_dt` before assuming a bug.
  - **A camera's currently-active scene may not be the one you'd expect** -
    `system_setup.last_scene_id` is the source of truth for "what scene is
    this camera writing to right now", not whatever scene looks newest in
    a picker. Cross-check `GROUP BY scene_id` counts/`MAX(captured_dt)` in
    `image_data` against `last_scene_id` if a scene appears to have gone
    quiet.
- `scene_data` exists (`scene_data_id`, `scene_id`, `start_dt`/`end_dt`,
  `alarm`, `status`, no blobs) - **is queried by this tool as of the
  performance fix below**, but only to resolve which `scene_data_id`
  values are relevant; its columns are never written to the output
  (`summary.csv`/filenames are unaffected, keeping the output format
  unchanged as originally required).

## Query performance: route through `scene_data`, not `image_data` directly

**Confirmed on real production data 2026-09-03** (SynTemp-LB002,
`scandb`): `image_data` has ~1.26 million rows and its primary key is the
*composite* `(image_data_id, scene_id)` - since `scene_id` is only the
**second** column, BTREE indexes can only be used efficiently via their
left-most prefix, so a query filtering `WHERE scene_id = ... AND
captured_dt >= ...` gets **zero** benefit from that index and forces a
full table scan + filesort (`SHOW PROCESSLIST` shows this as `Creating
sort index`, sometimes running 10-25+ minutes or effectively hanging).
`captured_dt` has no index anywhere.

The fix: `scene_data` (a separate, similarly large table - don't assume
it's small) **does** have a real index on `scene_id` (confirmed:
`scan_id` index, `scene_id` as its leading column) and `image_data` has
an *existing* index on `scene_data_id`. So the query is two steps:

1. **Query `scene_data` first** (cheap, uses the real index) to get the
   `scene_data_id` values whose session overlaps the requested window:
   ```sql
   SELECT scene_data_id FROM scene_data
   WHERE scene_id = %s AND end_dt >= %s [AND start_dt <= %s]
   ```
   Use `end_dt >= since_dt` (not `start_dt >= since_dt`) so a session that
   started before the window but was still capturing into it isn't
   missed - a real correctness gap, not just style.
2. **Query `image_data` using those `scene_data_id` values** (batched in
   groups of ~200, via `IN (...)`) instead of `scene_id`/`captured_dt` -
   this uses `image_data`'s existing `scene_data_id` index. Still also
   apply the precise `captured_dt` range filter here as a correctness
   backstop, since `scene_data`'s start/end bounds are coarser than exact
   per-image timestamps.
3. Fetch the actual `img1` BLOBs in a **third**, separate batched step, by
   `image_data_id` (the primary key's leading column, confirmed fully
   unique alone even though it's part of a composite key) - keeps the
   expensive filter/sort steps from ever having to move large BLOB data.
4. Sort the final small in-memory row list by `captured_dt` in Python
   (cheap at this point) rather than asking MySQL to sort.

Confirmed real timing with this approach: **~2-3 seconds total**
(`scene_data` step dominates, since only `scene_id` is indexed there -
`end_dt` still gets scanned within that narrowed set), vs. **3-5+ minutes,
sometimes effectively hung indefinitely**, going directly at `image_data`.

Log a line (`"Querying database..."`) *before* the first query executes,
not after - otherwise the Progress box shows nothing at all while the
query runs, which looks identical to a frozen/crashed app from the user's
perspective. This was a real support incident, not a hypothetical.

If you ever add a new query against `image_data`, check whether it filters
by `scene_id` and/or `captured_dt` - if so, it needs this same two-step
treatment or it will reintroduce the exact same multi-minute hang.

## UI flow (mirrors `scandb_dump_gui.py`'s UX patterns)

1. **Connect**: Database server IP (`Combobox` with saved-IP history),
   DB user (default `remote_root`), DB name (default `scandb`), Password
   (default `password`, masked). "Connect" button.
2. **Pick camera & scene**: two dependent `Combobox`es - System (camera)
   populated from `system_setup` (label shows system_id, camera_name,
   loc_desc, camera_ip, and a "(no scene data yet)" flag if
   `last_scene_id IS NULL` - don't hide these systems, just flag them);
   picking one populates Scene from
   `scene_setup WHERE system_id = ...`.
3. **Time range**: "From" and "To" date/time pickers - calendar-based
   (see widget choice below), each paired with hour/minute/second
   `Spinbox`es. "To" is disabled by a checkbox
   ("Stop at a specific date/time...") that's unchecked by default (open-
   ended = "up to right now"). Default "From" = `now - 1 day`; default
   "To" = `now`; **both have their minute/second zeroed** (hour-aligned
   defaults, not the exact current minute/second) per explicit request.
4. **Save to**: output directory `Entry` + Browse button. Default value is
   `Path.home() / "Documents"` (must resolve per-machine/per-user, not be
   hardcoded to any one person's path - it's meant to run on other
   people's machines from a USB drive).
   Then a zip-mode choice - **two Radiobuttons, no "none" option**:
   "Add zip" (default; keeps the plain folder *and* adds a `.zip` of it)
   and "Zip only" (deletes the unzipped folder after zipping, leaving
   only the `.zip`).
5. **Download** button + a **Progress** log (`Text` widget, read-only,
   fed via a `queue.Queue` drained by `after()` polling every ~150ms from
   a background worker `threading.Thread` - keep the UI responsive during
   the DB query/render).

Every DB-driven field label is prefixed with a small "ⓘ" info icon
(`ttk.Label` reading `"ⓘ"`, blue, `cursor="question_arrow"`) bound to a
custom `Tooltip` class (a borderless `Toplevel` shown on `<Enter>`,
destroyed on `<Leave>`) that shows the literal SQL/column it maps to on
hover - e.g. "System (camera):" hovers to show the exact
`SELECT ... FROM system_setup` query. This was an explicit, deliberate UX
addition - keep it.

## Calendar widget: `ttkbootstrap`, not `tkcalendar`

Use `ttkbootstrap` (`themename="flatly"`) for the whole app - subclass
`ttkbootstrap.Window` instead of `tk.Tk` (it *is* a `tk.Tk` subclass, so
`.title()`/`.geometry()`/`.iconbitmap()` etc. all still work unchanged).
Use `ttkbootstrap.widgets.DateEntry` for the calendar pickers instead of
`tkcalendar.DateEntry` (used by the older `scandb_dump_gui.py`) - it was
swapped in deliberately for a more modern flat look. Note its API differs
from `tkcalendar`:
- Constructor takes `dateformat="%Y-%m-%d"` and `startdate=<datetime>`,
  not `date_pattern="yyyy-mm-dd"` / `year=`/`month=`/`day=`.
- **There is no `.get_date()` method.** Read the typed value via
  `datetime.strptime(widget.entry.get(), "%Y-%m-%d").date()`.
- `.configure(state="disabled"/"normal")` does work as expected (disables
  both the entry and the popup button) - fine to use for the "To" row's
  enable/disable toggle.

Window size must be **660x950**, non-resizable. (`ttkbootstrap`'s theme
uses noticeably more padding than plain `ttk`/`tk` defaults - a smaller
size clips the Progress log off the bottom of the fixed window. If you add
more widgets, re-measure via `app.winfo_reqheight()` after
`update_idletasks()` rather than guessing.)

## Branding / icon

Title/header text: **"SynTemp Vision Data Viewer"**. Icon: a gunmetal-gray
rounded-square badge with a lighter steel-blue border, corner rivets, and
italic (sheared, since Impact has no real italic weight on Windows) white
"SV" / "DATA" text in Impact font (`C:\Windows\Fonts\impact.ttf`),
vertically centered. Save as both `syntemp_vision_icon.png`
(256x256) and `.ico` (multi-size 16/24/32/48/64/128/256) in this folder;
wire the `.ico` in as both the PyInstaller `EXE(icon=...)` and the running
app's `self.iconbitmap(...)`.

Shear-italic gotcha: `PIL.Image.transform(..., Image.AFFINE, (1, SHEAR,
-SHEAR*h/2, 0,1,0))` maps **output pixels back to input pixels**, so the
sign is inverted from intuition - `SHEAR > 0` leans the text right (correct
italic direction), not negative. Got this backwards once already; don't
repeat it.

## Known-IPs history file - frozen-exe path gotcha

`known_server_ips.json` (the saved-IP dropdown history) must be resolved
next to the **real executable**, not via bare `Path(__file__)`:

```python
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
KNOWN_IPS_FILE = APP_DIR / "known_server_ips.json"
```

Reasoning: in a PyInstaller **onefile** build, `__file__` resolves into the
temp extraction folder (`%TEMP%\_MEIxxxxxx\...`), which is randomly-named
per launch and deleted on exit - writing there means the saved-IP history
silently never persists. The icon file doesn't have this problem (it's
read-only and bundled as `datas`, so it exists in that same temp folder
for the duration of that run) - only files this app *writes* for later
need the `sys.executable`-relative fix.

## Packaging: PyInstaller, onefile, single portable .exe

The user explicitly wants **onefile** (a single .exe they can drop on a USB
drive and hand to a coworker), not onedir - even knowing onefile means a
slower launch (unzip-to-temp every run) since there's no Python install on
the target machine. Don't "fix" this back to onedir without being asked;
it's a deliberate portability/convenience tradeoff, not an oversight.

Spec file `SynTempVisionDataViewer.spec` (adapted from the older tool's
`scansceneAnaylze/ScanDBDumpTool.spec`, which used `tkcalendar`/`babel` -
this one needs `ttkbootstrap` instead since `tkcalendar` was fully
replaced):

```python
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('syntemp_vision_icon.ico', '.')]
binaries = []
hiddenimports = ['view_blob']
tmp_ret = collect_all('ttkbootstrap')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['dbImageViewer.py'],
    pathex=[r'C:\Users\synte\sandbox\scansceneAnaylze'],  # so it can find view_blob.py
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='SynTempVisionDataViewer',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    upx_exclude=[], runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon='syntemp_vision_icon.ico',
)
```

Build with: `python -m PyInstaller --noconfirm SynTempVisionDataViewer.spec`
(delete `dist/SynTempVisionDataViewer.exe` first if rebuilding, since the
bootloader can hold a lock on it while it's running).

## Dependencies

`pymysql`, `tkcalendar` (no longer needed once ttkbootstrap swap is done -
`ttkbootstrap` replaces it entirely), `ttkbootstrap`, `Pillow` (only needed
for icon generation, not at app runtime - `view_blob.py` needs
`matplotlib` for its jet colormap, so that stays bundled too). `tkinter`
ships with Python.

## Verification checklist after rebuilding

- `python -c "import ast; ast.parse(open('dbImageViewer.py').read())"` -
  syntax check.
- `python -c "import dbImageViewer"` - import check (also catches missing
  `view_blob.py` path issues).
- Launch it, connect to a real practice DB, confirm: system/scene pickers
  populate, tooltips show on the ⓘ icons, date defaults are hour-aligned
  yesterday/today, Download renders PNGs + summary.csv into
  `<ip>_<scene_id>_1`, running Download again for the same system/scene
  produces `<ip>_<scene_id>_2` (not an overwrite).
- Build the exe, run the actual `dist\SynTempVisionDataViewer.exe` (not
  just `python dbImageViewer.py`) to confirm the frozen build's bundled
  ttkbootstrap theme/icon/view_blob import all actually work - the dev
  environment passing isn't sufficient proof the frozen build works.
- Confirm the Progress log shows `"Querying database..."` immediately on
  clicking Download, and that a real download against production-sized
  data completes in seconds, not minutes - if it hangs, check that the
  query is going through `scene_data` first, not `image_data` directly.
