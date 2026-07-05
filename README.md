# FITR Workout Exporter

Export your own FITR Client workout history to files on your computer.

This tool opens a normal Chromium browser, lets you log in to FITR yourself, clicks through your own calendar, and saves the workouts it can see. It does not ask for your FITR password, store your password, bypass login, or try to access anyone else's account.

## Support And Compatibility

This project is provided **as is**. It is unsupported, and there is no guarantee of help, fixes, updates, or compatibility with future FITR changes.

The current parser was built and tested against **Operation LFG from coach Josh Bridges**. It has not been tested with other FITR programs, coaches, calendar formats, or workout layouts. Other programs may export partially, incorrectly, or not at all.

## What You Get

Each export creates a new timestamped folder under:

```text
fitr_output/export/
```

The most useful files are:

- `workouts.md` - the easiest file to read.
- `workouts.csv` - spreadsheet-friendly, good for Excel or Google Sheets.
- `workouts.json` - structured data for technical use.
- `raw_text/` - one text file per exported day.

Personal workout data is ignored by Git. The `.gitignore` excludes `fitr_output/`, `.fitr-browser-profile/`, virtual environments, caches, and common spreadsheet/data files.

## Quick Start For Windows

1. Install Python from <https://www.python.org/downloads/windows/>.
2. During Python install, check **Add python.exe to PATH**.
3. Download this project from GitHub.
   - Easiest option: click the green **Code** button, click **Download ZIP**, unzip the file, and open the unzipped folder.
   - Git option: if you already use Git, run `git clone <repo-url>` and open the cloned folder.
4. Double-click `setup_windows.bat`.
5. Double-click `export_workouts.bat`.
6. Enter your start and end dates when prompted.
7. Log in to FITR in the Chromium window if asked.
8. When the FITR calendar is visible, return to the command window and press Enter.
9. Open the newest folder in `fitr_output/export/`.
10. Open `workouts.md` or `workouts.csv`.

Date format must be:

```text
YYYY-MM-DD
```

Example:

```text
2025-09-15
2026-08-04
```

## What To Expect From The Batch Files

`setup_windows.bat` is the first-time setup file. It may take several minutes and it will download/install things in the project folder:

- a local Python environment in `.venv/`
- the Python packages listed in `requirements.txt`
- a Playwright-managed Chromium browser

You may see a lot of scrolling text while packages download and install. That is expected. You only need to run `setup_windows.bat` once per computer, unless you delete `.venv/` or move to a fresh copy of the project.

`export_workouts.bat` is the file you use after setup. It will:

1. Ask for a start date.
2. Ask for an end date.
3. Open Chromium.
4. Let you log in to FITR manually.
5. Wait until you press Enter in the command window.
6. Export the workouts it can see.

When entering dates, type only the date in this exact format:

```text
YYYY-MM-DD
```

Do not type words like `start`, `end`, slashes, or extra spaces.

Good examples:

```text
2025-09-15
2026-08-04
```

Bad examples:

```text
9/15/2025
Sept 15 2025
start 2025-09-15
```

## Normal Export Command

If you are comfortable with a terminal, you can run the exporter directly:

```powershell
python .\fitr_export.py --start-date 2025-09-15 --end-date 2026-08-04
```

The exporter stops when it reaches `Week 1, Day 1` by default. To keep going past that point:

```powershell
python .\fitr_export.py --start-date 2025-09-15 --end-date 2026-08-04 --no-stop-at-week1-day1
```

For a small test run:

```powershell
python .\fitr_export.py --start-date 2026-07-01 --end-date 2026-07-07 --max-workouts 3
```

## First-Time Setup By Terminal

The batch file does these steps for you, but they are listed here for reference:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

After setup, run:

```powershell
python .\fitr_export.py --start-date 2025-09-15 --end-date 2026-08-04
```

## How Login Works

The tool uses a local Chromium browser profile stored at:

```text
.fitr-browser-profile/
```

That means:

- You log in manually in the browser.
- The tool does not need your username or password.
- The tool does not store your password in code.
- Future runs usually stay logged in.
- Delete `.fitr-browser-profile/` if you want to force a fresh login.

## CSV Columns

`workouts.csv` includes separate columns for the FITR week and day:

```text
week_number
day_number
```

It also includes:

- `date`
- `title`
- `program_name`
- `day_header`
- `is_rest_day`
- `completion_status`
- `calendar_card_text`
- `workout_text`
- `notes`
- `raw_text_file`

The full workout text is in the `workout_text` column.

## What The Exporter Does

The default exporter uses the visible FITR calendar page:

1. Opens FITR calendar.
2. Waits for you to log in manually.
3. Visits calendar months from newest to oldest.
4. Clicks each visible workout or rest day in the date range.
5. Expands visible collapsed sections.
6. Omits video/media sections.
7. Keeps workout text, substitutions, and scales.
8. Records rest days as rest days.
9. Stops at Week 1 Day 1 unless told not to.
10. Writes Markdown, CSV, JSON, logs, and raw text files.

The tool is intentionally gentle. It uses visible browser actions and delays instead of aggressive request loops.

## Important Privacy Notes

Do not commit or share:

- `fitr_output/`
- `.fitr-browser-profile/`
- exported `.csv`, `.xlsx`, `.jsonl`, or raw workout files
- screenshots containing your account or workout data

This project is for exporting your own workout history from your own FITR account only.

Do not use it to:

- bypass authentication
- scrape credentials
- access someone else's account
- generate heavy traffic against FITR

## Troubleshooting

If `export_workouts.bat` says Python is missing:

- Install Python from <https://www.python.org/downloads/windows/>.
- Make sure **Add python.exe to PATH** is checked.
- Close and reopen the command window.
- Run `setup_windows.bat` again.

If Chromium opens but FITR is not logged in:

- Log in manually.
- Wait for the calendar to appear.
- Return to the command window and press Enter.

If the export misses a day:

- Try a small date range around the missing day.
- Example:

```powershell
python .\fitr_export.py --start-date 2025-09-29 --end-date 2025-10-01
```

If FITR changes the page layout:

- The exporter may need an update.
- Save the error message and the date range you tried.
- Do not share exported workout files unless you are comfortable sharing that personal data.

## Known Limitations

- This tool is unsupported and provided as is.
- The default export depends on FITR's visible calendar UI.
- The parser has only been tested with Operation LFG from coach Josh Bridges.
- If FITR changes button labels, modal structure, or calendar layout, selectors may need updates.
- Calendar dates are inferred from the rendered calendar order.
- Markdown and CSV output contain personal workout data.
