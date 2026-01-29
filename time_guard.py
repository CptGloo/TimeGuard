from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
import json
import urllib.request
import urllib.error
import threading
import pystray
from PIL import Image, ImageDraw

def get_app_dir() -> str:
    # Windows-friendly app data folder
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "TimeGuard")
    else:
        # Linux/macOS
        path = os.path.join(os.path.expanduser("~"), ".timeguard")
    os.makedirs(path, exist_ok=True)
    return path

def get_db_path() -> str:
    return os.path.join(get_app_dir(), "assistant.db")

def get_lock_path() -> str:
    return os.path.join(get_app_dir(), "assistant_ui.lock")

def get_self_launch_cmd() -> List[str]:
    # When frozen by PyInstaller, sys.executable is the .exe
    # When running as a .py, sys.executable is python and sys.argv[0] is the script path.
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(sys.argv[0])]

def make_tray_icon() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, size - 8, size - 8), fill=(40, 40, 40, 255))
    d.rectangle((30, 18, 34, 46), fill=(255, 255, 255, 255))  # simple "!"
    d.rectangle((30, 50, 34, 54), fill=(255, 255, 255, 255))
    return img

# -------------------------
# Data model
# -------------------------

@dataclass(frozen=True)
class Reminder:
    id: int
    title: str
    note: str
    due_at: datetime
    repeat_minutes: Optional[int]
    enabled: bool


# -------------------------
# Storage (SQLite)
# -------------------------

