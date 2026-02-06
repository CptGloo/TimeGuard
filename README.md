# TimeGuard

TimeGuard is a **local, name-based time and cycle manager** designed for people who want strong control over routines without cloud sync, IDs, or noisy abstractions.

It runs as a **background service + tray app** and lets you define **cycles** made of sequential tasks that only advance when *you acknowledge them*.

This project intentionally starts **fresh** (no legacy compatibility) and favors:
- explicit commands
- predictable state transitions
- local-first storage

---

## Core Concepts

### Cycle
A **cycle** is a named routine composed of ordered steps (tasks).

Each step:
- shows an alert
- waits for your acknowledgment (`ACK`)
- only then schedules the next step

A cycle never advances silently.

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
- Windows, Linux, or macOS

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
[travail] ON AE/- start_at=06:00 next_due=2026-02-05 10:55 state=WAIT_ACK current=0
  - step 0: +25m Examens Studi
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
- never migrated intentionally

---

## Design Philosophy

TimeGuard is built around a few strict rules:

- **Names, not IDs**
- **Acknowledgment drives time**
- **No silent progression**
- **Local-first, offline-first**
- **Explicit over clever**

If something happens, it is because you either:
- acknowledged it
- triggered it
- reset it

Nothing else.

---

## Non-goals

- No cloud sync
- No mobile app
- No analytics
- No AI scheduling
- No habit gamification

This is a tool, not a coach.

---

## Roadmap ideas (optional)

- `reset_all`
- `jump <cycle> <step>`
- `pause <cycle>` / `resume <cycle>`
- export / import cycles

---

## License

MIT (or whatever you choose)

---

## Final note

If you like systems that:
- behave exactly as specified
- don’t surprise y