#!/usr/bin/env python3
"""
dbImageViewer.py - like scandb_dump_gui.py / ScanDBDumpTool, but for
cameras that report into a shared SERVER database (system_setup/scene_setup/
image_data all populated with MANY cameras' rows) instead of each camera
having its own local scandb.

Connects directly to the server DB (no SSH/mysqldump - live pymysql query),
walks the user through: pick a system (camera) -> pick a scene on that
system -> pick a time range -> queries image_data.img1 blobs and renders
them via view_blob.py's existing, already-tested decode/render pipeline.
Output/<label>/<timestamp>/img1_{viewable,color,color_200_600}.png, one
summary.csv per label - nothing else, no raw blob dump. <label> is
<db_server_ip>_<scene_id>_<iteration>, a fresh number per download so
repeat downloads never collide/overwrite each other; the structure
inside it matches the existing output/central_1009/ example exactly
(what a coworker's separate tool consumes).

Requires: pip install pymysql  (already installed; tkinter ships with Python)

Run:
  python dbImageViewer.py
"""

import json
import shutil
import sys
import threading
import queue
import re
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import ttkbootstrap as tb
from ttkbootstrap.widgets import DateEntry
import pymysql

# view_blob.py lives in the sibling scansceneAnaylze/ project (the original
# per-camera tool this one reuses the decode/render pipeline from) - this is
# a copy (scansceneAnaylze/server_scan_dump_gui.py is the other, untouched
# copy), so it needs to point back there to find it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scansceneAnaylze"))
from view_blob import process_blob_bytes, write_summary_csv

# When frozen (PyInstaller onefile), __file__ points into the temp
# extraction folder that's wiped on exit - resolve next to the real exe
# instead so the known-IPs history actually persists between runs.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
KNOWN_IPS_FILE = APP_DIR / "known_server_ips.json"

_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def validate_datetime(s: str) -> str:
    s = s.strip()
    if not _DT_RE.match(s):
        raise ValueError(f"'{s}' isn't in YYYY-MM-DD HH:MM:SS format")
    datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return s


def load_known_ips():
    if KNOWN_IPS_FILE.exists():
        return json.loads(KNOWN_IPS_FILE.read_text())
    return []


def save_known_ip(ip):
    known = load_known_ips()
    if ip not in known:
        known.append(ip)
        KNOWN_IPS_FILE.write_text(json.dumps(known, indent=2))


def fetch_systems(conn):
    """Returns list of dicts: system_id, camera_name, loc_desc, camera_ip,
    last_scene_id - ordered by system_id. Systems with no scene data yet
    (last_scene_id IS NULL) are included but flagged, not hidden - so the
    user can see a system exists even if there's nothing to pull yet."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT system_id, camera_name, loc_desc, camera_ip, last_scene_id "
            "FROM system_setup ORDER BY system_id"
        )
        return cur.fetchall()


def fetch_scenes(conn, system_id):
    """Returns list of dicts: scene_id, scene_name, active - for one system_id."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT scene_id, scene_name, active FROM scene_setup "
            "WHERE system_id = %s ORDER BY scene_id",
            (system_id,),
        )
        return cur.fetchall()


def _next_iteration_label(out_root: Path, base_label: str) -> str:
    """base_label_1, base_label_2, ... - first one not already used as
    either a folder or a .zip under out_root."""
    n = 1
    while (out_root / f"{base_label}_{n}").exists() or (out_root / f"{base_label}_{n}.zip").exists():
        n += 1
    return f"{base_label}_{n}"


def fetch_and_save(conn, system_id, camera_name, db_ip, scene_id, scene_name, since_dt, until_dt,
                    out_root: Path, log, zip_mode="none"):
    """Pulls image_data rows for one scene_id within the time window and
    renders them exactly the way from_db()/from_sqldump() in view_blob.py
    already do - output/<label>/<timestamp>/img1_{viewable,color,
    color_200_600}.png + one summary.csv per label. The <timestamp>/img1_*
    + summary.csv structure inside <label> matches output/central_1009/
    exactly (what the coworker's downstream tool consumes) - only the
    outer <label> folder name itself is customized (IP_sceneid_iteration)
    since that's just for our own traceability, not read by that tool."""
    where = "scene_id = %s AND captured_dt >= %s"
    params = [scene_id, since_dt]
    if until_dt:
        where += " AND captured_dt <= %s"
        params.append(until_dt)

    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            f"SELECT image_data_id, image_id, captured_dt, HEX(img1) AS img1_hex "
            f"FROM image_data WHERE {where} AND img1 IS NOT NULL ORDER BY captured_dt",
            params,
        )
        rows = cur.fetchall()

    if not rows:
        log(f"No image_data rows found for system {system_id} / scene {scene_id} in that range.")
        return 0

    log(f"Found {len(rows)} row(s).")

    # Each download gets its own numbered folder - never reuses an existing
    # one, so nothing ever needs merging and a second download for the same
    # system/scene can't clobber the first one's data.
    label = _next_iteration_label(out_root, f"{db_ip}_{scene_id}")
    out_dir = out_root / label

    summary_rows = []
    saved = 0
    for row in rows:
        img1_hex = row["img1_hex"]
        if not img1_hex:
            continue
        raw = bytes.fromhex(img1_hex)
        stem_dt = str(row["captured_dt"]).replace(":", "-").replace(" ", "_")
        sample_dir = out_dir / stem_dt
        try:
            temp_min, temp_max = process_blob_bytes(raw, sample_dir, "img1")
            summary_rows.append((str(row["captured_dt"]), temp_min, temp_max))
            saved += 1
        except Exception as e:
            log(f"  skipped {stem_dt}: {e}")

    if summary_rows:
        write_summary_csv(out_dir / "summary.csv", summary_rows)
    log(f"Rendered {saved}/{len(rows)} samples into {out_dir}")

    if zip_mode in ("add", "only"):
        _zip_dir(out_dir, log, delete_after=(zip_mode == "only"))

    return saved


