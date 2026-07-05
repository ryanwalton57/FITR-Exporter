# FITR Client Workout History Export

Local Playwright tooling for exporting your own workout history from FITR Client.

This project does not scrape credentials, bypass authentication, or access anything outside the account you manually log in to. Chromium runs non-headless and stores its session locally in `.fitr-browser-profile/`.

## Current Architecture

The exporter now defaults to the faster **Playwright DOM scraper**:

- It opens a real Chromium browser.
- You log in to FITR manually.
- Playwright reuses the browser session stored in `.fitr-browser-profile/`.
- The exporter opens the FITR calendar once so the same logged-in browser context is active.
- In normal export mode, it reads the visible logged-in page.
- In experimental `--extraction-mode auto` or `--extraction-mode api`, it also tries to use FITR's own captured JSON responses while the logged-in frontend loads calendar/workout data.
- Captured JSON is currently slower in observed test runs, so `dom` is the default.
- The DOM scraper opens calendar pages such as `https://app.fitr.training/user/calendar?year=2026&month=7&day=1`.
- The DOM scraper finds rendered calendar cards with `[aria-label="Open schedule modal workout"]`.
- The DOM scraper clicks workout/rest-day cards, expands visible section chevrons, and reads visible modal text.
- It writes `workouts.md`, `workouts.csv`, `workouts.json`, per-workout raw text files, logs, and card-debug JSON.

The JSON responses were identified from discovery output and currently include:

```text
GET https://app.fitr.training/api/users/me
GET https://app.fitr.training/api/schedule?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>
GET https://app.fitr.training/api/schedule/<schedule_id>/athlete/<athlete_id>
GET https://app.fitr.training/api/schedule/<schedule_id>/comments?recipient_id=<athlete_id>
```

These responses are produced by FITR's own logged-in frontend. The tool does not bypass authentication and does not hard-code or extract credentials.

Session persistence is browser-managed. The scripts do not read, print, or store passwords. They do not intentionally read auth tokens from local storage, session storage, or cookies.

## URLs And Assumptions

Explicit URLs opened by the tool:

```text
https://app.fitr.training/user/calendar
https://app.fitr.training/user/calendar?year=<year>&month=<month>&day=1
```

Authenticated API endpoints observed/captured during export mode:

```text
https://app.fitr.training/api/users/me
https://app.fitr.training/api/schedule?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>
https://app.fitr.training/api/schedule/<schedule_id>/athlete/<athlete_id>
https://app.fitr.training/api/schedule/<schedule_id>/comments?recipient_id=<athlete_id>
```

Important DOM assumptions:

- Workout cards expose `aria-label="Open schedule modal workout"`.
- Calendar cards are rendered in Monday-start grid order.
- Day details appear in a modal/dialog after clicking a card.
- Modal headers look like `Mon, March 30 Week 27, Day 1`.
- Collapsed sections can be opened by buttons/role-buttons that look like chevrons or expand controls.

If FITR changes those UI details, rerun discovery mode and update the selectors.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## Discovery

Run:

```powershell
python .\fitr_discovery.py --start-date 2026-01-01 --end-date 2026-07-04
```

What happens:

1. Chromium opens visibly.
2. Log in manually if FITR asks you to.
3. Press Enter in the terminal once the calendar page is visible.
4. The script saves text and page-structure files under `fitr_output/discovery/<timestamp>/`.

The discovery output includes:

- visible page text,
- likely workout text snippets,
- summarized links, buttons, inputs, forms, tables, and common interactive elements,
- likely calendar/workout elements based on text, roles, attributes, and links,
- browser console messages.

Screenshots are off by default. Add `--screenshots` if you want visual debugging files.

To try opening a few likely workout entries and saving their page text:

```powershell
python .\fitr_discovery.py --start-date 2026-01-01 --end-date 2026-07-04 --click-likely 3
```

If workout details only appear after you manually click a calendar day, use manual capture mode:

```powershell
python .\fitr_discovery.py --start-date 2026-01-01 --end-date 2026-07-04 --manual-capture
```

In that mode, click a day or workout in Chromium, then press Enter in the terminal to save the visible workout text. Repeat for as many workouts as you want, then type `q` in the terminal to finish.

If the day modal has collapsed workout sections, add:

```powershell
python .\fitr_discovery.py --start-date 2026-01-01 --end-date 2026-07-04 --manual-capture --expand-before-capture
```

## API Discovery Mode

Use API discovery mode to observe candidate FITR XHR/fetch JSON responses without exporting workouts:

```powershell
python .\fitr_discovery.py --api-discovery
```

Workflow:

1. Chromium opens.
2. Log in manually if needed.
3. Click around the calendar, months, days, and workout modals.
4. Press Enter in the terminal when done.

Discovery writes:

```text
fitr_output/discovery/<timestamp>/api_samples/
  candidate_endpoints.txt
  candidate_endpoints.json
  structure/
```

Sanitized structure samples are saved by default. Raw JSON bodies are off by default because they may contain personal workout/account data. To save raw JSON samples for debugging:

