How to use it:

Open the UI window (manual)
python assistant_app.py --ui

Run the background service (the “real” assistant)
python assistant_app.py --service

Then create reminders inside the UI:

remind in 10m stand up

routine 22:30 prep for tomorrow

remind_repeat at 09:00 60 drink water

When something is due:

you’ll get a system notification

the window pops up automatically (only once; it won’t spam multiple windows)


pyinstaller --noconsole --onefile time_guard.py --collect-all uvicorn --collect-all fastapi --collect-all starlette --collect-all anyio