import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from fitr_network import NetworkCapture


CALENDAR_URL = "https://app.fitr.training/user/calendar"
DEFAULT_PROFILE_DIR = ".fitr-browser-profile"
DEFAULT_OUTPUT_DIR = "fitr_output/discovery"
DEFAULT_MAX_EXPAND_PASSES = 80


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover FITR Client calendar page structure for a future personal workout export."
    )
    parser.add_argument("--start-date", help="Start date for the future exporter, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="End date for the future exporter, YYYY-MM-DD.")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="Persistent Chromium user data directory.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for discovery artifacts.")
    parser.add_argument("--slow-mo-ms", type=int, default=150, help="Playwright slow motion delay in milliseconds.")
    parser.add_argument(
        "--click-likely",
        type=int,
        default=0,
        help="Optionally click this many likely workout elements for deeper discovery. Default is 0.",
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Save screenshots during discovery. Off by default because text output is usually more useful.",
    )
    parser.add_argument(
        "--manual-capture",
        action="store_true",
        help="After the calendar loads, let you manually click workouts and press Enter to save visible text.",
    )
    parser.add_argument(
        "--expand-before-capture",
        action="store_true",
        help="In manual capture mode, expand visible section chevrons before saving text.",
    )
    parser.add_argument(
        "--max-expand-passes",
        type=int,
        default=DEFAULT_MAX_EXPAND_PASSES,
        help="Maximum collapsed section controls to expand per manual capture.",
    )
    parser.add_argument(
        "--post-login-timeout-ms",
        type=int,
        default=120_000,
        help="How long to wait for the calendar page to load before prompting.",
    )
    parser.add_argument(
        "--api-discovery",
        action="store_true",
        help="Capture candidate XHR/fetch JSON endpoints while you browse the FITR calendar.",
    )
    parser.add_argument(
        "--save-raw-api-samples",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Save raw JSON response bodies for API debugging. Off by default; sanitized structures are always saved.",
    )
    parser.add_argument(
        "--max-api-samples-per-endpoint",
        type=int,
        default=3,
        help="Maximum JSON samples to save per candidate endpoint during API discovery.",
    )
    return parser.parse_args()


def validate_date(name: str, value: str | None) -> None:
    if not value:
        return
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"{name} must use YYYY-MM-DD, got {value!r}") from exc


def make_run_dir(base_dir: Path) -> Path:
    run_dir = base_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def configure_logging(run_dir: Path) -> None:
    log_path = run_dir / "discovery.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_text(value: str | None, limit: int = 500) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned[:limit]


def screenshot(page: Page, run_dir: Path, name: str, enabled: bool) -> None:
    if not enabled:
        return
    path = run_dir / name
    page.screenshot(path=str(path), full_page=True)
    logging.info("Saved screenshot: %s", path)


def dump_visible_text(page: Page, run_dir: Path) -> None:
    text = page.locator("body").inner_text(timeout=10_000)
    path = run_dir / "page_text.txt"
    path.write_text(text, encoding="utf-8")
    logging.info("Saved visible text: %s", path)


