import argparse
import csv
import html
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Page, Response, TimeoutError as PlaywrightTimeoutError, sync_playwright

from fitr_network import NetworkCapture


CALENDAR_URL = "https://app.fitr.training/user/calendar"
DEFAULT_PROFILE_DIR = ".fitr-browser-profile"
DEFAULT_OUTPUT_DIR = "fitr_output/export"
WORKOUT_SELECTOR = "[aria-label='Open schedule modal workout']"
DEFAULT_MAX_EXPAND_PASSES = 80


@dataclass(frozen=True)
class WorkoutCard:
    index: int
    workout_date: date
    row: int
    col: int
    text: str
    x: float
    y: float
    width: float
    height: float


class FitrApiResponseCache:
    def __init__(self) -> None:
        self.schedule_days_by_date: dict[str, dict[str, Any]] = {}
        self.details_by_schedule_id: dict[str, dict[str, Any]] = {}
        self.details_by_date: dict[str, dict[str, Any]] = {}
        self.comments_by_schedule_id: dict[str, Any] = {}

    def handle_response(self, response: Response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch"}:
            return

        parsed = urlsplit(response.url)
        if parsed.netloc != "app.fitr.training" or not parsed.path.startswith("/api/schedule"):
            return

        try:
            payload = response.json()
        except Exception:
            return

        if parsed.path == "/api/schedule":
            self._remember_schedule_days(payload)
            return

        detail_match = re.fullmatch(r"/api/schedule/(\d+)/athlete/\d+", parsed.path)
        if detail_match and isinstance(payload, dict):
            schedule_id = detail_match.group(1)
            self.details_by_schedule_id[schedule_id] = payload
            date_key = normalize_date_key(payload.get("date"))
            if date_key:
                self.details_by_date[date_key] = payload
            logging.info("Captured FITR workout JSON for schedule %s", schedule_id)
            return

        comments_match = re.fullmatch(r"/api/schedule/(\d+)/comments", parsed.path)
        if comments_match:
            schedule_id = comments_match.group(1)
            self.comments_by_schedule_id[schedule_id] = payload
            logging.info("Captured FITR comments JSON for schedule %s", schedule_id)

    def _remember_schedule_days(self, payload: Any) -> None:
        plans = payload.get("plans") if isinstance(payload, dict) else []
        for plan in plans if isinstance(plans, list) else []:
            if not isinstance(plan, dict):
                continue
            for day in plan.get("days") or []:
                if not isinstance(day, dict) or not day.get("date"):
                    continue
                day_copy = dict(day)
                day_copy["plan_title"] = plan.get("title")
                self.schedule_days_by_date[str(day["date"])] = day_copy

    def day_for_card(self, card: WorkoutCard) -> dict[str, Any] | None:
        return self.schedule_days_by_date.get(card.workout_date.isoformat())

    def detail_for_card(self, card: WorkoutCard) -> tuple[dict[str, Any], Any, dict[str, Any]] | None:
        day = self.day_for_card(card)
        schedule_id = str(day.get("schedule_id") or day.get("id") or "") if day else ""
        detail = self.details_by_schedule_id.get(schedule_id) if schedule_id else None
        if not detail:
            detail = self.details_by_date.get(card.workout_date.isoformat())
        if not detail:
            return None
        if not schedule_id:
            schedule_id = str(detail.get("id") or "")
        fallback_day = day or {
            "date": card.workout_date.isoformat(),
            "schedule_id": schedule_id,
            "plan_title": (detail.get("plan") or {}).get("title") if isinstance(detail.get("plan"), dict) else "",
            "number": ((detail.get("day") or {}).get("number") if isinstance(detail.get("day"), dict) else None),
            "week": (
                ((detail.get("day") or {}).get("week") or {}).get("position")
                if isinstance((detail.get("day") or {}).get("week") if isinstance(detail.get("day"), dict) else None, dict)
                else None
            ),
            "calendar_card_text": card.text,
        }
        return detail, self.comments_by_schedule_id.get(schedule_id, []), fallback_day


def wait_for_api_detail(
    api_cache: FitrApiResponseCache,
    card: WorkoutCard,
    timeout_ms: int = 8_000,
) -> tuple[dict[str, Any], Any, dict[str, Any]] | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        detail = api_cache.detail_for_card(card)
        if detail:
            return detail
        time.sleep(0.1)
    detail = api_cache.detail_for_card(card)
    if not detail:
        logging.info("No captured FITR detail matched %s after %sms", card.workout_date.isoformat(), timeout_ms)
    return detail


def normalize_date_key(value: Any) -> str:
    if value is None:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
    return match.group(0) if match else ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export your own FITR Client workout history from the calendar UI.")
    parser.add_argument("--start-date", required=True, help="Start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", required=True, help="End date, YYYY-MM-DD.")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="Persistent Chromium user data directory.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for export artifacts.")
    parser.add_argument("--slow-mo-ms", type=int, default=100, help="Playwright slow motion delay in milliseconds.")
    parser.add_argument("--delay-ms", type=int, default=900, help="Gentle delay after opening each workout.")
    parser.add_argument("--month-delay-ms", type=int, default=750, help="Gentle delay after loading each month.")
    parser.add_argument("--expand-delay-ms", type=int, default=350, help="Gentle delay after expanding each section.")
    parser.add_argument(
        "--extraction-mode",
        choices=("auto", "api", "dom"),
        default="dom",
        help="Use captured FITR JSON when available, force DOM text, or captured JSON with DOM fallback.",
    )
    parser.add_argument(
        "--max-expand-passes",
        type=int,
        default=DEFAULT_MAX_EXPAND_PASSES,
        help="Maximum collapsed section controls to expand per opened day.",
    )
    parser.add_argument("--max-workouts", type=int, help="Optional safety limit while testing.")
    parser.add_argument(
        "--stop-at-week1-day1",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop after exporting the first workout parsed as Week 1, Day 1. Enabled by default.",
    )
    parser.add_argument(
        "--capture-api-json-samples",
        action="store_true",
        help="Capture candidate XHR/fetch JSON endpoints while exporting for debugging.",
    )
    parser.add_argument(
        "--save-raw-api-samples",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Save raw JSON response bodies when API capture is enabled. Off by default; sanitized structures are saved.",
    )
    parser.add_argument(
        "--max-api-samples-per-endpoint",
        type=int,
        default=3,
        help="Maximum JSON samples to save per candidate endpoint when API capture is enabled.",
    )
    parser.add_argument("--screenshots", action="store_true", help="Save screenshots on failures.")
    return parser.parse_args()