class Storage:
    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = get_db_path()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        ...
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                due_at TEXT NOT NULL,
                repeat_minutes INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_fired_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(enabled, due_at);

            CREATE TABLE IF NOT EXISTS routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                time_hhmm TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS active_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,     -- ISO datetime
                message TEXT NOT NULL,
                source TEXT NOT NULL,         -- reminder|routine
                acknowledged INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_active_alerts_ack
            ON active_alerts(acknowledged, created_at);
            """
        )
        self._conn.commit()

    def add_alert(self, message: str, source: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.execute(
            "INSERT INTO active_alerts(created_at, message, source, acknowledged) VALUES (?, ?, ?, 0)",
            (now, message, source),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_unacked_alerts(self) -> List[Tuple[int, str, str, str]]:
        cur = self._conn.execute(
            "SELECT id, created_at, source, message FROM active_alerts WHERE acknowledged=0 ORDER BY id ASC"
        )
        return list(cur.fetchall())

    def ack_alert(self, alert_id: int) -> None:
        self._conn.execute("UPDATE active_alerts SET acknowledged=1 WHERE id=?", (alert_id,))
        self._conn.commit()

    def ack_all_alerts(self) -> None:
        self._conn.execute("UPDATE active_alerts SET acknowledged=1 WHERE acknowledged=0")
        self._conn.commit()


    def add_activity(self, kind: str, text: str, ts: Optional[datetime] = None) -> None:
        ts = ts or datetime.now()
        self._conn.execute(
            "INSERT INTO activity_log(ts, kind, text) VALUES (?, ?, ?)",
            (ts.isoformat(timespec="seconds"), kind, text),
        )
        self._conn.commit()

    def get_recent_activity(self, limit: int = 20) -> List[Tuple[str, str, str]]:
        cur = self._conn.execute(
            "SELECT ts, kind, text FROM activity_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(cur.fetchall())

    def enable_all_reminders(self) -> None:
        self._conn.execute("UPDATE reminders SET enabled=1")
        self._conn.commit()

    def enable_all_routines(self) -> None:
        self._conn.execute("UPDATE routines SET enabled=1")
        self._conn.commit()


    def add_reminder(
        self,
        title: str,
        due_at: datetime,
        note: str = "",
        repeat_minutes: Optional[int] = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO reminders(title, note, due_at, repeat_minutes, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            (title, note, due_at.isoformat(timespec="seconds"), repeat_minutes),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def set_reminder_enabled(self, reminder_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE reminders SET enabled=? WHERE id=?",
            (1 if enabled else 0, reminder_id),
        )
        self._conn.commit()

    def list_reminders(self) -> List[Reminder]:
        cur = self._conn.execute(
            "SELECT id, title, note, due_at, repeat_minutes, enabled FROM reminders ORDER BY due_at ASC"
        )
        rows = cur.fetchall()
        out: List[Reminder] = []
        for rid, title, note, due_at, repeat, enabled in rows:
            out.append(
                Reminder(
                    id=rid,
                    title=title,
                    note=note or "",
                    due_at=datetime.fromisoformat(due_at),
                    repeat_minutes=repeat,
                    enabled=bool(enabled),
                )
            )
        return out

    def get_due_reminders(self, now: datetime) -> List[Reminder]:
        cur = self._conn.execute(
            """
            SELECT id, title, note, due_at, repeat_minutes, enabled
            FROM reminders
            WHERE enabled=1 AND due_at <= ?
            ORDER BY due_at ASC
            """,
            (now.isoformat(timespec="seconds"),),
        )
        rows = cur.fetchall()
        out: List[Reminder] = []
        for rid, title, note, due_at, repeat, enabled in rows:
            out.append(
                Reminder(
                    id=rid,
                    title=title,
                    note=note or "",
                    due_at=datetime.fromisoformat(due_at),
                    repeat_minutes=repeat,
                    enabled=bool(enabled),
                )
            )
        return out

    def snooze_reminder(self, reminder_id: int, minutes: int) -> None:
        new_due = datetime.now() + timedelta(minutes=minutes)
        self._conn.execute(
            "UPDATE reminders SET due_at=? WHERE id=?",
            (new_due.isoformat(timespec="seconds"), reminder_id),
        )
        self._conn.commit()

    def fire_reminder(self, reminder: Reminder, fired_at: datetime) -> None:
        if reminder.repeat_minutes is None:
            self._conn.execute(
                "UPDATE reminders SET enabled=0, last_fired_at=? WHERE id=?",
                (fired_at.isoformat(timespec="seconds"), reminder.id),
            )
        else:
            next_due = max(reminder.due_at, fired_at) + timedelta(minutes=reminder.repeat_minutes)
            self._conn.execute(
                "UPDATE reminders SET due_at=?, last_fired_at=? WHERE id=?",
                (
                    next_due.isoformat(timespec="seconds"),
                    fired_at.isoformat(timespec="seconds"),
                    reminder.id,
                ),
            )
        self._conn.commit()

    def add_routine(self, title: str, time_hhmm: str, note: str = "") -> int:
        cur = self._conn.execute(
            """
            INSERT INTO routines(title, note, time_hhmm, enabled)
            VALUES (?, ?, ?, 1)
            """,
            (title, note, time_hhmm),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_routines(self) -> List[Tuple[int, str, str, str, bool]]:
        cur = self._conn.execute(
            "SELECT id, title, note, time_hhmm, enabled FROM routines ORDER BY time_hhmm ASC"
        )
        rows = cur.fetchall()
        return [(rid, t, n or "", hhmm, bool(en)) for (rid, t, n, hhmm, en) in rows]

    # ---- reminders: delete / edit ----

    def delete_reminder(self, reminder_id: int) -> None:
        self._conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
        self._conn.commit()

    def update_reminder_title(self, reminder_id: int, title: str) -> None:
        self._conn.execute("UPDATE reminders SET title=? WHERE id=?", (title, reminder_id))
        self._conn.commit()

    def update_reminder_note(self, reminder_id: int, note: str) -> None:
        self._conn.execute("UPDATE reminders SET note=? WHERE id=?", (note, reminder_id))
        self._conn.commit()

    def update_reminder_due_at(self, reminder_id: int, due_at: datetime) -> None:
        self._conn.execute(
            "UPDATE reminders SET due_at=? WHERE id=?",
            (due_at.isoformat(timespec="seconds"), reminder_id),
        )
        self._conn.commit()

    def update_reminder_repeat_minutes(self, reminder_id: int, repeat_minutes: Optional[int]) -> None:
        self._conn.execute(
            "UPDATE reminders SET repeat_minutes=? WHERE id=?",
            (repeat_minutes, reminder_id),
        )
        self._conn.commit()


    # ---- routines: enable/disable / delete / edit ----

    def set_routine_enabled(self, routine_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE routines SET enabled=? WHERE id=?",
            (1 if enabled else 0, routine_id),
        )
        self._conn.commit()

    def delete_routine(self, routine_id: int) -> None:
        self._conn.execute("DELETE FROM routines WHERE id=?", (routine_id,))
        self._conn.commit()

    def update_routine_title(self, routine_id: int, title: str) -> None:
        self._conn.execute("UPDATE routines SET title=? WHERE id=?", (title, routine_id))
        self._conn.commit()

    def update_routine_note(self, routine_id: int, note: str) -> None:
        self._conn.execute("UPDATE routines SET note=? WHERE id=?", (note, routine_id))
        self._conn.commit()

    def update_routine_time(self, routine_id: int, time_hhmm: str) -> None:
        _ = parse_hhmm(time_hhmm)  # validate
        self._conn.execute("UPDATE routines SET time_hhmm=? WHERE id=?", (time_hhmm, routine_id))
        self._conn.commit()



# -------------------------
# Parsing helpers
# -------------------------

def parse_hhmm(hhmm: str) -> Tuple[int, int]:
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Time must be HH:MM")
    h = int(parts[0])
    m = int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Invalid hour/minute")
    return h, m


def parse_when(tokens: List[str]) -> datetime:
    now = datetime.now()

    if len(tokens) >= 2 and tokens[0] == "in":
        amt = tokens[1].lower().strip()
        if amt.endswith("m"):
            mins = int(amt[:-1])
            return now + timedelta(minutes=mins)
        if amt.endswith("h"):
            hrs = int(amt[:-1])
            return now + timedelta(hours=hrs)
        raise ValueError("Use in 10m or in 2h")

    if len(tokens) >= 2 and tokens[0] in ("at", "today", "tomorrow"):
        hhmm = tokens[1]
        h, m = parse_hhmm(hhmm)
        base = now.date()
        if tokens[0] == "tomorrow":
            base = (now + timedelta(days=1)).date()
        return datetime.combine(base, datetime.min.time()).replace(hour=h, minute=m, second=0)

    if len(tokens) >= 2 and "-" in tokens[0]:
        date_str = tokens[0]
        hhmm = tokens[1]
        y, mo, d = [int(x) for x in date_str.split("-")]
        h, m = parse_hhmm(hhmm)
        return datetime(y, mo, d, h, m, 0)

    raise ValueError("Try: in 10m | at 14:30 | tomorrow 09:00 | 2026-01-12 18:00")



def format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# -------------------------
# Engine
# -------------------------

class Engine:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._last_routine_check_date: Optional[str] = None
        self._fired_routines_today: set[Tuple[str, str]] = set()

    def tick(self) -> List[str]:
        now = datetime.now()
        notifications: List[str] = []

        today_iso = now.date().isoformat()
        if self._last_routine_check_date != today_iso:
            self._last_routine_check_date = today_iso
            self._fired_routines_today = set()

        for r in self._storage.get_due_reminders(now):
            line = f"REMINDER: {r.title} (due {format_dt(r.due_at)})"
            if r.note:
                line += f" — {r.note}"
            notifications.append(line)
            self._storage.fire_reminder(r, now)

        for rid, title, note, time_hhmm, enabled in self._storage.list_routines():
            if not enabled:
                continue
            h, m = parse_hhmm(time_hhmm)
            routine_dt = datetime.combine(now.date(), datetime.min.time()).replace(hour=h, minute=m, second=0)
            key = (today_iso, time_hhmm)
            if routine_dt <= now and key not in self._fired_routines_today:
                line = f"ROUTINE: {title} ({time_hhmm})"
                if note:
                    line += f" — {note}"
                notifications.append(line)
                self._fired_routines_today.add(key)

        return notifications

_ui_lock = threading.Lock()
_ui_open = False

class ServiceRunner:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_nag = 0.0
        nag_every_seconds = 60.0

        storage = Storage()
        storage.enable_all_reminders()
        storage.enable_all_routines()
        engine = Engine(storage)

        while not self._stop.is_set():
            notes = engine.tick()

            now_t = time.time()

            # nag if unacked alerts exist
            if now_t - last_nag >= nag_every_seconds:
                unacked = storage.get_unacked_alerts()
                if unacked:
                    last_nag = now_t
                    preview = "\n".join([f"[{aid}] {msg}" for (aid, _ts, _src, msg) in unacked[:3]])
                    if len(unacked) > 3:
                        preview += f"\n(+{len(unacked)-3} more)"
                    send_notification("Pending alerts (ack needed)", preview, blink_seconds=12, force_foreground=True)

            # new alerts
            if notes:
                for n in notes:
                    storage.add_alert(n, source="reminder")

                msg = "\n".join(notes[:3]) + ("" if len(notes) <= 3 else f"\n(+{len(notes)-3} more)")
                send_notification("Personal Assistant", msg, blink_seconds=12, force_foreground=True)

                # optional: auto-open UI when something triggers
                # open_ui_window(prefill=msg)

            time.sleep(2.0)

# -------------------------
# UI popup (Tkinter)
# -------------------------

HELP_TEXT = """
Commands:
  status <text>                 Log what you're doing right now
  did <text>                    Log something you did
  note <text>                   Log a note

  remind <when> <title>         Create a one-time reminder
  remind_repeat <when> <mins> <title>  Create repeating reminder every <mins> minutes

  routine <HH:MM> <title>       Add a daily routine reminder at time
  list                          List reminders + routines
  recent [n]                    Show recent activity
  snooze <id> <mins>            Snooze a reminder by minutes
  disable <id>                  Disable a reminder
  enable <id>                   Enable a reminder

  Edit / delete:
  rem_del <id>
  rem_title <id> <new title...>
  rem_note <id> <new note...>
  rem_due <id> <when>             (ex: rem_due 3 tomorrow 09:00)
  rem_repeat <id> <mins|off>      (ex: rem_repeat 3 30 | rem_repeat 3 off)

  rt_del <id>
  rt_enable <id>
  rt_disable <id>
  rt_title <id> <new title...>
  rt_note <id> <new note...>
  rt_time <id> <HH:MM>

  ack <id>
  ack_all
  
  help
  quit

