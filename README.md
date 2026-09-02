# dbImageViewer




## Scope / Requirements

1. GUI that 
	a. Connect to a database (scandb)
	b. query scene_id, ....
	c. Select a time range
	d. bring raw blobs 
	e. ( need to organize them in a way they can be proccessed)
		i. meaning either save the raw image name as (IP, scene_id, sceneimage_id, system_id..)
	f. process them 
	g. output them 
	h. zip (optional)
2. Part 2
	a. based on the type of scene single image, 2x2, 3x2, whatever it is - build th eimage 
	b. resave as a single image
	
## Rules:
1. do not write to the db - never write only read
2. phase 1 has to be complete before moving to a new phase
3. i need to understand everything

## Status (2026-09-02)

Built as `sandbox\scansceneAnaylze\server_scan_dump_gui.py`, reusing `view_blob.py`'s
existing decode/render code rather than duplicating it.

**Real schema confirmed against the practice server DB** (multiple cameras
populated into one shared `scandb`):
- `system_setup` - the systems/cameras lookup table: `system_id` (zero-padded
  6-digit int), `camera_name`, `loc_desc`, `camera_ip`, `server_ip`,
  `last_scene_id` (NULL if that system has never captured a scene yet). This
  is the "friendly name" source for step 1 of the picker.
- `scene_setup` already has `system_id` on it (same table as the per-camera
  scandb, just many systems' rows instead of one).
- **`system_id` and `scene_id` are independent numbering, not parallel** -
  confirmed on real data (system_id 001020's `last_scene_id` is 1050, not
  1020). Never assume they line up.
- `image_data` has `scene_id` but NO `system_id` column - filtering is by
  `scene_id` alone once a system's scene has been picked (same as the
  per-camera tool). Scene_id numbers haven't collided across systems in the
  data seen so far.
- `scene_data` exists (`start_dt`/`end_dt`/`alarm`/`status`, no blobs) but
  is NOT currently fetched by this tool - decided against it to keep the
  output format from changing (see below).

**Picker flow:** pick system (from `system_setup`) -> pick scene (from
`scene_setup WHERE system_id=...`) -> pick a time range -> Download.

**Output format - deliberately kept identical to the existing
`output/central_1009/` example**, since a coworker's separate program
already consumes that exact structure: `output/central_<scene_id>/
<timestamp>/img1_{viewable,color,color_200_600}.png` + one `summary.csv`
per label. No raw blob dump, no extra subfolders, no `scene_data.csv` -
those were built at one point during this session and then deliberately
removed once the "don't change the output format" constraint came up.
Optional zip-the-output-folder checkbox added per the scope list above.

Part 2 (stitching multi-image scenes into one composite) not started -
`stitch_scenes.py` already does this from file dumps; adapting it to the
live-query path is the next step whenever this tool needs to handle a
system with a multi-image scene (image_x*image_y > 1).