def dump_workout_text_candidates(run_dir: Path, likely: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    for item in likely:
        attrs = item.get("attrs", {})
        rect = item.get("rect", {})
        text = safe_text(item.get("text"), 2000)
        if not text:
            continue
        lines.append(f"Element #{item.get('index')} <{item.get('tag')}>")
        lines.append(f"Position: x={rect.get('x')} y={rect.get('y')} w={rect.get('width')} h={rect.get('height')}")
        if attrs:
            lines.append("Attributes: " + json.dumps(attrs, ensure_ascii=False))
        lines.append("Text:")
        lines.append(text)
        lines.append("")
        lines.append("-" * 80)
        lines.append("")

    path = run_dir / "workout_text_candidates.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    logging.info("Saved workout text candidates: %s", path)


def save_current_page_text(page: Page, run_dir: Path, prefix: str, index: int) -> Path:
    page_text = page.locator("body").inner_text(timeout=10_000)
    path = run_dir / f"{prefix}_{index:02d}_page_text.txt"
    header = [
        f"URL: {page.url}",
        f"Title: {page.title()}",
        f"Captured at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        page_text,
    ]
    path.write_text("\n".join(header), encoding="utf-8")
    logging.info("Saved visible page text: %s", path)
    return path


def expand_visible_sections(page: Page, max_passes: int) -> int:
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
        time.sleep(0.35)

    logging.info("Expanded %s section controls before manual capture", total_clicked)
    return total_clicked


def manual_capture_loop(page: Page, run_dir: Path, expand_before_capture: bool, max_expand_passes: int) -> None:
    captures: list[dict[str, str]] = []
    print()
    print("Manual workout capture mode:")
    print("1. In Chromium, click a calendar day or workout so its details are visible.")
    print("2. Come back to this terminal and press Enter to save the visible workout text.")
    print("3. Type q and press Enter when done.")

    capture_number = 1
    while True:
        response = input(f"Capture workout #{capture_number}? Press Enter to save, or type q to finish: ").strip().lower()
        if response in {"q", "quit", "done", "exit"}:
            break

        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            logging.info("Network did not become idle before capture; saving current visible text anyway.")

        expanded_count = expand_visible_sections(page, max_expand_passes) if expand_before_capture else 0
        text_path = save_current_page_text(page, run_dir, "manual_capture", capture_number)
        structure = collect_page_structure(page)
        structure_path = run_dir / f"manual_capture_{capture_number:02d}_structure.json"
        save_json(structure_path, structure)
        captures.append(
            {
                "url": page.url,
                "title": page.title(),
                "text_file": str(text_path),
                "structure_file": str(structure_path),
                "expanded_section_controls": str(expanded_count),
            }
        )
        capture_number += 1

    save_json(run_dir / "manual_captures.json", captures)
    logging.info("Saved %s manual captures", len(captures))


def collect_page_structure(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const interesting = [
            "a", "button", "input", "select", "textarea", "form", "table",
            "[role]", "[aria-label]", "[data-testid]", "[data-test]", "[data-cy]",
            "[class*='calendar' i]", "[class*='workout' i]", "[class*='session' i]",
            "[class*='event' i]", "[href*='workout' i]", "[href*='calendar' i]"
          ].join(",");

          const attrsOf = (el) => {
            const attrs = {};
            for (const attr of el.attributes || []) {
              if (
                ["id", "class", "role", "aria-label", "aria-current", "name", "type", "placeholder", "href", "title"].includes(attr.name) ||
                attr.name.startsWith("data-")
              ) {
                attrs[attr.name] = attr.value;
              }
            }
            return attrs;
          };

          const elementSummary = (el, index) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return {
              index,
              tag: el.tagName.toLowerCase(),
              text: (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 500),
              attrs: attrsOf(el),
              visible: !!(rect.width || rect.height) && style.visibility !== "hidden" && style.display !== "none",
              rect: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
              }
            };
          };

          const elements = Array.from(document.querySelectorAll(interesting)).map(elementSummary);
          const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6")).map(elementSummary);

          return {
            url: location.href,
            title: document.title,
            timestamp: new Date().toISOString(),
            bodyTextLength: (document.body?.innerText || "").length,
            headings,
            elements
          };
        }
        """
    )


def is_likely_workout_element(item: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            item.get("tag", ""),
            item.get("text", ""),
            " ".join(f"{key}={value}" for key, value in item.get("attrs", {}).items()),
        ]
    ).lower()
    patterns = [
        "workout",
        "training",
        "session",
        "program",
        "complete",
        "completed",
        "calendar",
        "exercise",
        "sets",
        "reps",
        "kg",
        "lbs",
    ]
    return any(pattern in haystack for pattern in patterns)


def collect_likely_workout_elements(structure: dict[str, Any]) -> list[dict[str, Any]]:
    elements = structure.get("elements", [])
    likely = [item for item in elements if item.get("visible") and is_likely_workout_element(item)]
    likely.sort(key=lambda item: (item.get("rect", {}).get("y", 0), item.get("rect", {}).get("x", 0)))
    return likely[:250]


def wait_for_calendar_or_prompt(page: Page, timeout_ms: int) -> None:
    try:
        page.wait_for_url(re.compile(r".*/user/calendar.*"), timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        logging.info("Calendar URL was not detected automatically.")

    print()
    print("When the FITR calendar is visible in Chromium, press Enter here to continue discovery.")
    print("If you are not logged in yet, log in manually first. No credentials are read or stored by this script.")
    input()


def click_likely_elements(
    page: Page,
    run_dir: Path,
    likely: list[dict[str, Any]],
    limit: int,
    screenshots_enabled: bool,
) -> None:
    if limit <= 0:
        return

    clicked: list[dict[str, Any]] = []
    for item in likely[:limit]:
        rect = item.get("rect") or {}
        width = rect.get("width", 0)
        height = rect.get("height", 0)
        if width < 2 or height < 2:
            continue

        x = rect.get("x", 0) + width / 2
        y = rect.get("y", 0) + height / 2
        logging.info("Clicking likely element %s at %s,%s: %s", item.get("index"), x, y, safe_text(item.get("text"), 120))
        try:
            page.mouse.click(x, y)
            page.wait_for_load_state("networkidle", timeout=10_000)
            time.sleep(1.5)
            click_number = len(clicked) + 1
            screenshot(page, run_dir, f"clicked_{click_number:02d}.png", screenshots_enabled)
            page_text = page.locator("body").inner_text(timeout=10_000)
            text_path = run_dir / f"clicked_{click_number:02d}_page_text.txt"
            text_path.write_text(page_text, encoding="utf-8")
            clicked.append(
                {
                    "clicked": item,
                    "url_after": page.url,
                    "title_after": page.title(),
                    "text_file": str(text_path),
                }
            )
            page.go_back(wait_until="networkidle", timeout=15_000)
            time.sleep(1)
        except Exception as exc:
            logging.exception("Click discovery failed for element %s", item.get("index"))
            screenshot(page, run_dir, f"failure_click_{item.get('index', 'unknown')}.png", screenshots_enabled)
            clicked.append({"clicked": item, "error": repr(exc), "url_after": page.url})

    save_json(run_dir / "clicked_likely_elements.json", clicked)


def main() -> int:
    args = parse_args()
    validate_date("--start-date", args.start_date)
    validate_date("--end-date", args.end_date)

    run_dir = make_run_dir(Path(args.output_dir))
    configure_logging(run_dir)

    logging.info("Discovery range: start=%s end=%s", args.start_date, args.end_date)
    logging.info("Artifacts will be saved in %s", run_dir)

    console_messages: list[dict[str, str]] = []
    network_capture = (
        NetworkCapture(
            run_dir,
            save_raw_samples=args.save_raw_api_samples,
            max_samples_per_endpoint=args.max_api_samples_per_endpoint,
        )
        if args.api_discovery
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

        page.on(
            "console",
            lambda msg: console_messages.append({"type": msg.type, "text": safe_text(msg.text, 1000)}),
        )
        if network_capture:
            page.on("response", network_capture.handle_response)

        try:
            logging.info("Opening %s", CALENDAR_URL)
            page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=60_000)
            wait_for_calendar_or_prompt(page, args.post_login_timeout_ms)
            if network_capture:
                print()
                print("API discovery is active. Click months/days/workouts in Chromium to trigger FITR API calls.")
                print("Press Enter here when you are done capturing candidate endpoints.")
                input()

            screenshot(page, run_dir, "01_calendar_loaded.png", args.screenshots)
            dump_visible_text(page, run_dir)

            if args.manual_capture:
                manual_capture_loop(page, run_dir, args.expand_before_capture, args.max_expand_passes)

            structure = collect_page_structure(page)
            save_json(run_dir / "page_structure.json", structure)
            logging.info("Saved page structure with %s elements", len(structure.get("elements", [])))

            likely = collect_likely_workout_elements(structure)
            save_json(run_dir / "likely_workout_elements.json", likely)
            dump_workout_text_candidates(run_dir, likely)
            logging.info("Saved %s likely workout/calendar elements", len(likely))

            click_likely_elements(page, run_dir, likely, args.click_likely, args.screenshots)
            screenshot(page, run_dir, "02_after_optional_clicks.png", args.screenshots)

            save_json(run_dir / "console_messages.json", console_messages)
            if network_capture:
                network_capture.write_summary()
                logging.info("API discovery captured %s candidate endpoints", network_capture.candidate_count())
            logging.info("Discovery complete.")
            print(f"\nDiscovery complete. Review: {run_dir}")
            return 0
        except Exception:
            logging.exception("Discovery failed.")
            try:
                screenshot(page, run_dir, "failure.png", args.screenshots)
                save_json(run_dir / "console_messages.json", console_messages)
                if network_capture:
                    network_capture.write_summary()
            finally:
                context.close()
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
