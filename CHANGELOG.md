# Changelog

## v1.1.0 - 2026-09-03

### Highlights
- Fixed a severe query performance issue: downloads now route through
  `scene_data` (which has a usable index) instead of querying `image_data`
  directly by `scene_id`/`captured_dt` (which has none) - confirmed
  real-world queries dropped from 3-25+ minutes (sometimes effectively
  hanging) down to ~2-3 seconds.
- Added an immediate "Querying database..." log line so the Progress box
  never looks frozen while a query is in flight.

### Results
- Verified against production data on SynTemp-LB002 (`scandb`,
  ~1.26M-row `image_data` table): direct query took 10-25+ minutes or
  hung indefinitely; the new two-step `scene_data`-routed query completed
  in ~2-3 seconds for the same result set.
- Output correctness unchanged - confirmed via a mock test that row
  order/blob matching is identical between the old and new query paths.

### Known Issues
- The `scene_data` step's `end_dt` filter is itself unindexed within the
  `scene_id`-narrowed result set, so a scene with a very long history
  could still take a few seconds - a large improvement over the prior
  multi-minute behavior, but not instant.
- The root database issue (no index covering
  `image_data(scene_id, captured_dt)`) is unfixed at the DB level by
  design - the fix lives entirely in the app's query strategy, not a
  schema change, per explicit request not to alter the database.

### Compatibility
- No output format changes - `<label>/<timestamp>/img1_*.png` +
  `summary.csv` structure is identical to prior versions.
- No UI changes required by this fix; existing saved settings/known IPs
  unaffected.

---

## v1.0.0 - 2026-09-02

### Highlights
- Initial release: connects directly to a shared server database
  (`system_setup`/`scene_setup`/`image_data`/`scene_data`), picks
  camera -> scene -> time range, downloads and renders thermal images.
- Iteration-numbered output folders (`<db_ip>_<scene_id>_<n>`) so repeat
  downloads for the same system/scene never overwrite each other.
- `ttkbootstrap`-based UI (flatly theme) with calendar date pickers,
  per-field DB-query tooltips (hover the ⓘ icon), and custom SynTemp
  Vision branding/icon.
- Zip options: "Add zip" (keep the plain folder and add a `.zip`) or
  "Zip only" (zip then delete the unzipped folder).
- Packaged as a standalone onefile PyInstaller exe for portable
  distribution (no Python install required on the target machine).

### Results
- Verified end-to-end against a practice server DB: connect -> pick
  system/scene -> download -> rendered PNGs + summary.csv landed
  correctly in the expected output structure.

### Known Issues
- Query performance issue with large `image_data` tables - see the
  v1.1.0 fix above.
- Phase 2 (stitching multi-image scenes into a single composite image)
  not implemented.

### Compatibility
- Output format matches the existing `output/central_1009/` reference
  example that a coworker's separate downstream tool consumes.
