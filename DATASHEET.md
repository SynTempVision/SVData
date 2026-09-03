# Database Datasheet

Reference for the `scandb` tables this tool reads. Read-only - this tool
never writes to any of these. Confirmed against a real production
database (SynTemp-LB002, MariaDB 10.4.10) except where noted as "practice
DB" only.

## `system_setup`

One row per camera (a "system").

| Column | Notes |
|---|---|
| `system_id` | Zero-padded 6-digit int. Independent numbering from `scene_id` - **never assume they line up** (confirmed: system_id `001020`'s `last_scene_id` was `1050`, not `1020`). |
| `camera_name` | Friendly name. |
| `loc_desc` | Location description. |
| `camera_ip` | The camera's own IP (distinct from the DB server IP). |
| `server_ip` | - |
| `last_scene_id` | `NULL` if this system has never captured a scene. **This is the source of truth for "what scene is this camera writing to right now"** - not whatever scene looks newest in a picker UI. |

## `scene_setup`

| Column | Notes |
|---|---|
| `system_id` | Links to `system_setup`. |
| `scene_id` | Independent numbering from `system_id` (see above). |
| `scene_name` | - |
| `active` | - |

## `image_data`

**~1.26 million rows** in production (confirmed 2026-09-03: 1,257,988).
This is a **shared table across every camera on the server**, not just
one scene.

| Column | Notes |
|---|---|
| `image_data_id` | Leading column of the composite primary key (see Indexes below). Confirmed fully unique alone - cardinality matches total row count even though it's not a standalone key. |
| `scene_data_id` | Links to `scene_data.scene_data_id`. **Has its own index** - see Indexes below, this is the fast path. |
| `scene_id` | Links to `scene_setup`/`system_setup`. **No system_id column here** - filter by `scene_id` alone once a system's scene is picked. |
| `image_id` | - |
| `image_dt` | **NOT NULL, auto-updates.** Safer filter if `captured_dt` is ever found to have gaps. |
| `captured_dt` | Nullable - "true" capture time, but not guaranteed populated. This tool filters/sorts on this column; no gaps observed in testing so far, but worth rechecking if rows ever seem to be missing from a time-range query. |
| `alarm`, `p_offset`, `t_offset` | Not used by this tool. |
| `img1`, `img2` | BLOBs. Queried as `HEX(img1)` then decoded client-side with `bytes.fromhex()`. `img2` not used by this tool. |

**On-disk location** (production, SynTemp-LB002): `datadir` =
`e:\mysql\data\` - the `E:` drive there is a spinning HDD, not the SSD
(`C:`). Worth checking on any server this runs against, since a large
table on mechanical storage meaningfully compounds any missing-index
slowness.

## `scene_data`

**~1.37 million rows** in production (confirmed 2026-09-03: 1,365,234) -
**not** a small table, despite having no BLOB columns. One row per
capture "session" - in observed data, sessions for a given scene were
very short (~1 second, `start_dt` to `end_dt`), spaced at a regular
interval (e.g. every 15 minutes) - but don't assume this cadence is
universal across all scenes.

| Column | Notes |
|---|---|
| `scene_data_id` | Primary key. |
| `scene_id` | **Has a real index here** (see Indexes below) - this is what makes the two-step query approach fast. |
| `start_dt`, `end_dt` | Session bounds. Use `end_dt >= since` (not `start_dt >= since`) when filtering for "sessions overlapping a time window," so a session that started earlier but ran into the window isn't missed. |
| `alarm`, `status` | Not used by this tool. |

## Indexes (the whole reason the two-step query exists)

```
image_data:
  PRIMARY   (image_data_id, scene_id)   -- composite; scene_id is NOT
                                            usable alone since it's not
                                            the leading column
  scene_data_id                          -- single-column, USABLE for
                                            WHERE scene_data_id = ...

scene_data:
  PRIMARY   (scene_data_id)
  scan_id   (scene_id)                   -- single-column, USABLE for
                                            WHERE scene_id = ...
                                            (misleadingly named "scan_id"
                                            despite indexing scene_id)
```

**The problem**: `image_data` has no index usable for `WHERE scene_id = ...`
or `WHERE captured_dt >= ...` - a query filtering on either forces a full
table scan across ~1.26M rows (confirmed: 10-25+ minutes, sometimes
effectively hanging, `SHOW PROCESSLIST` shows `State: Creating sort index`
when combined with `ORDER BY`).

**The fix this tool uses**: query `scene_data` first (`scene_id` is
indexed there) to get the small set of relevant `scene_data_id` values,
then query `image_data` by `scene_data_id` (also indexed) instead of by
`scene_id` directly. See `REBUILD_PROMPT.md`'s "Query performance"
section for the full implementation detail. Confirmed real timing:
~2-3 seconds total this way, vs. minutes/hung going directly at
`image_data`.

If you're extending this tool and adding any new query against
`image_data`, check whether it filters by `scene_id` and/or `captured_dt`
- if so, it needs the same `scene_data`-routing treatment or it will
reintroduce the exact same multi-minute hang.