Time formats:
  in 10m     | in 2h
  at 14:30   | today 18:00 | tomorrow 09:00
  2026-01-12 18:00

Examples:
  status working on Godot aim trainer
  did 30 min aim practice
  remind in 25m stand up
  remind_repeat at 09:00 60 drink water
  routine 22:30 prep for tomorrow
""".strip()



def run_ui_popup(prefill: str = "") -> None:
    import tkinter as tk
    from tkinter import scrolledtext

    storage = Storage()
    root = tk.Tk()
    root.title("Personal Assistant")
    root.geometry("700x500")
    #root.attributes("-topmost", True)
    root.bell()

    output = scrolledtext.ScrolledText(root, wrap=tk.WORD)
    output.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    entry = tk.Entry(root)
    entry.pack(fill=tk.X, padx=8, pady=(0, 8))

    def write(line: str) -> None:
        output.insert(tk.END, line + "\n")
        output.see(tk.END)

    def refresh_recent() -> None:
        write("— Recent activity —")
        rows = storage.get_recent_activity(15)
        for ts, kind, text in reversed(rows):
            write(f"{ts} [{kind}] {text}")
        write("")

    def print_list() -> None:
        write("— Reminders —")
        for r in storage.list_reminders():
            rep = f" every {r.repeat_minutes}m" if r.repeat_minutes is not None else ""
            on = "ON" if r.enabled else "OFF"
            write(f"[{r.id}] {on} {format_dt(r.due_at)}{rep} — {r.title}" + (f" ({r.note})" if r.note else ""))
        write("")
        write("— Routines (daily) —")
        for rid, title, note, hhmm, enabled in storage.list_routines():
            on = "ON" if enabled else "OFF"
            write(f"[{rid}] {on} {hhmm} — {title}" + (f" ({note})" if note else ""))
        write("")

    def handle_command(cmd: str) -> None:
        parts = cmd.strip().split()
        if not parts:
            return
        op = parts[0].lower()
        rest = parts[1:]

        try:
            if op == "help":
                for line in HELP_TEXT.splitlines():
                    write(line)

            elif op == "quit" or op == "exit":
                root.destroy()
            elif op in ("status", "did", "note"):
                text = " ".join(rest).strip()
                storage.add_activity(op, text)
                write("Logged.")
            elif op == "recent":
                refresh_recent()
            elif op == "list":
                print_list()
            elif op == "remind":
                due = parse_when(rest[:2])
                title = " ".join(rest[2:])
                rid = storage.add_reminder(title=title, due_at=due)
                write(f"Created reminder [{rid}] for {format_dt(due)}.")
            elif op == "remind_repeat":
                due = parse_when(rest[:2])
                mins = int(rest[2])
                title = " ".join(rest[3:])
                rid = storage.add_reminder(title=title, due_at=due, repeat_minutes=mins)
                write(f"Created repeating reminder [{rid}] starting {format_dt(due)} every {mins}m.")
            elif op == "routine":
                hhmm = rest[0]
                _ = parse_hhmm(hhmm)
                title = " ".join(rest[1:])
                rid = storage.add_routine(title=title, time_hhmm=hhmm)
                write(f"Created routine [{rid}] at {hhmm} daily.")
            elif op == "snooze":
                rid = int(rest[0])
                mins = int(rest[1])
                storage.snooze_reminder(rid, mins)
                write("Snoozed.")
            elif op == "disable":
                rid = int(rest[0])
                storage.set_reminder_enabled(rid, False)
                write("Disabled.")
            elif op == "enable":
                rid = int(rest[0])
                storage.set_reminder_enabled(rid, True)
                write("Enabled.")
            elif op == "alerts":
                rows = storage.get_unacked_alerts()
                if not rows:
                    write("No pending alerts 🎉")
                else:
                    write("— Pending alerts —")
                    for aid, ts, src, msg in rows:
                        write(f"[{aid}] {ts} ({src}) {msg}")
                    write("Use: ack <id>  |  ack_all")
            elif op == "ack":
                aid = int(rest[0])
                storage.ack_alert(aid)
                write("Acknowledged.")

            elif op == "ack_all":
                storage.ack_all_alerts()
                write("All acknowledged.")

            # -------- reminders: delete / edit --------

            elif op == "rem_del":
                rid = int(rest[0])
                storage.delete_reminder(rid)
                write("Reminder supprimé.")

            elif op == "rem_title":
                rid = int(rest[0])
                title = " ".join(rest[1:]).strip()
                storage.update_reminder_title(rid, title)
                write("Titre modifié.")

            elif op == "rem_note":
                rid = int(rest[0])
                note = " ".join(rest[1:]).strip()
                storage.update_reminder_note(rid, note)
                write("Note modifiée.")

            elif op == "rem_due":
                # rem_due <id> <when...>
                rid = int(rest[0])
                due = parse_when(rest[1:3])  # mêmes formats que remind
                storage.update_reminder_due_at(rid, due)
                write(f"Date modifiée: {format_dt(due)}")

            elif op == "rem_repeat":
                # rem_repeat <id> off  | rem_repeat <id> <mins>
                rid = int(rest[0])
                val = rest[1].lower()
                if val == "off":
                    storage.update_reminder_repeat_minutes(rid, None)
                    write("Répétition désactivée.")
                else:
                    mins = int(val)
                    storage.update_reminder_repeat_minutes(rid, mins)
                    write(f"Répétition: {mins} minutes.")


            # -------- routines: enable/disable / delete / edit --------

            elif op == "rt_del":
                tid = int(rest[0])
                storage.delete_routine(tid)
                write("Routine supprimée.")

            elif op == "rt_enable":
                tid = int(rest[0])
                storage.set_routine_enabled(tid, True)
                write("Routine activée.")

            elif op == "rt_disable":
                tid = int(rest[0])
                storage.set_routine_enabled(tid, False)
                write("Routine désactivée.")

            elif op == "rt_title":
                tid = int(rest[0])
                title = " ".join(rest[1:]).strip()
                storage.update_routine_title(tid, title)
                write("Titre routine modifié.")

            elif op == "rt_note":
                tid = int(rest[0])
                note = " ".join(rest[1:]).strip()
                storage.update_routine_note(tid, note)
                write("Note routine modifiée.")

            elif op == "rt_time":
                tid = int(rest[0])
                hhmm = rest[1]
                storage.update_routine_time(tid, hhmm)
                write(f"Heure routine modifiée: {hhmm}")



            else:
                write("Unknown command. Type 'help'.")
        except Exception as e:
            write(f"Error: {e}")

    def on_enter(_evt=None) -> None:
        cmd = entry.get()
        entry.delete(0, tk.END)
        write("> " + cmd)
        handle_command(cmd)

    entry.bind("<Return>", on_enter)

    if prefill:
        write(prefill)
        write("")

    for line in HELP_TEXT.splitlines():
        write(line)
    write("")
    write("Type 'quit' to close this window.")

    refresh_recent()
    entry.focus_set()
    root.mainloop()


# -------------------------
# Service (background)
# -------------------------

def send_notification(
    title: str,
    message: str,
    sound: str = "beep",
    blink_seconds: int = 8,
    screen: int = 1,
    position: str = "top_right",
    force_foreground: bool = True,
) -> None:
    url = "http://127.0.0.1:17333/notify"
    payload = {
        "title": title,
        "message": message,
        "sound": sound,
        "blink_seconds": blink_seconds,
        "screen": screen,
        "position": position,
        "force_foreground": force_foreground,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=1.5).close()
    except Exception:
        print(f"[NOTIFY FALLBACK] {title}: {message}")


def is_ui_already_open(lock_path: Optional[str] = None) -> bool:
    lock_path = lock_path or get_lock_path()
    # Simple lock file approach for POC.
    # If lock exists and is "fresh", assume UI is open.
    if not os.path.exists(lock_path):
        return False
    try:
        age = time.time() - os.path.getmtime(lock_path)
        return age < 60.0  # updated by UI once a second
    except Exception:
        return False


def run_service() -> None:
    last_nag = 0.0
    nag_every_seconds = 60.0
    storage = Storage()
    storage.enable_all_reminders()
    storage.enable_all_routines()
    engine = Engine(storage)

    print("Assistant service running. (Ctrl+C to stop)")
    while True:
        notes = engine.tick()

        # Persistent nags while there are unacknowledged alerts
        now_t = time.time()
        if now_t - last_nag >= nag_every_seconds:
            unacked = storage.get_unacked_alerts()
            if unacked:
                last_nag = now_t
                preview = "\n".join([f"[{aid}] {msg}" for (aid, _ts, _src, msg) in unacked[:3]])
                if len(unacked) > 3:
                    preview += f"\n(+{len(unacked)-3} more)"
                send_notification("Pending alerts (ack needed)", preview)

                if not is_ui_already_open():
                    subprocess.Popen(get_self_launch_cmd() + ["--ui"])


        if notes:
            # Create one alert per note (so you can acknowledge individually)
            for n in notes:
                storage.add_alert(n, source="reminder")  # or "routine" if you want to distinguish

            # Send a notification right away
            msg = "\n".join(notes[:3]) + ("" if len(notes) <= 3 else f"\n(+{len(notes)-3} more)")
            send_notification("Personal Assistant", msg)

            if not is_ui_already_open():
                subprocess.Popen(get_self_launch_cmd() + ["--ui"])


        time.sleep(2.0)


def run_ui_mode(prefill: str) -> None:
    # Update lock file periodically so service knows UI is open
    lock_path = get_lock_path()
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write("open")

    # Background thread to "touch" lock file
    import threading

    stop = False

    def toucher() -> None:
        while not stop:
            try:
                os.utime(lock_path, None)
            except Exception:
                pass
            time.sleep(1.0)

    t = threading.Thread(target=toucher, daemon=True)
    t.start()

    try:
        run_ui_popup(prefill=prefill)
    finally:
        stop = True
        try:
            os.remove(lock_path)
        except Exception:
            pass

def run_tray() -> None:
    runner = ServiceRunner()
    runner.start()

    def on_open(_icon, _item) -> None:
        subprocess.Popen(get_self_launch_cmd() + ["--ui"])

    def on_quit(icon, _item) -> None:
        runner.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open UI", on_open),
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("TimeGuard", make_tray_icon(), "TimeGuard", menu)
    icon.run()

# -------------------------
# Entrypoint
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", action="store_true", help="Run background reminder service")
    ap.add_argument("--ui", action="store_true", help="Run UI popup window")
    ap.add_argument("--prefill", type=str, default="", help="Prefill message for UI")
    ap.add_argument("--tray", action="store_true", help="Run tray daemon")
    args = ap.parse_args()

    if args.service:
        run_service()
        return

    if args.ui:
        run_ui_mode(args.prefill)
        return

    if args.tray:
        run_tray()
        return

    # Default: open UI
    run_tray()


if __name__ == "__main__":
    main()
