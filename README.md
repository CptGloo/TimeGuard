# TimeGuard

TimeGuard is a **podomoro cycle manager** designed without cloud sync, or noisy abstractions.

It runs as a **background service + tray app** and lets you define **cycles** made of sequential tasks that only advance when *you acknowledge them*.

It uses this specific notifier to alert the user of the next task :
https://github.com/CptGloo/notifier_daemon

This project intentionally starts **fresh** (no legacy compatibility) and favors:
- explicit commands
- predictable state transitions
- local-first storage

---

## Core Concepts

### Cycle
A **cycle** is a named routine composed of ordered steps or tasks.

Each step:
- shows an alert
- waits for your acknowledgment (`ACK`)
- schedules the next step

Example mental model:
```
Alert → WAIT_ACK → ack → timer starts → next alert
```

---

### Task (Step)
A task belongs to a cycle and has:
- a delay (minutes after ack)
- a message

Tasks loop forever in order.

---

### State machine (important)
Each cycle is always in one of two states:

- `READY`
  - waiting for `next_due`
- `WAIT_ACK`
  - alert has fired
  - user must acknowledge

There is **no automatic progression**.

---

## Installation

### Requirements
- Python 3.10+

### Run directly
```bash
python time_guard.py --tray
```

This starts:
- background service
- tray icon
- notification engine

### Build executable (Windows)
```bash
pyinstaller --noconsole --onefile time_guard.py --collect-all uvicorn --collect-all fastapi --collect-all starlette --collect-all anyio
```

---

## Commands

### Create a cycle
```text
cycle <name> at <HH:MM> <mins> <message...> [auto_enable] [auto_trigger]
```

Example:
```text
cycle travail at 06:00 25 Examens Studi auto_enable
```

---

### Add a task
```text
task <cycle_name> <mins> <message...>
```

Example:
```text
task travail 10 pause
task travail 25 programmation / trading
```

---

### List cycles
```text
list
```

Output example:
```text
[studying] ON AE/- start_at=06:00 next_due=2026-02-05 10:55 state=WAIT_ACK current=0
  - step 0: +25m Examens
  - step 1: +10m pause
```

---

### Trigger a cycle manually
```text
trigger <cycle_name>
```

Forces the current step to fire immediately.

---

### Acknowledge alerts

Acknowledge one cycle:
```text
ack <cycle_name>
```

Acknowledge everything:
```text
ack_all
```

Acknowledging:
- clears `WAIT_ACK`
- schedules the next step **from ack time**

---

### Reset a cycle
```text
reset <cycle_name>
```

Effect:
- step → 0
- state → `WAIT_ACK`
- cycle behaves as if step 0 just fired

You must `ack` to restart timing.

---

### Enable / Disable
```text
enable <cycle_name>
disable <cycle_name>
```

---

### Auto start options

#### auto_enable
```text
auto_enable <cycle_name> on|off
```

If ON:
- cycle is enabled automatically at app start

#### auto_trigger
```text
auto_trigger <cycle_name> on|off
```

If ON:
- cycle is enabled
- cycle is triggered immediately at app start

---

### Alerts
```text
alerts
```

Shows all pending (unacked) alerts.

---

## Storage

- SQLite database
- Location:
  - Windows: `%APPDATA%/TimeGuard/assistant.db`
  - Linux/macOS: `~/.timeguard/assistant.db`

The database is:
- local-only
- automatically reset if schema mismatches

---