def parse_date_arg(name: str, value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"{name} must use YYYY-MM-DD, got {value!r}") from exc


def make_run_dir(base_dir: Path) -> Path:
    run_dir = base_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "raw_text").mkdir()
    return run_dir


def configure_logging(run_dir: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(run_dir / "export.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")[:120]


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def is_rest_day_text(value: str | None) -> bool:
    return bool(value and re.search(r"\bREST\s+DAY\b", value, re.IGNORECASE))


def repair_mojibake(value: str) -> str:
    replacements = {
        "\u00e2\u20ac\u201d": "\u2014",
        "\u00e2\u20ac\u201c": "\u2013",
        "\u00e2\u20ac\u2122": "\u2019",
        "\u00e2\u20ac\u0153": "\u201c",
        "\u00e2\u20ac\u009d": "\u201d",
        "\u00e2\u2020\u2019": "\u2192",
        "\u00e2\u2030\u02c6": "\u2248",
        "\u00c3\u2014": "\u00d7",
    }
    for broken, repaired in replacements.items():
        value = value.replace(broken, repaired)

    markers = ("â", "Ã", "Â")
    if not any(marker in value for marker in markers):
        return value

    try:
        repaired = value.encode("cp1252").decode("utf-8")
    except UnicodeError:
        return value

    original_marker_count = sum(value.count(marker) for marker in markers)
    repaired_marker_count = sum(repaired.count(marker) for marker in markers)
    return repaired if repaired_marker_count < original_marker_count else value


def month_starts(start: date, end: date) -> list[date]:
    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    months: list[date] = []
    while current <= final:
        months.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def month_starts_desc(start: date, end: date) -> list[date]:
    return list(reversed(month_starts(start, end)))


def monday_on_or_before(day: date) -> date:
    return day - timedelta(days=day.weekday())


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", value)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [clean_text(line) for line in text.splitlines()]
    return repair_mojibake("\n".join(line for line in lines if line))


def section_text_from_api(section: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    title = clean_text(section.get("title"))
    kind = clean_text(section.get("kind"))
    if title:
        lines.append(title)
    elif kind:
        lines.append(kind.title())

    description = strip_html(section.get("formatted_description") or section.get("description"))
    if description:
        lines.extend(description.splitlines())

    if section.get("completed") is True:
        lines.append("Completed")

    performance = section.get("performance")
    if performance:
        lines.append("Performance")
        lines.append(json.dumps(performance, ensure_ascii=False, default=str))

    benchmarks = section.get("benchmarks")
    if benchmarks:
        lines.append("Benchmarks")
        lines.append(json.dumps(benchmarks, ensure_ascii=False, default=str))

    return lines


def comments_text_from_api(comments: Any) -> list[str]:
    if not isinstance(comments, list) or not comments:
        return []

    lines = ["Comments"]
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = strip_html(
            comment.get("body")
            or comment.get("text")
            or comment.get("message")
            or comment.get("comment")
        )
        if body:
            lines.append(body)
    return lines if len(lines) > 1 else []


def api_detail_to_record(
    detail: dict[str, Any],
    comments: Any,
    fallback_day: dict[str, Any],
) -> dict[str, Any]:
    workout_date = datetime.strptime(str(detail.get("date") or fallback_day["date"]), "%Y-%m-%d").date()
    plan = detail.get("plan") if isinstance(detail.get("plan"), dict) else {}
    day = detail.get("day") if isinstance(detail.get("day"), dict) else {}
    week = day.get("week") if isinstance(day.get("week"), dict) else {}
    program_week = week.get("position") or fallback_day.get("week")
    program_day = day.get("number") or fallback_day.get("number")
    program_name = clean_text(plan.get("title") or fallback_day.get("plan_title"))
    day_header = f"{workout_date.strftime('%a')}, {workout_date.strftime('%B')} {workout_date.day} - Week {program_week}, Day {program_day}"

    lines = [program_name or "Unknown", day_header]
    sections = day.get("sections") if isinstance(day.get("sections"), list) else []
    for section in sorted((item for item in sections if isinstance(item, dict)), key=lambda item: item.get("position") or 0):
        section_lines = section_text_from_api(section)
        if section_lines:
            lines.extend(["", *section_lines])

    lines.extend(comments_text_from_api(comments))
    detail_text = "\n".join(line for line in lines if line is not None).strip()
    is_rest_day = is_rest_day_text(detail_text) or not sections
    if is_rest_day and "REST DAY" not in detail_text.upper():
        detail_text = "\n".join([program_name or "Unknown", day_header, "REST DAY"])

    record = {
        "date": workout_date.isoformat(),
        "calendar_card_text": fallback_day.get("calendar_card_text", ""),
        "title": day_header,
        "program_name": program_name,
        "day_header": day_header,
        "program_week": int(program_week) if program_week is not None else None,
        "program_day": int(program_day) if program_day is not None else None,
        "completion_status": "completed" if re.search(r"\bcomplete(d)?\b", detail_text, re.I) else "",
        "is_rest_day": is_rest_day,
        "notes": "\n".join(line for line in detail_text.splitlines() if re.search(r"note|comment", line, re.I)),
        "raw_text": detail_text,
        "extraction_method": "api",
        "fitr_schedule_id": fallback_day.get("schedule_id") or detail.get("id"),
    }
    return record


def wait_for_calendar_or_prompt(page: Page) -> None:
    try:
        page.wait_for_url(re.compile(r".*/user/calendar.*"), timeout=120_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        logging.info("Calendar URL was not detected automatically.")

    print()
    print("If FITR asked you to log in, finish logging in in Chromium.")
    print("When the calendar is visible, press Enter here to start the export.")
    input()


def navigate_to_month(page: Page, month: date) -> None:
    url = f"{CALENDAR_URL}?year={month.year}&month={month.month}&day=1"
    logging.info("Opening month: %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        logging.info("Network did not become idle for %s; continuing.", month)
    try:
        page.wait_for_selector(".calendar-grid, .calendar-user, [class*='calendar' i]", timeout=30_000)
    except PlaywrightTimeoutError:
        logging.info("Calendar grid selector was not detected for %s; continuing with visible page.", month)


def smallest_spacing(values: list[int], fallback: float) -> float:
    unique = sorted(set(values))
    gaps = [b - a for a, b in zip(unique, unique[1:]) if b - a > 10]
    return float(min(gaps)) if gaps else fallback


def collect_cards(page: Page, month: date) -> list[WorkoutCard]:
    raw_cards = page.locator(WORKOUT_SELECTOR).evaluate_all(
        """
        (nodes) => nodes.map((node, index) => {
          const rect = node.getBoundingClientRect();
          return {
            index,
            text: (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim(),
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height
          };
        }).filter((item) => item.width > 2 && item.height > 2)
        """
    )
    if not raw_cards:
        return []

    grid_start = monday_on_or_before(month)

    cards: list[WorkoutCard] = []
    for visible_position, item in enumerate(raw_cards):
        row = visible_position // 7
        col = visible_position % 7
        workout_date = grid_start + timedelta(days=visible_position)
        cards.append(
            WorkoutCard(
                index=int(item["index"]),
                workout_date=workout_date,
                row=row,
                col=col,
                text=clean_text(item["text"]),
                x=float(item["x"]),
                y=float(item["y"]),
                width=float(item["width"]),
                height=float(item["height"]),
            )
        )

    cards.sort(key=lambda card: (card.workout_date, card.y, card.x))
    return cards


def visible_detail_text(page: Page) -> str:
    modal = page.locator("[role='dialog'], .modal.show, .modal, [class*='modal' i]").last
    try:
        if modal.count() and modal.is_visible(timeout=1_000):
            text = modal.inner_text(timeout=5_000)
            if text.strip():
                return text
    except PlaywrightTimeoutError:
        pass
    return page.locator("body").inner_text(timeout=10_000)


def detail_date_pattern(workout_date: date) -> re.Pattern[str]:
    weekday = workout_date.strftime("%a")
    month = workout_date.strftime("%B")
    day_without_zero = str(workout_date.day)
    day_with_zero = f"{workout_date.day:02d}"
    return re.compile(
        rf"^{re.escape(weekday)},\s+{re.escape(month)}\s+({day_without_zero}|{day_with_zero})\b",
        re.IGNORECASE,
    )


def generic_detail_date_pattern() -> re.Pattern[str]:
    months = (
        "January|February|March|April|May|June|July|August|September|October|November|December"
    )
    return re.compile(
        rf"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+({months})\s+\d{{1,2}}\b.*\bWeek\s+\d+,\s*Day\s+[1-7]\b",
        re.IGNORECASE,
    )


def has_detail_date_header(detail_text: str, card: WorkoutCard) -> bool:
    return any(
        detail_date_pattern(card.workout_date).search(line.strip())
        or generic_detail_date_pattern().search(line.strip())
        for line in detail_text.splitlines()
    )


def extract_workout_detail_text(full_text: str, card: WorkoutCard) -> str:
    lines = [line.rstrip() for line in full_text.splitlines()]
    date_pattern = detail_date_pattern(card.workout_date)

    header_index: int | None = None
    for index, line in enumerate(lines):
        if date_pattern.search(line.strip()):
            header_index = index

    if header_index is None:
        generic_pattern = generic_detail_date_pattern()
        for index, line in enumerate(lines):
            if generic_pattern.search(line.strip()):
                header_index = index

    if header_index is None:
        logging.info("Could not find modal date header for %s; saving full visible text.", card.workout_date)
        detail_lines = [line for line in lines if line.strip()]
        return "\n".join(filter_detail_sections(detail_lines)).strip()

    start_index = header_index
    for candidate in range(header_index - 1, -1, -1):
        if lines[candidate].strip():
            start_index = candidate
            break

    end_index = len(lines)
    for index in range(header_index + 1, len(lines)):
        if lines[index].strip().lower() in {"close day", "close"}:
            end_index = index
            break

    detail_lines = [line for line in lines[start_index:end_index] if line.strip()]
    return repair_mojibake("\n".join(filter_detail_sections(detail_lines)).strip())


def capture_dom_record(
    page: Page,
    card: WorkoutCard,
    args: argparse.Namespace,
    is_rest_day: bool,
) -> tuple[dict[str, Any], str]:
    expanded_count = 0 if is_rest_day else expand_visible_sections(page, args.expand_delay_ms, args.max_expand_passes)
    visible_text = visible_detail_text(page)
    detail_text = extract_workout_detail_text(visible_text, card)
    record = parse_basic_fields(card, detail_text)
    record["expanded_section_controls"] = expanded_count
    record["extraction_method"] = "dom"
    record["fitr_schedule_id"] = ""

    detail_claims_rest = is_rest_day_text(detail_text) and has_detail_date_header(detail_text, card)
    record["is_rest_day"] = is_rest_day or detail_claims_rest
    if record["is_rest_day"]:
        detail_text = synthesize_rest_day_text(record, card)
        record = parse_basic_fields(card, detail_text)
        record["expanded_section_controls"] = expanded_count
        record["extraction_method"] = "dom"
        record["fitr_schedule_id"] = ""
        record["is_rest_day"] = True

    return record, detail_text


def filter_detail_sections(lines: list[str]) -> list[str]:
    section_headers = {
        "performance",
        "results",
        "notes",
        "comments",
        "scoring",
        "score",
        "strength",
        "conditioning",
        "conditioning/strength",
        "extra credit:",
    }
    cleaned: list[str] = []
    skipping_media = False

    for line in lines:
        normalized = line.strip().lower()
        if normalized in {"save", "*required"}:
            continue

        if normalized == "media":
            skipping_media = True
            continue

        if skipping_media:
            if normalized in section_headers:
                skipping_media = False
            else:
                continue

        cleaned.append(line)

    return cleaned


def expand_visible_sections(page: Page, delay_ms: int, max_passes: int) -> int:
    total_clicked = 0
    for _ in range(max_passes):
        clicked = page.evaluate(
            """
            () => {
              const modalRoots = Array.from(document.querySelectorAll(
                "[role='dialog'], .modal.show, .modal, [class*='modal' i]"
              )).filter((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 2 && rect.height > 2 && style.display !== "none" && style.visibility !== "hidden";
              });
              const root = modalRoots.at(-1) || document.body;
              const controls = Array.from(root.querySelectorAll("button, [role='button']"));
              const isVisible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 2 && rect.height > 2 && style.display !== "none" && style.visibility !== "hidden";
              };
              const isWanted = (el) => {
                if (el.dataset.fitrExportClicked === "true") {
                  return false;
                }
                const ariaExpanded = (el.getAttribute("aria-expanded") || "").toLowerCase();
                const haystack = [
                  el.getAttribute("aria-label"),
                  el.getAttribute("title"),
                  el.getAttribute("class"),
                  el.innerText,
                  el.textContent
                ].filter(Boolean).join(" ").toLowerCase();
                const looksExpandable =
                  ariaExpanded === "false" ||
                  haystack.includes("chevron down") ||
                  haystack.includes("expand") ||
                  haystack.includes("show more") ||
                  haystack.includes("view more") ||
                  haystack.includes("open section");
                const looksUnsafe =
                  haystack.includes("close") ||
                  haystack.includes("delete") ||
                  haystack.includes("remove") ||
                  haystack.includes("add text") ||
                  haystack.includes("add section") ||
                  haystack.includes("calendar-add");
                return looksExpandable && !looksUnsafe;
              };
              const next = controls.find((el) => isVisible(el) && isWanted(el));
              if (!next) {
                return 0;
              }
              next.dataset.fitrExportClicked = "true";
              next.scrollIntoView({block: "center", inline: "center"});
              next.click();
              return 1;
            }
            """
        )
        if not clicked:
            break
        total_clicked += int(clicked)
        time.sleep(delay_ms / 1000)

    if total_clicked:
        logging.info("Expanded %s section controls", total_clicked)
    if total_clicked >= max_passes:
        logging.info("Reached max expansion passes (%s); consider raising --max-expand-passes if output is incomplete.", max_passes)
    return total_clicked


def close_detail(page: Page) -> None:
    close_selectors = [
        "button[aria-label='Close']",
        "button[aria-label='Close Icon']",
        "button:has-text('Close')",
        ".modal button.btn-close",
        ".modal [data-bs-dismiss='modal']",
    ]
    for selector in close_selectors:
        locator = page.locator(selector).last
        try:
            if locator.count() and locator.is_visible(timeout=500):
                locator.click(timeout=2_000)
                time.sleep(0.4)
                return
        except PlaywrightTimeoutError:
            continue
    page.keyboard.press("Escape")
    time.sleep(0.4)


def parse_basic_fields(card: WorkoutCard, detail_text: str) -> dict[str, Any]:
    lines = [line.strip() for line in detail_text.splitlines() if line.strip()]
    program_name = lines[0] if lines else ""
    day_header = ""
    if len(lines) > 1:
        expected_header = detail_date_pattern(card.workout_date).search(lines[1])
        generic_header = generic_detail_date_pattern().search(lines[1])
        if expected_header or generic_header:
            day_header = lines[1]
    program_week, program_day = parse_program_week_day(day_header)
    title = day_header or program_name or card.text
    return {
        "date": card.workout_date.isoformat(),
        "calendar_card_text": card.text,
        "title": title,
        "program_name": program_name,
        "day_header": day_header,
        "program_week": program_week,
        "program_day": program_day,
        "completion_status": "completed" if re.search(r"\bcomplete(d)?\b", detail_text, re.I) else "",
        "is_rest_day": is_rest_day_text(detail_text),
        "notes": "\n".join(line for line in lines if re.search(r"note|comment", line, re.I)),
        "raw_text": detail_text,
    }


def synthesize_rest_day_text(record: dict[str, Any], card: WorkoutCard) -> str:
    program_name = record.get("program_name") or re.sub(r"\bREST\s+DAY\b", "", card.text, flags=re.IGNORECASE).strip()
    program_name = clean_text(program_name) or "Unknown"
    lines = [program_name]
    if record.get("day_header") and detail_date_pattern(card.workout_date).search(record["day_header"]):
        lines.append(record["day_header"])
    else:
        lines.append(card.workout_date.isoformat())
    lines.append("REST DAY")
    return "\n".join(lines)


def parse_program_week_day(day_header: str) -> tuple[int | None, int | None]:
    match = re.search(r"\bWeek\s+(\d+),\s*Day\s+([1-7])\b", day_header, re.IGNORECASE)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def export_workouts(
    page: Page,
    run_dir: Path,
    start: date,
    end: date,
    args: argparse.Namespace,
    api_cache: FitrApiResponseCache | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    stop_export = False

    for month in month_starts_desc(start, end):
        if stop_export:
            return records

        navigate_to_month(page, month)
        time.sleep(args.month_delay_ms / 1000)
        all_month_cards = [
            card
            for card in collect_cards(page, month)
            if start <= card.workout_date <= end
        ]
        cards = [
            card
            for card in all_month_cards
        ]
        cards.sort(key=lambda card: (card.workout_date, card.y, card.x), reverse=True)
        logging.info(
            "Found %s workout/rest cards in range for %s-%02d",
            len(cards),
            month.year,
            month.month,
        )

        month_summary = [
            {
                "index": card.index,
                "date": card.workout_date.isoformat(),
                "text": card.text,
                "row": card.row,
                "col": card.col,
                "x": card.x,
                "y": card.y,
            }
            for card in cards
        ]
        save_json(run_dir / f"cards_{month.year}_{month.month:02d}.json", month_summary)

        for card in cards:
            if stop_export:
                return records
            if args.max_workouts and len(records) >= args.max_workouts:
                return records

            key = card.workout_date.isoformat()
            if key in seen_keys:
                continue

            logging.info("Opening %s: %s", card.workout_date.isoformat(), card.text)
            try:
                page.locator(WORKOUT_SELECTOR).nth(card.index).click(timeout=10_000)
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                logging.info("Click/load timed out; trying to capture current visible text.")

            is_rest_day = is_rest_day_text(card.text)
            time.sleep(args.delay_ms / 1000)

            api_detail = (
                wait_for_api_detail(api_cache, card)
                if api_cache and args.extraction_mode != "dom"
                else None
            )
            if api_detail:
                detail, comments, fallback_day = api_detail
                record = api_detail_to_record(detail, comments, fallback_day)
                detail_text = record["raw_text"]
                record["expanded_section_controls"] = 0
                logging.info("Using captured FITR API JSON for %s", card.workout_date.isoformat())
            else:
                if args.extraction_mode == "api" and not is_rest_day:
                    logging.info("No captured API detail for %s; using DOM text for this day.", card.workout_date.isoformat())
                record, detail_text = capture_dom_record(page, card, args, is_rest_day)
                if not is_rest_day and not record.get("day_header"):
                    logging.info("Detail capture for %s did not include a day header; retrying once.", card.workout_date.isoformat())
                    close_detail(page)
                    navigate_to_month(page, month)
                    time.sleep(args.month_delay_ms / 1000)
                    try:
                        page.locator(WORKOUT_SELECTOR).nth(card.index).click(timeout=10_000)
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except PlaywrightTimeoutError:
                        logging.info("Retry click/load timed out for %s.", card.workout_date.isoformat())
                    time.sleep(args.delay_ms / 1000)
                    record, detail_text = capture_dom_record(page, card, args, is_rest_day)
            record_key = record.get("date") or key
            if record_key in seen_keys:
                logging.info("Skipping duplicate exported date: %s", record_key)
                close_detail(page)
                continue
            seen_keys.add(record_key)

            raw_name = safe_filename(f"{len(records) + 1:04d}_{record['date']}_{record['title']}")
            raw_path = run_dir / "raw_text" / f"{raw_name}.txt"
            raw_path.write_text(detail_text, encoding="utf-8")
            record["raw_text_file"] = str(raw_path)
            records.append(record)

            close_detail(page)

            if args.stop_at_week1_day1 and record.get("program_week") == 1 and record.get("program_day") == 1:
                logging.info("Reached Week 1, Day 1 at %s; stopping export.", record["date"])
                stop_export = True

    return records


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "date",
        "title",
        "program_name",
        "day_header",
        "week_number",
        "day_number",
        "is_rest_day",
        "completion_status",
        "calendar_card_text",
        "workout_text",
        "notes",
        "extraction_method",
        "fitr_schedule_id",
        "raw_text_file",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["week_number"] = record.get("program_week")
            row["day_number"] = record.get("program_day")
            row["workout_text"] = record.get("raw_text", "")
            writer.writerow(row)


def write_markdown_report(path: Path, records: list[dict[str, Any]], start: date, end: date) -> None:
    lines = [
        "# FITR Workout Export",
        "",
        f"Date range: {start.isoformat()} to {end.isoformat()}",
        f"Workouts: {len(records)}",
        "",
    ]

    for record in records:
        heading_parts = [record.get("date", "")]
        if record.get("day_header"):
            heading_parts.append(record["day_header"])
        elif record.get("program_week") and record.get("program_day"):
            heading_parts.append(f"Week {record['program_week']}, Day {record['program_day']}")

        lines.extend(
            [
                f"## {' - '.join(part for part in heading_parts if part)}",
                "",
                f"Program: {record.get('program_name') or 'Unknown'}",
                "",
            ]
        )

        raw_lines = [line.rstrip() for line in record.get("raw_text", "").splitlines()]
        if raw_lines and raw_lines[0].strip() == record.get("program_name", "").strip():
            raw_lines = raw_lines[1:]
        if raw_lines and raw_lines[0].strip() == record.get("day_header", "").strip():
            raw_lines = raw_lines[1:]

        lines.extend(raw_lines)
        lines.extend(["", "---", ""])

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    start = parse_date_arg("--start-date", args.start_date)
    end = parse_date_arg("--end-date", args.end_date)
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date")

    run_dir = make_run_dir(Path(args.output_dir))
    configure_logging(run_dir)
    logging.info("Exporting FITR workouts from %s to %s", start, end)
    logging.info("Extraction mode: %s", args.extraction_mode)

    network_capture = (
        NetworkCapture(
            run_dir,
            save_raw_samples=args.save_raw_api_samples,
            max_samples_per_endpoint=args.max_api_samples_per_endpoint,
        )
        if args.capture_api_json_samples
        else None
    )

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=args.profile_dir,
            headless=False,
            slow_mo=args.slow_mo_ms,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        api_cache = FitrApiResponseCache() if args.extraction_mode != "dom" else None
        if api_cache:
            page.on("response", api_cache.handle_response)
        if network_capture:
            page.on("response", network_capture.handle_response)

        try:
            page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=60_000)
            wait_for_calendar_or_prompt(page)
            records = export_workouts(page, run_dir, start, end, args, api_cache=api_cache)

            save_json(run_dir / "workouts.json", records)
            write_csv(run_dir / "workouts.csv", records)
            write_markdown_report(run_dir / "workouts.md", records, start, end)
            if network_capture:
                network_capture.write_summary()
                logging.info("API capture observed %s candidate endpoints", network_capture.candidate_count())
            logging.info("Export complete: %s workouts", len(records))
            print(f"\nExport complete: {len(records)} workouts")
            print(f"Open this report first: {run_dir / 'workouts.md'}")
            print(f"Output directory: {run_dir}")
            return 0
        except Exception:
            logging.exception("Export failed.")
            if args.screenshots:
                page.screenshot(path=str(run_dir / "failure.png"), full_page=True)
            if network_capture:
                network_capture.write_summary()
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