def _zip_dir(dir_path: Path, log, delete_after=False):
    zip_path = shutil.make_archive(str(dir_path), "zip", root_dir=dir_path)
    log(f"Zipped to {zip_path}")
    if delete_after:
        shutil.rmtree(dir_path)
        log(f"Removed unzipped folder {dir_path} (zip-only mode).")
    return zip_path


class Tooltip:
    """Small hover tooltip bound to a single widget (used on the 'ⓘ' info
    icons to show what DB query/column a field feeds into)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, font=("Segoe UI", 8),
                 wraplength=320).pack(ipadx=4, ipady=2)

    def _hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class DbImageViewerApp(tb.Window):
    def __init__(self):
        super().__init__(themename="flatly")
        self.title("SynTemp Vision Data Viewer")
        self.geometry("660x950")
        self.resizable(False, False)
        icon_path = Path(__file__).with_name("syntemp_vision_icon.ico")
        if icon_path.exists():
            self.iconbitmap(str(icon_path))

        self.conn = None
        self.systems = []
        self.scenes = []
        self.msg_queue: queue.Queue = queue.Queue()

        pad = {"padx": 10, "pady": 4}

        ttk.Label(self, text="SynTemp Vision Data Viewer", font=("Segoe UI", 12, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0)
        )

        conn_frm = ttk.LabelFrame(self, text="1. Connect")
        conn_frm.pack(fill="x", **pad)

        self._labeled(conn_frm, "Database server IP:",
                      "pymysql.connect(host=...) - the MySQL/MariaDB server to query", row=0)
        self.ip_var = tk.StringVar()
        self.ip_combo = ttk.Combobox(conn_frm, textvariable=self.ip_var, width=27,
                                      values=load_known_ips())
        self.ip_combo.grid(row=0, column=1, sticky="w")

        self._labeled(conn_frm, "DB user:", "pymysql.connect(user=...)", row=1)
        self.user_var = tk.StringVar(value="remote_root")
        ttk.Entry(conn_frm, textvariable=self.user_var, width=30).grid(row=1, column=1, sticky="w")

        self._labeled(conn_frm, "DB name:", "pymysql.connect(database=...) - e.g. scandb", row=2)
        self.db_var = tk.StringVar(value="scandb")
        ttk.Entry(conn_frm, textvariable=self.db_var, width=30).grid(row=2, column=1, sticky="w")

        self._labeled(conn_frm, "Password:", "pymysql.connect(password=...)", row=3)
        self.pass_var = tk.StringVar(value="password")
        ttk.Entry(conn_frm, textvariable=self.pass_var, width=30, show="*").grid(row=3, column=1, sticky="w")

        self.connect_btn = ttk.Button(conn_frm, text="Connect", command=self._connect)
        self.connect_btn.grid(row=4, column=0, columnspan=2, pady=6)

        pick_frm = ttk.LabelFrame(self, text="2. Pick camera & scene")
        pick_frm.pack(fill="x", **pad)

        self._labeled(pick_frm, "System (camera):",
                      "SELECT system_id, camera_name, loc_desc, camera_ip, last_scene_id "
                      "FROM system_setup", row=0)
        self.system_var = tk.StringVar()
        self.system_combo = ttk.Combobox(pick_frm, textvariable=self.system_var, width=55,
                                          state="disabled")
        self.system_combo.grid(row=0, column=1, sticky="w")
        self.system_combo.bind("<<ComboboxSelected>>", self._on_system_selected)

        self._labeled(pick_frm, "Scene:",
                      "SELECT scene_id, scene_name, active FROM scene_setup "
                      "WHERE system_id = <picked system>", row=1)
        self.scene_var = tk.StringVar()
        self.scene_combo = ttk.Combobox(pick_frm, textvariable=self.scene_var, width=55,
                                         state="disabled")
        self.scene_combo.grid(row=1, column=1, sticky="w")

        time_frm = ttk.LabelFrame(self, text="3. Time range")
        time_frm.pack(fill="x", **pad)

        since_default = (datetime.now() - timedelta(days=1)).replace(minute=0, second=0)
        from_row = ttk.Frame(time_frm)
        from_row.grid(row=0, column=0, sticky="w")
        self._info_icon(from_row, "Filters image_data.captured_dt >= this value").pack(side="left")
        ttk.Label(from_row, text="From:", width=6).pack(side="left")
        self.since_cal = DateEntry(from_row, width=12, dateformat="%Y-%m-%d",
                                    startdate=since_default)
        self.since_cal.pack(side="left")
        self.since_hour, self.since_minute, self.since_second = self._time_spinboxes(from_row, since_default)

        self.until_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(time_frm, text="Stop at a specific date/time (leave unchecked to go through right now)",
                         variable=self.until_enabled_var, command=self._toggle_until
                         ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        until_default = datetime.now().replace(minute=0, second=0)
        self.until_row = ttk.Frame(time_frm)
        self.until_row.grid(row=2, column=0, sticky="w")
        self._info_icon(self.until_row, "Filters image_data.captured_dt <= this value "
                        "(only applied if \"Stop at a specific date/time\" is checked)").pack(side="left")
        ttk.Label(self.until_row, text="To:", width=6).pack(side="left")
        self.until_cal = DateEntry(self.until_row, width=12, dateformat="%Y-%m-%d",
                                    startdate=until_default)
        self.until_cal.pack(side="left")
        self.until_hour, self.until_minute, self.until_second = self._time_spinboxes(self.until_row, until_default)
        self._toggle_until()  # start disabled to match until_enabled_var's default

        out_frm = ttk.LabelFrame(self, text="4. Save to")
        out_frm.pack(fill="x", **pad)
        self.outdir_var = tk.StringVar(value=str(Path.home() / "Documents"))
        ttk.Entry(out_frm, textvariable=self.outdir_var, width=45).grid(row=0, column=0, sticky="w", padx=(6, 0))
        ttk.Button(out_frm, text="Browse...", command=self._browse).grid(row=0, column=1, padx=6)

        self.zip_mode_var = tk.StringVar(value="add")
        zip_row = ttk.Frame(out_frm)
        zip_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=(6, 0))
        ttk.Radiobutton(zip_row, text="Add zip", variable=self.zip_mode_var,
                         value="add").pack(side="left")
        ttk.Radiobutton(zip_row, text="Zip only (delete unzipped folder)", variable=self.zip_mode_var,
                         value="only").pack(side="left", padx=(10, 0))

        self.run_btn = ttk.Button(self, text="Download", command=self._start, state="disabled")
        self.run_btn.pack(padx=10, pady=6, anchor="w")

        ttk.Label(self, text="Progress:").pack(anchor="w", padx=10)
        self.log_box = tk.Text(self, height=14, width=76, state="disabled", bg="#f7f7f7")
        self.log_box.pack(padx=10, pady=(0, 10))

        self.after(100, self._poll_queue)

    def _labeled(self, parent, text, query_info, row, column=0, **grid_kw):
        """Grids a 'ⓘ <text>' pair at (row, column) - hovering the ⓘ shows
        what DB query/column this field actually feeds. Returns the row
        frame in case the caller wants to pack more into it."""
        frm = ttk.Frame(parent)
        frm.grid(row=row, column=column, sticky="w", **grid_kw)
        icon = ttk.Label(frm, text="ⓘ", foreground="#0066cc", cursor="question_arrow")
        icon.pack(side="left")
        Tooltip(icon, query_info)
        ttk.Label(frm, text=text).pack(side="left")
        return frm

    def _info_icon(self, parent, query_info):
        """Same 'ⓘ' + tooltip, but as a standalone widget for callers that
        pack their own label right after it (e.g. the From/To time rows)."""
        icon = ttk.Label(parent, text="ⓘ", foreground="#0066cc", cursor="question_arrow")
        Tooltip(icon, query_info)
        return icon

    def _log(self, msg):
        self.msg_queue.put(msg)

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _time_spinboxes(self, parent, default_dt: datetime):
        """Builds the 'at HH:MM:SS' spinbox trio used next to both the From
        and To calendars, pre-filled to default_dt. Returns (hour, minute,
        second) widgets. Same helper as scandb_dump_gui.py."""
        ttk.Label(parent, text="  at").pack(side="left")
        boxes = []
        for i, (lo, hi, val) in enumerate([(0, 23, default_dt.hour), (0, 59, default_dt.minute), (0, 59, default_dt.second)]):
            if i:
                ttk.Label(parent, text=":").pack(side="left")
            box = tk.Spinbox(parent, from_=lo, to=hi, width=3, format="%02.0f")
            box.delete(0, "end")
            box.insert(0, f"{val:02d}")
            box.pack(side="left", padx=(4, 0) if i == 0 else 0)
            boxes.append(box)
        return boxes

    def _toggle_until(self):
        state = "normal" if self.until_enabled_var.get() else "disabled"
        self.until_cal.configure(state=state)
        for w in (self.until_hour, self.until_minute, self.until_second):
            w.configure(state=state)

    def _since_datetime_str(self) -> str:
        d = datetime.strptime(self.since_cal.entry.get(), "%Y-%m-%d").date()
        h, m, s = self.since_hour.get(), self.since_minute.get(), self.since_second.get()
        return f"{d.strftime('%Y-%m-%d')} {int(h):02d}:{int(m):02d}:{int(s):02d}"

    def _until_datetime_str(self):
        if not self.until_enabled_var.get():
            return None
        d = datetime.strptime(self.until_cal.entry.get(), "%Y-%m-%d").date()
        h, m, s = self.until_hour.get(), self.until_minute.get(), self.until_second.get()
        return f"{d.strftime('%Y-%m-%d')} {int(h):02d}:{int(m):02d}:{int(s):02d}"

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.outdir_var.get() or str(Path.home()))
        if chosen:
            self.outdir_var.set(chosen)

    def _connect(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showerror("Missing info", "Enter the database server IP.")
            return
        try:
            self.conn = pymysql.connect(
                host=ip, user=self.user_var.get().strip(),
                password=self.pass_var.get(), database=self.db_var.get().strip(),
            )
            save_known_ip(ip)
            self.systems = fetch_systems(self.conn)
        except Exception as e:
            messagebox.showerror("Connection failed", str(e))
            return

        labels = []
        for s in self.systems:
            flag = "" if s["last_scene_id"] is not None else "  (no scene data yet)"
            labels.append(
                f"{s['system_id']}  {s['camera_name']}  -  {s['loc_desc']}  [{s['camera_ip']}]{flag}"
            )
        self.system_combo.configure(values=labels, state="readonly")
        self._log(f"Connected. {len(self.systems)} system(s) found.")

    def _on_system_selected(self, event=None):
        idx = self.system_combo.current()
        if idx < 0:
            return
        system = self.systems[idx]
        try:
            self.scenes = fetch_scenes(self.conn, system["system_id"])
        except Exception as e:
            messagebox.showerror("Query failed", str(e))
            return

        if not self.scenes:
            self.scene_combo.configure(values=[], state="disabled")
            self.run_btn.configure(state="disabled")
            self._log(f"System {system['system_id']} has no scenes in scene_setup.")
            return

        labels = [
            f"{sc['scene_id']}  {sc['scene_name']}  (active: {sc['active']})"
            for sc in self.scenes
        ]
        self.scene_combo.configure(values=labels, state="readonly")
        self.scene_combo.current(0)
        self.run_btn.configure(state="normal")

    def _start(self):
        sys_idx = self.system_combo.current()
        scene_idx = self.scene_combo.current()
        if sys_idx < 0 or scene_idx < 0:
            messagebox.showerror("Missing info", "Pick a system and a scene first.")
            return
        system = self.systems[sys_idx]
        scene = self.scenes[scene_idx]

        try:
            since_dt = validate_datetime(self._since_datetime_str())
            until_str = self._until_datetime_str()
            until_dt = validate_datetime(until_str) if until_str else None
        except ValueError as e:
            messagebox.showerror("Invalid date", str(e))
            return
        if until_dt and until_dt <= since_dt:
            messagebox.showerror("Invalid range", "The \"To\" date/time must be after the \"From\" date/time.")
            return

        outdir = self.outdir_var.get().strip()
        if not outdir:
            messagebox.showerror("Missing info", "Choose a folder to save the files to.")
            return
        out_root = Path(outdir)
        out_root.mkdir(parents=True, exist_ok=True)

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.run_btn.configure(state="disabled")

        def worker():
            try:
                fetch_and_save(
                    self.conn, system["system_id"], system["camera_name"], self.ip_var.get().strip(),
                    scene["scene_id"], scene["scene_name"], since_dt, until_dt,
                    out_root, self._log, zip_mode=self.zip_mode_var.get(),
                )
                self._log("Done.")
            except Exception as e:
                self._log(f"ERROR: {e}")
            finally:
                self.run_btn.configure(state="normal")

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    DbImageViewerApp().mainloop()
