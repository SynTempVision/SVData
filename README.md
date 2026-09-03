# SynTemp Vision Data Viewer (SVData)

A Windows GUI tool that connects directly (via `pymysql`, no SSH/mysqldump)
to a **shared server database** where many cameras' rows are all populated
into the same tables (`system_setup`, `scene_setup`, `image_data`,
`scene_data`), distinguished by `system_id`/`scene_id` - as opposed to the
older per-camera tool (`scansceneAnaylze/scandb_dump_gui.py` /
`ScanDBDumpTool.exe`), which SSHes into one camera's own local `scandb`.

Picks a camera (system) -> picks a scene on that camera -> picks a time
range -> pulls the thermal image blobs for that scene and renders them to
PNGs, reusing the existing, already-tested decode/render pipeline in
`scansceneAnaylze/view_blob.py` rather than duplicating it.

See `DATASHEET.md` for the DB schema this tool depends on, and
`CHANGELOG.md` for version history. `REBUILD_PROMPT.md` is a full spec an
AI assistant can rebuild this tool from scratch from if the source is ever
lost.

## Rules

1. **Read-only.** Never writes to the database, only `SELECT`.
2. Reuses `scansceneAnaylze/view_blob.py`'s decode/render pipeline rather
   than duplicating it (`process_blob_bytes`, `write_summary_csv`).
3. Output format for what's *inside* a label folder never changes -
   `<label>/<timestamp>/img1_{viewable,color,color_200_600}.png` +
   `summary.csv` - a coworker's separate downstream program consumes that
   exact structure.

## Usage

```
python dbImageViewer.py
```

or run the built standalone exe (`dist/SynTempVisionDataViewer.exe`,
built via `SynTempVisionDataViewer.spec` - see below).

1. **Connect**: database server IP, DB user (default `remote_root`), DB
   name (default `scandb`), password.
2. **Pick camera & scene**: System (camera) from `system_setup`, then
   Scene from `scene_setup WHERE system_id = ...`.
3. **Time range**: From/To calendar pickers (`ttkbootstrap.DateEntry`),
   defaults to "yesterday through right now," hour-aligned.
4. **Save to**: output directory, plus a zip mode - **Add zip** (keeps the
   plain folder and adds a `.zip`) or **Zip only** (deletes the unzipped
   folder after zipping).
5. **Download** - runs in a background thread, Progress log shows live
   status.

## Output

```
<out_root>/<db_server_ip>_<scene_id>_<iteration>/<timestamp>/img1_viewable.png
<out_root>/<db_server_ip>_<scene_id>_<iteration>/<timestamp>/img1_color.png
<out_root>/<db_server_ip>_<scene_id>_<iteration>/<timestamp>/img1_color_200_600.png
<out_root>/<db_server_ip>_<scene_id>_<iteration>/summary.csv
```

`<iteration>` is an auto-incrementing number (`_1`, `_2`, ...) - every
download gets its own never-reused folder, so two downloads for the same
system/scene can never collide or overwrite each other.

## Building the standalone exe

```
python -m PyInstaller --noconfirm SynTempVisionDataViewer.spec
```

Onefile build by design (portable - drop the single exe on a USB drive
and hand it to someone with no Python install), not onedir - slower
startup (unpacks to temp on every launch) is an accepted tradeoff for
portability.

## Dependencies

`pymysql`, `ttkbootstrap` (theme + `DateEntry` widget), `pyinstaller`
(build only). `scansceneAnaylze/view_blob.py`'s own dependencies
(`matplotlib`, for its jet colormap) come along via the `sys.path` import.
`tkinter` ships with Python.