```powershell
python .\fitr_discovery.py --api-discovery --save-raw-api-samples
```

Raw samples are written under:

```text
fitr_output/discovery/<timestamp>/api_samples/raw/
```

Do not share raw samples publicly unless you have reviewed them.

## Automated Export

After you have logged in once, run:

```powershell
python .\fitr_export.py --start-date 2026-01-01 --end-date 2026-07-04
```

The exporter:

- opens Chromium visibly with the same persisted local profile,
- defaults to `--extraction-mode dom`, which uses the faster visible-page scraper,
- uses polite delays between month loads, workout opens, and section expansion,
- visits each month/date range from newest to oldest,
- writes one raw text file per exported day plus the one-file Markdown report,
- records rest days as compact entries with date/week/day and `REST DAY`,
- parses `Week #, Day #` from FITR API data or the DOM day header when available,
- stops after exporting Week 1, Day 1 by default,
- writes a formatted `workouts.md` report plus `workouts.json` and `workouts.csv`.

When the DOM fallback is used, it also:

- finds visible workout cards using `aria-label="Open schedule modal workout"`,
- infers each card date from its calendar grid row and column,
- includes adjacent-month spillover days when needed and de-duplicates by date,
- opens each workout gently,
- expands visible collapsed section chevrons in the day modal,
- saves raw visible workout detail text,
- omits movement-video `Media` sections while keeping substitutions and scales,
- closes the detail modal before continuing.

To try the experimental captured-JSON path during the normal UI walk:

```powershell
python .\fitr_export.py --start-date 2026-01-01 --end-date 2026-07-04 --extraction-mode api
```

To explicitly force the visible-text DOM scraper:

```powershell
python .\fitr_export.py --start-date 2026-01-01 --end-date 2026-07-04 --extraction-mode dom
```

To capture candidate API/XHR JSON while exporting:

```powershell
python .\fitr_export.py --start-date 2026-01-01 --end-date 2026-07-04 --capture-api-json-samples
```

This saves sanitized endpoint structures for debugging. To also save raw JSON response bodies:

```powershell
python .\fitr_export.py --start-date 2026-01-01 --end-date 2026-07-04 --capture-api-json-samples --save-raw-api-samples
```

For a small test run:

```powershell
python .\fitr_export.py --start-date 2026-07-01 --end-date 2026-07-07 --max-workouts 3
```

The exporter defaults to 80 expansion passes per opened day, which covers deeply nested substitutions and scales sections. You can raise it if needed:

```powershell
python .\fitr_export.py --start-date 2026-07-01 --end-date 2026-07-07 --max-expand-passes 10
```

FITR day headers like `Wed, July 01 Week 40, Day 3` are exported as `program_week` and `program_day` automatically. You only need to provide the calendar date range.

To keep exporting past Week 1, Day 1:

```powershell
python .\fitr_export.py --start-date 2025-09-15 --end-date 2026-08-04 --no-stop-at-week1-day1
```

If FITR changes the calendar markup, rerun `fitr_discovery.py` and update `WORKOUT_SELECTOR` in [fitr_export.py](C:/Users/ak574/Documents/Github/FITR/fitr_export.py:14).

## Output Files

Discovery runs create files like:

```text
fitr_output/
  export/
    20260704_120500/
      export.log
      workouts.md
      workouts.json
      workouts.csv
      raw_text/
        0001_2026-07-01_Operation_LFG.txt
      api_samples/
        candidate_endpoints.txt
        candidate_endpoints.json
        structure/
  discovery/
    20260704_120000/
      discovery.log
      page_text.txt
      workout_text_candidates.txt
      manual_capture_01_page_text.txt
      manual_capture_01_structure.json
      manual_captures.json
      clicked_01_page_text.txt
      page_structure.json
      likely_workout_elements.json
```

## Next Step

Open `workouts.md` first. It is the human-readable one-file report. Use `workouts.csv` for spreadsheets; it includes the cleaned workout text in a `workout_text` column. Use `workouts.json` for structured data.

The included exporter handles the first pass. After reviewing real `raw_text` output, improve field parsing for:

- exercises,
- sets,
- reps,
- weights,
- detailed notes,
- visible comments,
- completion status.

## Notes

- Session state is stored in `.fitr-browser-profile/`. Delete that folder to force a fresh login.
- Keep request rates gentle. This project is intended for personal data export, not automated load generation.
- If FITR changes its UI, rerun discovery and update selectors before exporting.
- Use this tool only to export your own workout history from your own authenticated FITR account.
- Do not use it to bypass authentication, scrape credentials, or access another person's data.

## Known Limitations

- Captured JSON extraction depends on FITR's private authenticated JSON response shapes, which may change without notice.
- DOM fallback depends on FITR's rendered UI and selectors.
- DOM fallback calendar dates are inferred from rendered calendar order.
- API output formats section descriptions, benchmarks, performances, and comments from the JSON shape we have observed so far.
- Markdown and CSV output contain personal workout data; review before sharing.
