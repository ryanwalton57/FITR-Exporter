import hashlib
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Response


CANDIDATE_URL_PATTERNS = (
    "calendar",
    "workout",
    "workouts",
    "schedule",
    "scheduled",
    "session",
    "sessions",
    "program",
    "programs",
    "training",
)

SENSITIVE_KEY_PATTERNS = re.compile(
    r"(authorization|token|password|secret|cookie|session|csrf|xsrf|jwt|bearer|email|phone)",
    re.IGNORECASE,
)


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    sanitized_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if SENSITIVE_KEY_PATTERNS.search(key):
            sanitized_query.append((key, "<redacted>"))
        elif re.search(r"\d{4}-\d{2}-\d{2}", value):
            sanitized_query.append((key, value))
        elif value.isdigit() and len(value) <= 4:
            sanitized_query.append((key, value))
        else:
            sanitized_query.append((key, "<value>"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized_query), ""))


def endpoint_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def safe_filename(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")[:80]
    return f"{stem}_{digest}"


def looks_relevant_url(url: str) -> bool:
    lowered = url.lower()
    return any(pattern in lowered for pattern in CANDIDATE_URL_PATTERNS)


def summarize_json(value: Any, depth: int = 0, max_depth: int = 5) -> Any:
    if depth >= max_depth:
        return type(value).__name__

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            if SENSITIVE_KEY_PATTERNS.search(str(key)):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = summarize_json(item, depth + 1, max_depth)
        if len(value) > 80:
            result["<truncated_keys>"] = len(value) - 80
        return result

    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "sample": [summarize_json(item, depth + 1, max_depth) for item in value[:3]],
        }

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        if re.search(r"\d{4}-\d{2}-\d{2}", value):
            return "<date-string>"
        return "str"
    return type(value).__name__


class NetworkCapture:
    def __init__(
        self,
        run_dir: Path,
        save_raw_samples: bool,
        max_samples_per_endpoint: int = 3,
    ) -> None:
        self.run_dir = run_dir
        self.save_raw_samples = save_raw_samples
        self.max_samples_per_endpoint = max_samples_per_endpoint
        self.samples_dir = run_dir / "api_samples"
        self.raw_dir = self.samples_dir / "raw"
        self.structure_dir = self.samples_dir / "structure"
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        self.structure_dir.mkdir(parents=True, exist_ok=True)
        if self.save_raw_samples:
            self.raw_dir.mkdir(parents=True, exist_ok=True)

        self.endpoints: dict[str, dict[str, Any]] = {}
        self.sample_counts: defaultdict[str, int] = defaultdict(int)

    def handle_response(self, response: Response) -> None:
        request = response.request
        resource_type = request.resource_type
        if resource_type not in {"xhr", "fetch"}:
            return

        url = response.url
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type and not looks_relevant_url(url):
            return

        try:
            payload = response.json()
        except Exception:
            return

        key = endpoint_key(url)
        candidate = looks_relevant_url(url) or self._payload_looks_relevant(payload)
        endpoint = self.endpoints.setdefault(
            key,
            {
                "endpoint": sanitize_url(key),
                "example_url": sanitize_url(url),
                "methods": sorted({request.method}),
                "status_codes": sorted({response.status}),
                "resource_types": sorted({resource_type}),
                "candidate": candidate,
                "sample_count": 0,
                "raw_samples_saved": 0,
                "structure_samples_saved": 0,
            },
        )
        endpoint["methods"] = sorted(set(endpoint["methods"]) | {request.method})
        endpoint["status_codes"] = sorted(set(endpoint["status_codes"]) | {response.status})
        endpoint["resource_types"] = sorted(set(endpoint["resource_types"]) | {resource_type})
        endpoint["candidate"] = bool(endpoint["candidate"] or candidate)
        endpoint["sample_count"] += 1

        if not candidate:
            return

        if self.sample_counts[key] >= self.max_samples_per_endpoint:
            return

        self.sample_counts[key] += 1
        sample_number = self.sample_counts[key]
        name = safe_filename(f"{urlsplit(key).netloc}_{urlsplit(key).path.strip('/') or 'root'}_{sample_number}")

        structure_path = self.structure_dir / f"{name}.json"
        structure_path.write_text(
            json.dumps(
                {
                    "endpoint": sanitize_url(key),
                    "example_url": sanitize_url(url),
                    "status": response.status,
                    "method": request.method,
                    "resource_type": resource_type,
                    "structure": summarize_json(payload),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        endpoint["structure_samples_saved"] += 1

        if self.save_raw_samples:
            raw_path = self.raw_dir / f"{name}.json"
            raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            endpoint["raw_samples_saved"] += 1

        logging.info("Captured candidate JSON endpoint: %s", sanitize_url(url))

    def write_summary(self) -> None:
        endpoints = sorted(
            self.endpoints.values(),
            key=lambda item: (not item["candidate"], item["endpoint"]),
        )
        summary_path = self.samples_dir / "candidate_endpoints.json"
        summary_path.write_text(json.dumps(endpoints, indent=2, ensure_ascii=False), encoding="utf-8")

        text_lines = ["Candidate API endpoints", ""]
        for endpoint in endpoints:
            marker = "candidate" if endpoint["candidate"] else "observed"
            text_lines.append(f"- [{marker}] {endpoint['endpoint']}")
            text_lines.append(f"  example: {endpoint['example_url']}")
            text_lines.append(
                f"  samples: {endpoint['sample_count']} observed, "
                f"{endpoint['structure_samples_saved']} structures, "
                f"{endpoint['raw_samples_saved']} raw"
            )
        (self.samples_dir / "candidate_endpoints.txt").write_text("\n".join(text_lines) + "\n", encoding="utf-8")

    def candidate_count(self) -> int:
        return sum(1 for endpoint in self.endpoints.values() if endpoint["candidate"])

    def _payload_looks_relevant(self, payload: Any) -> bool:
        try:
            text = json.dumps(payload, ensure_ascii=False).lower()
        except TypeError:
            return False
        return any(pattern in text for pattern in CANDIDATE_URL_PATTERNS)
