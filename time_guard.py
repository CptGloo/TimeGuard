from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import pystray
from PIL import Image, ImageDraw


def get_app_dir() -> str:
    # Windows-friendly app data folder
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "TimeGuard")
    else:
        path = os.path.join(os.path.expanduser("~"), ".timeguard")
    os.makedirs(path, exist_ok=True)
    return path


def get_db_path() -> str:
    return os.path.join(get_app_dir(), "assistant.db")


def get_lock_path() -> str:
    return os.path.join(get_app_dir(), "assistant_ui.lock")


def get_self_launch_cmd() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(sys.argv[0])]


def make_tray_icon() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, size - 8, size - 8), fill=(40, 40, 40, 255))
    d.rectangle((30, 18, 34, 46), fill=(255, 255, 255, 255))
    d.rectangle((30, 50, 34, 54), fill=(255, 255, 255, 255))
    return img


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


def next_occurrence_at(hhmm: str, now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now()
    h, m = parse_hhmm(hhmm)
    candidate = datetime.combine(now.date(), datetime.min.time()).replace(hour=h, minute=m, second=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# -------------------------
# Data model
# -------------------------

@dataclass(frozen=True)
class Cycle:
    name: str
    start_time_hhmm: str
    enabled: bool
    auto_enable: bool
    auto_trigger: bool
    next_due: datetime
    current_index: int
    waiting_ack: bool


@dataclass(frozen=True)
class CycleTask:
    index: int
    minutes_after_ack: int
    message: str


# -------------------------
# Storage (SQLite)
# -------------------------

class Storage:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or get_db_path()
        self._reset_if_legacy_schema()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()
        self._reset_if_schema_mismatch()

    def _reset_if_legacy_schema(self) -> None:
        # Start fresh: if we detect any legacy tables, back up the DB and recreate.
        if not os.path.exists(self._db_path):
            return
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                legacy_markers = {"reminders", "routines", "activity_log"}
                if tables.intersection(legacy_markers):
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup = self._db_path + f".legacy_{ts}.bak"
                    conn.close()
                    conn = None
                    os.replace(self._db_path, backup)
            finally:
                if conn is not None:
                    conn.close()
        except Exception:
            return

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cycles (
                name TEXT PRIMARY KEY,
                start_time_hhmm TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_enable INTEGER NOT NULL DEFAULT 0,
                auto_trigger INTEGER NOT NULL DEFAULT 0,
                next_due TEXT NOT NULL,
                current_index INTEGER NOT NULL DEFAULT 0,
                waiting_ack INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cycle_tasks (
                cycle_name TEXT NOT NULL,
                task_index INTEGER NOT NULL,
                minutes_after_ack INTEGER NOT NULL,
                message TEXT NOT NULL,
                PRIMARY KEY (cycle_name, task_index),
                FOREIGN KEY (cycle_name) REFERENCES cycles(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS active_alerts (
                alert_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL,           -- cycle name
                message TEXT NOT NULL,
                source TEXT NOT NULL,         -- cycle
                acknowledged INTEGER NOT NULL DEFAULT 0,
                meta_json TEXT NOT NULL       -- {"task_index":int}
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Per-cycle pause info used by snooze (stores remaining time until due)
            CREATE TABLE IF NOT EXISTS cycle_pause (
                cycle_name TEXT PRIMARY KEY,
                remaining_seconds INTEGER NOT NULL,
                FOREIGN KEY (cycle_name) REFERENCES cycles(name) ON DELETE CASCADE
            );

CREATE INDEX IF NOT EXISTS idx_active_alerts_ack
            ON active_alerts(acknowledged, created_at);

            CREATE INDEX IF NOT EXISTS idx_active_alerts_name
            ON active_alerts(acknowledged, name, created_at);
            """
        )
        self._conn.commit()

    def _reset_if_schema_mismatch(self) -> None:
        """Start fresh if the DB exists but doesn't match our expected schema."""
        try:
            cols = [
                r[1]
                for r in self._conn.execute("PRAGMA table_info(cycles)").fetchall()
            ]
            required = {
                "name",
                "start_time_hhmm",
                "enabled",
                "auto_enable",
                "auto_trigger",
                "next_due",
                "current_index",
                "waiting_ack",
            }
            if cols and not required.issubset(set(cols)):
                # Close current connection, back up DB, recreate from scratch.
                try:
                    self._conn.close()
                except Exception:
                    pass

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = self._db_path + f".schema_{ts}.bak"
                try:
                    os.replace(self._db_path, backup)
                except Exception:
                    # If we can't back it up, still try to remove it.
                    try:
                        os.remove(self._db_path)
                    except Exception:
                        return

                self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA foreign_keys=ON;")
                self._init_schema()
        except Exception:
            # If anything looks off, do nothing here. (We already start fresh on legacy markers.)
            return

    # ---- cycles ----

    def create_cycle(
        self,
        name: str,
        start_time_hhmm: str,
        first_minutes: int,
        first_message: str,
        auto_enable: bool = False,
        auto_trigger: bool = False,
    ) -> None:
        start_dt = next_occurrence_at(start_time_hhmm)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO cycles(
                name, start_time_hhmm, enabled, auto_enable, auto_trigger, next_due, current_index, waiting_ack
            )
            VALUES (?, ?, 1, ?, ?, ?, 0, 0)
            """,
            (
                name,
                start_time_hhmm,
                1 if auto_enable else 0,
                1 if auto_trigger else 0,
                start_dt.isoformat(timespec="seconds"),
            ),
        )
        self._conn.execute("DELETE FROM cycle_tasks WHERE cycle_name=?", (name,))
        self._conn.execute(
            """
            INSERT INTO cycle_tasks(cycle_name, task_index, minutes_after_ack, message)
            VALUES (?, 0, ?, ?)
            """,
            (name, int(first_minutes), first_message),
        )
        self._conn.commit()

    def add_task(self, cycle_name: str, minutes_after_ack: int, message: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(task_index), -1) FROM cycle_tasks WHERE cycle_name=?",
            (cycle_name,),
        ).fetchone()
        next_index = int(row[0]) + 1
        self._conn.execute(
            """
            INSERT INTO cycle_tasks(cycle_name, task_index, minutes_after_ack, message)
            VALUES (?, ?, ?, ?)
            """,
            (cycle_name, next_index, int(minutes_after_ack), message),
        )
        self._conn.commit()
        return next_index

    def delete_cycle(self, name: str) -> None:
        self._conn.execute("DELETE FROM cycles WHERE name=?", (name,))
        self._conn.commit()

    def set_cycle_enabled(self, name: str, enabled: bool) -> None:
        self._conn.execute("UPDATE cycles SET enabled=? WHERE name=?", (1 if enabled else 0, name))
        self._conn.commit()

    def trigger_cycle(self, name: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE cycles SET next_due=?, waiting_ack=0 WHERE name=?",
            (now, name),
        )
        self._conn.commit()

    def list_cycles(self) -> List[Cycle]:
        cur = self._conn.execute(
            """
            SELECT name, start_time_hhmm, enabled, auto_enable, auto_trigger,
                   next_due, current_index, waiting_ack
            FROM cycles
            ORDER BY name ASC
            """
        )
        out: List[Cycle] = []
        for name, hhmm, enabled, auto_enable, auto_trigger, next_due, idx, waiting_ack in cur.fetchall():
            out.append(
                Cycle(
                    name=name,
                    start_time_hhmm=hhmm,
                    enabled=bool(enabled),
                    auto_enable=bool(auto_enable),
                    auto_trigger=bool(auto_trigger),
                    next_due=datetime.fromisoformat(next_due),
                    current_index=int(idx),
                    waiting_ack=bool(waiting_ack),
                )
            )
        return out

    def get_cycle(self, name: str) -> Cycle:
        row = self._conn.execute(
            """
            SELECT name, start_time_hhmm, enabled, auto_enable, auto_trigger,
                   next_due, current_index, waiting_ack
            FROM cycles
            WHERE name=?
            """,
            (name,),
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown cycle '{name}'")
        return Cycle(
            name=row[0],
            start_time_hhmm=row[1],
            enabled=bool(row[2]),
            auto_enable=bool(row[3]),
            auto_trigger=bool(row[4]),
            next_due=datetime.fromisoformat(row[5]),
            current_index=int(row[6]),
            waiting_ack=bool(row[7]),
        )

    def set_cycle_auto_enable(self, name: str, value: bool) -> None:
        self._conn.execute(
            "UPDATE cycles SET auto_enable=? WHERE name=?",
            (1 if value else 0, name),
        )
        self._conn.commit()

    def set_cycle_auto_trigger(self, name: str, value: bool) -> None:
        self._conn.execute(
            "UPDATE cycles SET auto_trigger=? WHERE name=?",
            (1 if value else 0, name),
        )
        self._conn.commit()

    def apply_autostart(self) -> None:
        """Apply per-cycle auto_* behavior once at app start."""
        now_iso = datetime.now().isoformat(timespec="seconds")

        # auto_enable: just ensure enabled.
        self._conn.execute("UPDATE cycles SET enabled=1 WHERE auto_enable=1")

        # auto_trigger: enable AND set next_due to now to fire on next tick.
        self._conn.execute(
            """
            UPDATE cycles
            SET enabled=1, next_due=?, waiting_ack=0
            WHERE auto_trigger=1
            """,
            (now_iso,),
        )
        self._conn.commit()

        

    # ---- snooze (global) ----

    def get_snoozed_until(self) -> Optional[datetime]:
        row = self._conn.execute(
            "SELECT value FROM app_state WHERE key='snoozed_until'"
        ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except Exception:
            return None

    def is_snoozed(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        until = self.get_snoozed_until()
        return bool(until and until > now)

    def set_snooze(self, minutes: int) -> datetime:
        """Snooze notifications and pause READY timers for N minutes.

        Key behavior:
        - DOES NOT modify cycles in WAIT_ACK (they still require your ack).
        - Pauses only READY timers by storing the remaining time until due.
        - Suppresses notifications while snoozed (engine returns no notes).
        - On unsnooze (or snooze expiry), READY timers resume with the same remaining time.
        """
        minutes = int(minutes)
        if minutes <= 0:
            raise ValueError("Snooze minutes must be > 0")

        now = datetime.now()
        delta = timedelta(minutes=minutes)

        current_until = self.get_snoozed_until()
        base = current_until if (current_until and current_until > now) else now
        until = base + delta

        # If we're entering snooze (not extending), snapshot remaining time for READY cycles.
        entering = not (current_until and current_until > now)
        if entering:
            for c in self.list_cycles():
                if not c.enabled:
                    continue
                if c.waiting_ack:
                    continue  # keep WAIT_ACK intact
                remaining = max(0, int((c.next_due - now).total_seconds()))
                self._conn.execute(
                    "INSERT INTO cycle_pause(cycle_name, remaining_seconds) VALUES(?, ?) "
                    "ON CONFLICT(cycle_name) DO UPDATE SET remaining_seconds=excluded.remaining_seconds",
                    (c.name, remaining),
                )

        # Store / update global snooze marker
        self._conn.execute(
            "INSERT INTO app_state(key, value) VALUES('snoozed_until', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (until.isoformat(timespec="seconds"),),
        )

        self._conn.commit()
        return until

    def clear_snooze(self) -> None:
        """Clear snooze and resume READY timers using saved remaining time."""
        now = datetime.now()

        # Resume paused READY cycles (WAIT_ACK cycles were never changed)
        cur = self._conn.execute("SELECT cycle_name, remaining_seconds FROM cycle_pause")
        for cycle_name, remaining_seconds in cur.fetchall():
            # Only adjust if the cycle is currently READY
            try:
                c = self.get_cycle(cycle_name)
            except Exception:
                continue
            if c.waiting_ack:
                continue
            new_due = now + timedelta(seconds=int(remaining_seconds))
            self._conn.execute(
                "UPDATE cycles SET next_due=? WHERE name=?",
                (new_due.isoformat(timespec="seconds"), cycle_name),
            )

        self._conn.execute("DELETE FROM cycle_pause")
        self._conn.execute("DELETE FROM app_state WHERE key='snoozed_until'")
        self._conn.commit()

    def maybe_restore_expired_snooze(self) -> None:
        """If snooze has expired but pause data still exists, restore timers once."""
        until = self.get_snoozed_until()
        if not until:
            return
        if until > datetime.now():
            return
        # Expired: restore and clear markers
        self.clear_snooze()

    def list_tasks(self, cycle_name: str) -> List[CycleTask]:
        cur = self._conn.execute(
            """
            SELECT task_index, minutes_after_ack, message
            FROM cycle_tasks
            WHERE cycle_name=?
            ORDER BY task_index ASC
            """,
            (cycle_name,),
        )
        return [CycleTask(index=int(i), minutes_after_ack=int(m), message=msg) for (i, m, msg) in cur.fetchall()]

    def reset_cycle(self, name: str) -> None:
        """Reset a cycle to step 0 and put it into WAIT_ACK.

        Semantics: behaves as if step 0 just fired. You must `ack <name>` to start timing again.
        """
        now_iso = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """
            UPDATE cycles
            SET current_index=0,
                waiting_ack=1,
                next_due=?
            WHERE name=?
            """,
            (now_iso, name),
        )
        self._conn.commit()

    def redo_cycle(self, name: str, task_index: Optional[int] = None) -> int:
        """Redo a task inside a cycle.

        - If task_index is None: redo the current task.
        - Otherwise: set the cycle to that task index.
        In both cases the cycle is put into WAIT_ACK and next_due is set to now.

        Returns the task index that will be redone.
        """
        c = self.get_cycle(name)
        tasks = self.list_tasks(name)
        if not tasks:
            raise ValueError(f"Cycle '{name}' has no tasks.")

        valid = {t.index for t in tasks}
        idx = c.current_index if task_index is None else int(task_index)
        if idx not in valid:
            raise ValueError(f"Invalid task index {idx} for cycle '{name}'. Valid: {sorted(valid)}")

        # A redo supersedes older pending alerts for the same cycle.
        self.ack_alerts_by_name(name)

        now_iso = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """
            UPDATE cycles
            SET current_index=?,
                waiting_ack=1,
                next_due=?
            WHERE name=?
            """,
            (idx, now_iso, name),
        )
        self._conn.commit()
        return idx

# ---- alerts ----

    def add_alert(self, name: str, message: str, source: str, meta: dict) -> str:
        alert_key = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """
            INSERT INTO active_alerts(alert_key, created_at, name, message, source, acknowledged, meta_json)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (alert_key, now, name, message, source, json.dumps(meta, separators=(",", ":"))),
        )
        self._conn.commit()
        return alert_key

    def get_unacked_alerts(self) -> List[Tuple[str, str, str, str]]:
        cur = self._conn.execute(
            "SELECT name, created_at, source, message FROM active_alerts WHERE acknowledged=0 ORDER BY created_at ASC"
        )
        return list(cur.fetchall())

    def ack_all_alerts(self) -> int:
        cur = self._conn.execute("UPDATE active_alerts SET acknowledged=1 WHERE acknowledged=0")
        self._conn.commit()
        return int(cur.rowcount)

    def ack_alerts_by_name(self, name: str) -> int:
        cur = self._conn.execute(
            "UPDATE active_alerts SET acknowledged=1 WHERE acknowledged=0 AND name=?",
            (name,),
        )
        self._conn.commit()
        return int(cur.rowcount)

    def advance_cycle_on_ack(self, name: str, ack_time: Optional[datetime] = None) -> None:
        ack_time = ack_time or datetime.now()
        c = self.get_cycle(name)
        if not c.waiting_ack:
            return

        tasks = self.list_tasks(name)
        if not tasks:
            return

        current_task = next((t for t in tasks if t.index == c.current_index), tasks[0])
        next_index = (c.current_index + 1) % len(tasks)
        next_due = ack_time + timedelta(minutes=current_task.minutes_after_ack)

        self._conn.execute(
            """
            UPDATE cycles
            SET current_index=?, next_due=?, waiting_ack=0
            WHERE name=?
            """,
            (next_index, next_due.isoformat(timespec="seconds"), name),
        )
        self._conn.commit()

    def mark_cycle_waiting_ack(self, name: str) -> None:
        self._conn.execute("UPDATE cycles SET waiting_ack=1 WHERE name=?", (name,))
        self._conn.commit()


# -------------------------
# Engine
# -------------------------

class Engine:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def tick(self) -> List[Tuple[str, str, str, dict]]:
        now = datetime.now()
        if self._storage.is_snoozed(now):
            return []
        out: List[Tuple[str, str, str, dict]] = []

        for c in self._storage.list_cycles():
            if not c.enabled:
                continue
            if c.waiting_ack:
                continue
            if c.next_due > now:
                continue

            tasks = self._storage.list_tasks(c.name)
            if not tasks:
                continue

            task = next((t for t in tasks if t.index == c.current_index), tasks[0])
            out.append((c.name, "cycle", task.message, {"task_index": task.index}))
            self._storage.mark_cycle_waiting_ack(c.name)

        return out


# -------------------------
# Notifications + UI
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
    try:
        import urllib.request
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=1.5).close()
    except Exception:
        print(f"[NOTIFY FALLBACK] {title}: {message}")


def is_ui_already_open(lock_path: Optional[str] = None) -> bool:
    lock_path = lock_path or get_lock_path()
    if not os.path.exists(lock_path):
        return False
    try:
        age = time.time() - os.path.getmtime(lock_path)
        return age < 60.0
    except Exception:
        return False


HELP_TEXT = """
Commands:
  cycle <name> at <HH:MM> <mins> <message...> [auto_enable] [auto_trigger]
      Create or replace a cycle. First alert happens at the next HH:MM.
      After you ack, the next alert is scheduled <mins> minutes later.

  task <cycle_name> <mins> <message...>
      Append a task to a cycle. After you ack a task, the next one is scheduled <mins> minutes later.

  trigger <cycle_name>
      Trigger the current task immediately (manual start / manual poke).

  list
      List cycles and their tasks.

  enable <cycle_name>
  disable <cycle_name>
  del <cycle_name>
  reset <cycle_name>
      Reset the cycle to step 0 and put it into WAIT_ACK (as if step 0 just fired).

  redo <cycle_name> [task_index]
      Redo a task and put the cycle into WAIT_ACK.
      - Without task_index: redo the current task.
      - With task_index: redo that specific step number (e.g. redo work 3).



  snooze <minutes>
      Snooze everything for N minutes:
      - stops alerts from popping (including pending-alert nags)
      - pauses READY timers (WAIT_ACK cycles are not changed)
      - timers resume on unsnooze or when snooze expires

  unsnooze
      End snooze now and resume paused READY timers.

  auto_enable <cycle_name> on|off
      If ON: at app start, this cycle is forced enabled.

  auto_trigger <cycle_name> on|off
      If ON: at app start, this cycle is forced enabled AND triggered immediately.

  alerts
      Show pending alerts.

  ack <cycle_name>
      Acknowledge the current alert(s) for that cycle AND start the timer for the next task.

  ack_all
      Acknowledge all alerts (does NOT advance cycles).

  help
  quit

Example:
  cycle project at 06:00 90 Start by opening the files of the project
  task project 15 now we work on the refactoring
  task project 30 now i want to do a set of pushup
  task project 30 now we implement the design
  auto_trigger project on
  trigger project
  ack project
""".strip()


def run_ui_popup(prefill: str = "") -> None:
    import tkinter as tk
    from tkinter import scrolledtext

    storage = Storage()

    root = tk.Tk()
    root.title("TimeGuard")
    root.geometry("760x520")
    root.bell()

    output = scrolledtext.ScrolledText(root, wrap=tk.WORD)
    output.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    entry = tk.Entry(root)
    entry.pack(fill=tk.X, padx=8, pady=(0, 8))

    def write(line: str) -> None:
        output.insert(tk.END, line + "\n")
        output.see(tk.END)

    def print_list() -> None:
        write("— Cycles —")
        snoozed_until = storage.get_snoozed_until()
        if snoozed_until and snoozed_until > datetime.now():
            remaining = int((snoozed_until - datetime.now()).total_seconds() // 60)
            write(f"[GLOBAL] SNOOZED until {format_dt(snoozed_until)} (~{remaining}m remaining)")
        
        cycles = storage.list_cycles()
        if not cycles:
            write("(none)")
            write("")
            return
        for c in cycles:
            on = "ON" if c.enabled else "OFF"
            waiting = "WAIT_ACK" if c.waiting_ack else "READY"
            ae = "AE" if c.auto_enable else "-"
            at = "AT" if c.auto_trigger else "-"
            write(
                f"[{c.name}] {on} {ae}/{at} start_at={c.start_time_hhmm} next_due={format_dt(c.next_due)} state={waiting} current={c.current_index}"
            )
            tasks = storage.list_tasks(c.name)
            for t in tasks:
                write(f"  - step {t.index}: +{t.minutes_after_ack}m  {t.message}")
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

            elif op in ("quit", "exit"):
                root.destroy()

            elif op == "list":
                print_list()

            elif op == "cycle":
                if len(rest) < 5 or rest[1].lower() != "at":
                    raise ValueError("Usage: cycle <name> at <HH:MM> <mins> <message...> [auto_enable] [auto_trigger]")
                name = rest[0]
                hhmm = rest[2]
                _ = parse_hhmm(hhmm)
                mins = int(rest[3])
                tail = rest[4:]

                # Optional trailing flags: auto_enable / auto_trigger
                flag_tokens = {"auto_enable", "--auto_enable", "auto_trigger", "--auto_trigger"}
                found = {t.lower() for t in tail if t.lower() in flag_tokens}
                auto_enable = ("auto_enable" in found) or ("--auto_enable" in found)
                auto_trigger = ("auto_trigger" in found) or ("--auto_trigger" in found)
                msg_tokens = [t for t in tail if t.lower() not in flag_tokens]
                msg = " ".join(msg_tokens).strip()
                if not msg:
                    raise ValueError("Message required.")
                storage.create_cycle(
                    name=name,
                    start_time_hhmm=hhmm,
                    first_minutes=mins,
                    first_message=msg,
                    auto_enable=auto_enable,
                    auto_trigger=auto_trigger,
                )
                flags = []
                if auto_enable:
                    flags.append("auto_enable")
                if auto_trigger:
                    flags.append("auto_trigger")
                extra = (" [" + ", ".join(flags) + "]") if flags else ""
                write(f"Created cycle [{name}] at {hhmm}. First delay after ack: {mins}m.{extra}")

            elif op == "task":
                if len(rest) < 3:
                    raise ValueError("Usage: task <cycle_name> <mins> <message...>")
                name = rest[0]
                mins = int(rest[1])
                msg = " ".join(rest[2:]).strip()
                idx = storage.add_task(name, mins, msg)
                write(f"Added task to [{name}] as step {idx} (+{mins}m).")

            elif op == "trigger":
                if len(rest) != 1:
                    raise ValueError("Usage: trigger <cycle_name>")
                name = rest[0]
                storage.trigger_cycle(name)
                write(f"Triggered [{name}] (next tick will fire it).")

            elif op == "enable":
                if len(rest) != 1:
                    raise ValueError("Usage: enable <cycle_name>")
                storage.set_cycle_enabled(rest[0], True)
                write("Enabled.")

            elif op == "disable":
                if len(rest) != 1:
                    raise ValueError("Usage: disable <cycle_name>")
                storage.set_cycle_enabled(rest[0], False)
                write("Disabled.")

            elif op == "del":
                if len(rest) != 1:
                    raise ValueError("Usage: del <cycle_name>")
                storage.delete_cycle(rest[0])
                write("Deleted.")

            elif op == "reset":
                if len(rest) != 1:
                    raise ValueError("Usage: reset <cycle_name>")
                name = rest[0]
                storage.reset_cycle(name)

                tasks = storage.list_tasks(name)
                task0 = next((t for t in tasks if t.index == 0), tasks[0])
                storage.add_alert(name=name, message=task0.message, source="cycle", meta={"task_index": task0.index, "reset": True})
                send_notification("TimeGuard (reset)", f"[{name}] step {task0.index}: {task0.message}", blink_seconds=12, force_foreground=True)

                write(f"Cycle [{name}] reset to step 0 (WAIT_ACK).")

            elif op == "redo":
                if len(rest) not in (1, 2):
                    raise ValueError("Usage: redo <cycle_name> [task_index]")
                name = rest[0]
                idx = int(rest[1]) if len(rest) == 2 else None
                chosen = storage.redo_cycle(name, idx)

                tasks = storage.list_tasks(name)
                task = next((t for t in tasks if t.index == chosen), tasks[0])
                storage.add_alert(name=name, message=task.message, source="cycle", meta={"task_index": task.index, "redo": True})
                send_notification("TimeGuard (redo)", f"[{name}] step {task.index}: {task.message}", blink_seconds=12, force_foreground=True)

                write(f"Cycle [{name}] redo step {chosen} (WAIT_ACK).")


            elif op == "snooze":
                if len(rest) != 1:
                    raise ValueError("Usage: snooze <minutes>")
                mins = int(rest[0])
                until = storage.set_snooze(mins)
                send_notification("TimeGuard (snoozed)", f"Snoozed for {mins} min (until {format_dt(until)}).", blink_seconds=8, force_foreground=False)
                write(f"Snoozed everything for {mins} minutes (until {format_dt(until)}).")

            elif op == "unsnooze":
                storage.clear_snooze()
                write("Snooze cleared.")

            elif op == "auto_enable":
                if len(rest) != 2 or rest[1].lower() not in ("on", "off"):
                    raise ValueError("Usage: auto_enable <cycle_name> on|off")
                name = rest[0]
                val = rest[1].lower() == "on"
                storage.set_cycle_auto_enable(name, val)
                write(f"auto_enable for [{name}] set to {'ON' if val else 'OFF'}.")

            elif op == "auto_trigger":
                if len(rest) != 2 or rest[1].lower() not in ("on", "off"):
                    raise ValueError("Usage: auto_trigger <cycle_name> on|off")
                name = rest[0]
                val = rest[1].lower() == "on"
                storage.set_cycle_auto_trigger(name, val)
                write(f"auto_trigger for [{name}] set to {'ON' if val else 'OFF'}.")

            elif op == "alerts":
                rows = storage.get_unacked_alerts()
                if not rows:
                    write("No pending alerts.")
                else:
                    write("— Pending alerts —")
                    for name, ts, src, msg in rows:
                        write(f"[{name}] {ts} ({src}) {msg}")
                    write("Use: ack <cycle_name> | ack_all")

            elif op == "ack":
                if len(rest) != 1:
                    raise ValueError("Usage: ack <cycle_name>")
                name = rest[0]
                n = storage.ack_alerts_by_name(name)
                storage.advance_cycle_on_ack(name)
                write(f"Acknowledged {n} alert(s) for '{name}'. Next task scheduled from now.")

            elif op == "ack_all":
                n = storage.ack_all_alerts()

                # Advance all cycles that were waiting for acknowledgement
                for c in storage.list_cycles():
                    if c.waiting_ack:
                        storage.advance_cycle_on_ack(c.name)

                write(f"Acknowledged {n} alert(s). Advanced waiting cycles.")

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
    write("")
    print_list()
    entry.focus_set()
    root.mainloop()


# -------------------------
# Background service
# -------------------------

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
        storage.apply_autostart()
        engine = Engine(storage)

        while not self._stop.is_set():
            storage.maybe_restore_expired_snooze()
            notes = engine.tick()
            now_t = time.time()

            if now_t - last_nag >= nag_every_seconds and not storage.is_snoozed():
                unacked = storage.get_unacked_alerts()
                if unacked:
                    last_nag = now_t
                    preview = "\n".join([f"{name}: {msg}" for (name, _ts, _src, msg) in unacked[:3]])
                    if len(unacked) > 3:
                        preview += f"\n(+{len(unacked)-3} more)"
                    send_notification("Pending alerts (ack needed)", preview, blink_seconds=12, force_foreground=True)

            if notes:
                for name, source, message, meta in notes:
                    storage.add_alert(name=name, message=message, source=source, meta=meta)

                msg = "\n".join([m for (_n, _s, m, _meta) in notes[:3]]) + (
                    "" if len(notes) <= 3 else f"\n(+{len(notes)-3} more)"
                )
                send_notification("TimeGuard", msg, blink_seconds=12, force_foreground=True)

                if not is_ui_already_open():
                    subprocess.Popen(get_self_launch_cmd() + ["--ui"])

            time.sleep(2.0)


def run_service() -> None:
    last_nag = 0.0
    nag_every_seconds = 60.0

    storage = Storage()
    storage.apply_autostart()
    engine = Engine(storage)

    print("TimeGuard service running. (Ctrl+C to stop)")
    while True:
        storage.maybe_restore_expired_snooze()
        notes = engine.tick()

        now_t = time.time()
        if now_t - last_nag >= nag_every_seconds and not storage.is_snoozed():
            unacked = storage.get_unacked_alerts()
            if unacked:
                last_nag = now_t
                preview = "\n".join([f"{name}: {msg}" for (name, _ts, _src, msg) in unacked[:3]])
                if len(unacked) > 3:
                    preview += f"\n(+{len(unacked)-3} more)"
                send_notification("Pending alerts (ack needed)", preview)
                if not is_ui_already_open():
                    subprocess.Popen(get_self_launch_cmd() + ["--ui"])

        if notes:
            for name, source, message, meta in notes:
                storage.add_alert(name=name, message=message, source=source, meta=meta)
            msg = "\n".join([m for (_n, _s, m, _meta) in notes[:3]]) + (
                "" if len(notes) <= 3 else f"\n(+{len(notes)-3} more)"
            )
            send_notification("TimeGuard", msg)
            if not is_ui_already_open():
                subprocess.Popen(get_self_launch_cmd() + ["--ui"])

        time.sleep(2.0)


def run_ui_mode(prefill: str) -> None:
    lock_path = get_lock_path()
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write("open")

    stop_flag = {"stop": False}

    def toucher() -> None:
        while not stop_flag["stop"]:
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
        stop_flag["stop"] = True
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", action="store_true", help="Run background service")
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

    run_tray()


if __name__ == "__main__":
    main()
