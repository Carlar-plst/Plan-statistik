#!/usr/bin/env python3
"""
Municipality Hearing Link Validator
------------------------------------
Manual run:   python validate_links.py
Later, for automated/periodic runs, this script can be invoked as-is from
cron / Task Scheduler / a CI job with no changes needed.

Uses only the Python standard library (urllib).

What it does:
  - Reads municipality_hearing_links.js
  - GETs each URL (15s timeout, follows redirects, records the final URL)
  - Classifies each result as OK / WARNING / DEFECT
  - Retries only genuine failures (not warnings) with exponential backoff
    before giving up and marking a link DEFECT
  - Writes a timestamped JSON file per run into validation_results/, so
    runs can be diffed/cross-checked against each other later
  - Tracks currently-broken links in known_issues.json across runs, so a
    link that's been broken for weeks doesn't look "new" every run -
    this is what a future email-alert step would filter on
      (result["is_new_defect"] == True)
"""

import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---- Config ----
JS_FILE = Path("municipality_hearing_links.js")
RESULTS_DIR = Path("validation_results")
KNOWN_ISSUES_FILE = Path("known_issues.json")

TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
BACKOFF_SECONDS = [5, 10, 20]  # delay before retry 1, 2, 3 respectively

# 401/403/429 = site might deliberately be blocking automated requests -> needs a
# human to check by hand, not necessarily a real defect.
WARNING_STATUS_CODES = {401, 403, 429}
OK_STATUS_RANGE = range(200, 400)

USER_AGENT = "Mozilla/5.0 (compatible; HearingLinkValidator/1.0; +internal-link-check)"


def load_links(js_path: Path) -> dict:
    """Extract the MUNICIPALITY_HEARING_LINKS object from the .js file as-is."""
    content = js_path.read_text(encoding="utf-8")
    match = re.search(r'const\s+\w+\s*=\s*', content)
    if not match:
        raise ValueError(f"No 'const NAME = {{...}}' declaration found in {js_path}")
    content = content[match.end():]
    content = content.rstrip().rstrip(';').strip()
    content = re.sub(r'(?<=[{,\s])(url|label)\s*:', r'"\1":', content)  # bare keys -> JSON
    return json.loads(content)



def classify(status_code, error):
    """'ok' / 'warning' / 'defect_candidate' for a single attempt."""
    if error:
        return "defect_candidate"
    if status_code in WARNING_STATUS_CODES:
        return "warning"
    if status_code in OK_STATUS_RANGE:
        return "ok"
    return "defect_candidate"  # 404, 410, 5xx, anything unexpected


class _CountingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Tracks how many redirects a single request followed."""

    def __init__(self):
        super().__init__()
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirect_count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url_once(url: str) -> dict:
    """Single HTTP attempt. Always returns a result dict, never raises."""
    start = time.monotonic()
    handler = _CountingRedirectHandler()
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            return {
                "status_code": resp.status,
                "final_url": resp.geturl(),
                "response_time_s": round(time.monotonic() - start, 3),
                "redirect_count": handler.redirect_count,
                "error": None,
            }
    except urllib.error.HTTPError as e:
        # Raised for 4xx/5xx, but it's a real response - not a connection failure.
        return {
            "status_code": e.code,
            "final_url": e.url if hasattr(e, "url") else url,
            "response_time_s": round(time.monotonic() - start, 3),
            "redirect_count": handler.redirect_count,
            "error": None,
        }
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.timeout):
            error = "timeout"
        elif isinstance(reason, socket.gaierror):
            error = "dns_error"
        elif isinstance(reason, ssl.SSLError):
            error = f"ssl_error: {reason}"
        else:
            error = f"connection_error: {reason}"
        return _fail(start, error)
    except socket.timeout:
        return _fail(start, "timeout")
    except ValueError as e:
        return _fail(start, f"invalid_url: {e}", elapsed_override=0.0)
    except Exception as e:
        return _fail(start, f"unexpected_error: {e}")


def _fail(start, error, elapsed_override=None):
    elapsed = elapsed_override if elapsed_override is not None else round(time.monotonic() - start, 3)
    return {"status_code": None, "final_url": None, "response_time_s": elapsed,
            "redirect_count": None, "error": error}


def check_url_with_retries(url: str):
    """Retries only real failures. Warnings and OKs return immediately."""
    attempts = []
    for attempt_num in range(MAX_RETRIES + 1):
        result = check_url_once(url)
        result["attempt"] = attempt_num + 1
        attempts.append(result)

        category = classify(result["status_code"], result["error"])
        if category != "defect_candidate":
            return attempts, category

        if attempt_num < MAX_RETRIES:
            time.sleep(BACKOFF_SECONDS[attempt_num])

    return attempts, "defect"  # retries exhausted, still failing


def load_known_issues() -> dict:
    if KNOWN_ISSUES_FILE.exists():
        return json.loads(KNOWN_ISSUES_FILE.read_text(encoding="utf-8"))
    return {}


def save_known_issues(known_issues: dict):
    KNOWN_ISSUES_FILE.write_text(
        json.dumps(known_issues, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    links = load_links(JS_FILE)
    known_issues = load_known_issues()
    run_timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    results = {}
    new_defects, recovered = [], []

    print(f"Checking {len(links)} links...")
    for i, (municipality, info) in enumerate(sorted(links.items()), start=1):
        url = info["url"]
        print(f"[{i}/{len(links)}] {municipality} ... ", end="", flush=True)

        attempts, final_status = check_url_with_retries(url)
        last = attempts[-1]

        is_new_defect = False
        if final_status == "defect":
            signature = last["error"] or last["status_code"]
            existing = known_issues.get(municipality)
            if not existing or existing.get("error_signature") != signature:
                is_new_defect = True
                new_defects.append(municipality)
            known_issues[municipality] = {
                "error_signature": signature,
                "first_seen": existing["first_seen"] if existing else run_timestamp,
                "last_seen": run_timestamp,
            }
        elif municipality in known_issues:
            recovered.append(municipality)
            del known_issues[municipality]

        results[municipality] = {
            "url": url,
            "label": info.get("label"),
            "timestamp": run_timestamp,
            "final_status": final_status,  # "ok" / "warning" / "defect"
            "is_new_defect": is_new_defect,
            "status_code": last["status_code"],
            "final_url": last["final_url"],
            "response_time_s": last["response_time_s"],
            "error": last["error"],
            "attempts_made": len(attempts),
            "attempt_log": attempts,
        }
        print(final_status.upper())

    save_known_issues(known_issues)

    RESULTS_DIR.mkdir(exist_ok=True)
    out_file = RESULTS_DIR / f"results_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}.json"
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in results.values() if r["final_status"] == "ok")
    warn = sum(1 for r in results.values() if r["final_status"] == "warning")
    defect = sum(1 for r in results.values() if r["final_status"] == "defect")

    print("\n--- Summary ---")
    print(f"OK: {ok}  WARNING: {warn}  DEFECT: {defect}")
    if new_defects:
        print(f"NEW defects (would trigger email later): {', '.join(new_defects)}")
    if recovered:
        print(f"Recovered since last run: {', '.join(recovered)}")
    print(f"Results saved to {out_file}")


if __name__ == "__main__":
    main()