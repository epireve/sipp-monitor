#!/usr/bin/env python3
"""
SIPP Court URL Connectivity Checker
====================================
Checks all 349 Indonesian district court SIPP URLs for HTTP connectivity,
diffs against the previous state snapshot, generates a dated markdown report,
and auto-opens GitHub issues for major jurisdiction failures exceeding 24h.

Usage:
    python check_connectivity.py [--output-dir REPORTS_DIR] [--state-file STATE_FILE]

Environment variables (injected by GitHub Actions):
    GH_TOKEN        - GitHub token for issue creation
    GH_REPO         - owner/repo for issue creation (e.g. epireve/sipp-monitor)
    NOTIFY_EMAIL    - recipient email (unused here; handled by Actions workflow)
"""

import asyncio
import aiohttp
import csv
import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict

# ─── Configuration ────────────────────────────────────────────────────────────

COURTS_CSV = Path(__file__).parent.parent / "data" / "sipp_courts.csv"
DEFAULT_STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"
DEFAULT_REPORTS_DIR = Path(__file__).parent.parent / "reports"

TIMEOUT_SECONDS = 15
CONCURRENCY = 35
RETRY_COUNT = 2
RETRY_DELAY = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SIPP-Monitor/2.0; research-bot)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "id,en;q=0.5",
}

# Courts where failure > 24h triggers a GitHub issue
MAJOR_JURISDICTION_SLUGS = {
    "sipp.pn-jakartapusat.go.id",
    "sipp.pn-jakartaselatan.go.id",
    "sipp.pn-jakartabarat.go.id",
    "sipp.pn-jakartautara.go.id",
    "sipp.pn-jakartatimur.go.id",
    "sipp.pn-surabaya.go.id",
    "sipp.pn-bandung.go.id",
    "sipp.pn-medan.go.id",
    "sipp.pn-semarangkota.go.id",
    "sipp.pn-makassar.go.id",
    "sipp.pn-palembang.go.id",
    "sipp.pn-pekanbaru.go.id",
    "sipp.pn-banjarmasin.go.id",
    "sipp.pn-pontianak.go.id",
    "sipp.pn-samarinda.go.id",
    "sipp.pn-balikpapan.go.id",
    "sipp.pn-manado.go.id",
    "sipp.pn-denpasar.go.id",
    "sipp.pn-mataram.go.id",
    "sipp.pn-kupang.go.id",
    "sipp.pn-ambon.go.id",
    "sipp.pn-jayapura.go.id",
    "sipp.pn-yogyakarta.go.id",
    "sipp.pn-malang.go.id",
    "sipp.pn-bekasi.go.id",
    "sipp.pn-tangerang.go.id",
    "sipp.pn-bogor.go.id",
}

# ─── Status Classification ────────────────────────────────────────────────────

def classify_status(http_code: int | None, error: str | None) -> str:
    if error:
        if "timeout" in error.lower() or error == "TIMEOUT":
            return "TIMEOUT"
        if "refused" in error.lower() or "refused" in str(error):
            return "CONNECTION_REFUSED"
        if "ssl" in error.lower():
            return "SSL_ERROR"
        return "ERROR"
    if http_code is None:
        return "ERROR"
    if http_code in (200, 304):
        return "ACTIVE"
    if http_code == 403:
        return "ACTIVE_RESTRICTED"
    if http_code in (301, 302, 307, 308):
        return "REDIRECT"
    if http_code == 404:
        return "NOT_FOUND"
    if http_code in (500, 503):
        return "SERVER_ERROR"
    if http_code == 502:
        return "BAD_GATEWAY"
    return f"HTTP_{http_code}"

REACHABLE_STATUSES = {"ACTIVE", "ACTIVE_RESTRICTED", "REDIRECT"}
INACCESSIBLE_STATUSES = {
    "TIMEOUT", "CONNECTION_REFUSED", "ERROR", "NOT_FOUND",
    "SERVER_ERROR", "BAD_GATEWAY", "SSL_ERROR",
}

# ─── Async HTTP checker ───────────────────────────────────────────────────────

async def check_url(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore,
                    row: dict, retry: int = RETRY_COUNT) -> dict:
    url = row["sipp_url"]
    result = dict(row)

    for attempt in range(retry + 1):
        async with semaphore:
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                    allow_redirects=True,
                    headers=HEADERS,
                    ssl=False,
                ) as resp:
                    status = resp.status
                    final_url = str(resp.url)
                    result["http_status"] = status
                    result["final_url"] = final_url
                    result["connectivity"] = classify_status(status, None)

                    # Detect non-standard redirects
                    from urllib.parse import urlparse
                    orig_host = urlparse(url).netloc
                    final_host = urlparse(final_url).netloc
                    if orig_host != final_host and status in (200, 301, 302, 307):
                        result["connectivity"] = "REDIRECT_NONSTANDARD"
                        result["notes"] = f"→ {final_url}"
                    return result

            except asyncio.TimeoutError:
                result["http_status"] = 0
                result["connectivity"] = "TIMEOUT"
                result["final_url"] = url
            except aiohttp.ClientConnectorError as e:
                result["http_status"] = 0
                result["connectivity"] = "CONNECTION_REFUSED"
                result["final_url"] = url
                result["notes"] = str(e)[:80]
            except aiohttp.ClientSSLError as e:
                result["http_status"] = 0
                result["connectivity"] = "SSL_ERROR"
                result["final_url"] = url
                result["notes"] = str(e)[:80]
            except Exception as e:
                result["http_status"] = 0
                result["connectivity"] = "ERROR"
                result["final_url"] = url
                result["notes"] = f"{type(e).__name__}: {str(e)[:60]}"

            if attempt < retry:
                await asyncio.sleep(RETRY_DELAY)

    return result


async def run_checks(rows: list[dict]) -> list[dict]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY, ttl_dns_cache=300, ssl=False, limit_per_host=2
    )
    results = []
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_url(session, semaphore, row) for row in rows]
        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            completed += 1
            if completed % 50 == 0:
                print(f"  [{completed}/{len(rows)}] checked...", flush=True)
    return results

# ─── State Management ─────────────────────────────────────────────────────────

def load_state(state_file: Path) -> dict:
    """Load previous check state: {sipp_url: {status, first_down_at, last_checked}}"""
    if state_file.exists():
        try:
            with open(state_file) as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: could not load state file: {e}")
    return {}

def save_state(state: dict, state_file: Path):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def diff_results(current_results: list[dict], prev_state: dict,
                 now: str) -> dict:
    """
    Returns:
      - went_down:   newly inaccessible (was reachable, now down)
      - came_back:   recovered (was down, now reachable)
      - still_down:  persistently inaccessible
      - major_down_24h: major jurisdictions down > 24h
      - new_state:   updated state dict
    """
    went_down = []
    came_back = []
    still_down = []
    new_state = {}
    now_dt = datetime.fromisoformat(now)

    for r in current_results:
        url = r["sipp_url"]
        conn = r["connectivity"]
        is_reachable = conn in REACHABLE_STATUSES or conn == "REDIRECT_NONSTANDARD"
        prev = prev_state.get(url, {})
        prev_conn = prev.get("connectivity", "UNKNOWN")
        prev_reachable = prev_conn in REACHABLE_STATUSES or prev_conn == "REDIRECT_NONSTANDARD"

        entry = {
            "court_name": r["court_name"],
            "connectivity": conn,
            "http_status": r.get("http_status", 0),
            "last_checked": now,
            "province": r["province"],
            "high_court": r["high_court"],
        }

        if is_reachable:
            entry["first_down_at"] = None
            if not prev_reachable and prev_conn != "UNKNOWN":
                # Recovered
                entry["recovered_from"] = prev_conn
                entry["was_down_since"] = prev.get("first_down_at")
                came_back.append({**r, **entry})
        else:
            # Currently down
            if prev.get("first_down_at"):
                entry["first_down_at"] = prev["first_down_at"]
            else:
                entry["first_down_at"] = now

            if prev_reachable or prev_conn == "UNKNOWN":
                went_down.append({**r, **entry})
            else:
                still_down.append({**r, **entry})

        new_state[url] = entry

    # Major jurisdictions down > 24h
    major_down_24h = []
    for r in still_down + went_down:
        url = r["sipp_url"]
        host = url.replace("https://", "").replace("http://", "").rstrip("/")
        if host in MAJOR_JURISDICTION_SLUGS:
            first_down = new_state[url].get("first_down_at")
            if first_down:
                down_since = datetime.fromisoformat(first_down)
                hours_down = (now_dt - down_since).total_seconds() / 3600
                if hours_down >= 24 or r in went_down:
                    major_down_24h.append({
                        **r,
                        "hours_down": round(hours_down, 1),
                        "first_down_at": first_down,
                    })

    return {
        "went_down": went_down,
        "came_back": came_back,
        "still_down": still_down,
        "major_down_24h": major_down_24h,
        "new_state": new_state,
    }

# ─── Markdown Report ──────────────────────────────────────────────────────────

def generate_report(current_results: list[dict], diff: dict,
                    run_ts: str, elapsed: float) -> str:
    date_str = datetime.fromisoformat(run_ts).strftime("%A, %d %B %Y")
    time_str = datetime.fromisoformat(run_ts).strftime("%H:%M UTC")
    total = len(current_results)
    by_conn = Counter(r["connectivity"] for r in current_results)

    active = sum(by_conn.get(s, 0) for s in REACHABLE_STATUSES) + by_conn.get("REDIRECT_NONSTANDARD", 0)
    inaccessible = sum(by_conn.get(s, 0) for s in INACCESSIBLE_STATUSES)
    pct_active = round(active / total * 100, 1)

    lines = [
        f"# SIPP Court Connectivity Report",
        f"",
        f"**Date:** {date_str}  ",
        f"**Run time:** {time_str}  ",
        f"**Duration:** {elapsed:.1f}s  ",
        f"**Courts checked:** {total}",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Status | Count |",
        f"|--------|-------|",
        f"| ✅ Active (HTTP 200) | {by_conn.get('ACTIVE', 0)} |",
        f"| 🔒 Active Restricted (HTTP 403) | {by_conn.get('ACTIVE_RESTRICTED', 0)} |",
        f"| ↪️ Redirect (non-standard) | {by_conn.get('REDIRECT_NONSTANDARD', 0) + by_conn.get('REDIRECT', 0)} |",
        f"| ❌ Inaccessible | {inaccessible} |",
        f"| 🔴 Server Error | {by_conn.get('SERVER_ERROR', 0) + by_conn.get('BAD_GATEWAY', 0)} |",
        f"| **Total Reachable** | **{active} / {total} ({pct_active}%)** |",
        f"",
    ]

    # Status changes section
    went_down = diff["went_down"]
    came_back = diff["came_back"]
    major = diff["major_down_24h"]

    lines += [f"---", f"", f"## Status Changes", f""]

    if not went_down and not came_back:
        lines.append("_No status changes since last run._")
    else:
        if went_down:
            lines += [
                f"### 🔴 Newly Inaccessible ({len(went_down)})",
                f"",
                f"| Court | Province | High Court | Failure Mode | SIPP URL |",
                f"|-------|----------|------------|--------------|----------|",
            ]
            for r in sorted(went_down, key=lambda x: x["province"]):
                lines.append(
                    f"| {r['court_name']} | {r['province']} | {r['high_court']} "
                    f"| `{r['connectivity']}` | {r['sipp_url']} |"
                )
            lines.append("")

        if came_back:
            lines += [
                f"### 🟢 Recovered ({len(came_back)})",
                f"",
                f"| Court | Province | Was Down Since | Recovery |",
                f"|-------|----------|----------------|----------|",
            ]
            for r in sorted(came_back, key=lambda x: x["province"]):
                was_down = r.get("was_down_since", "unknown")
                lines.append(
                    f"| {r['court_name']} | {r['province']} | {was_down} | ✅ |"
                )
            lines.append("")

    # Major jurisdiction alerts
    if major:
        lines += [
            f"---", f"",
            f"## ⚠️ Major Jurisdiction Alerts",
            f"",
            f"> These courts serve high-volume jurisdictions. "
            f"Outages have been flagged for GitHub issue creation.",
            f"",
            f"| Court | Province | Hours Down | Since | Failure Mode |",
            f"|-------|----------|------------|-------|--------------|",
        ]
        for r in major:
            lines.append(
                f"| **{r['court_name']}** | {r['province']} | "
                f"{r.get('hours_down', 'N/A')}h | {r.get('first_down_at', 'N/A')[:10]} "
                f"| `{r['connectivity']}` |"
            )
        lines.append("")

    # Persistent outages
    still_down = diff["still_down"]
    if still_down:
        lines += [
            f"---", f"",
            f"## 🟡 Persistent Outages ({len(still_down)})",
            f"",
            f"Courts that were already down in the previous run:",
            f"",
            f"| Court | Province | Down Since | Hours | Failure Mode |",
            f"|-------|----------|------------|-------|--------------|",
        ]
        now_dt = datetime.fromisoformat(run_ts)
        for r in sorted(still_down, key=lambda x: x.get("first_down_at", "")):
            fd = r.get("first_down_at", "")
            hours = ""
            if fd:
                try:
                    hours = str(round((now_dt - datetime.fromisoformat(fd)).total_seconds() / 3600, 1)) + "h"
                except Exception:
                    hours = "?"
            lines.append(
                f"| {r['court_name']} | {r['province']} | {fd[:10] if fd else 'N/A'} "
                f"| {hours} | `{r['connectivity']}` |"
            )
        lines.append("")

    # Full connectivity breakdown by province
    lines += [
        f"---", f"",
        f"## Province Breakdown", f"",
        f"| Province | Total | Active | Restricted | Inaccessible |",
        f"|----------|-------|--------|------------|--------------|",
    ]
    by_province = defaultdict(lambda: {"total": 0, "active": 0, "restricted": 0, "inaccessible": 0})
    for r in current_results:
        p = r["province"]
        by_province[p]["total"] += 1
        conn = r["connectivity"]
        if conn in REACHABLE_STATUSES or conn == "REDIRECT_NONSTANDARD":
            by_province[p]["active"] += 1
        elif conn == "ACTIVE_RESTRICTED":
            by_province[p]["restricted"] += 1
        else:
            by_province[p]["inaccessible"] += 1

    for prov in sorted(by_province):
        d = by_province[prov]
        lines.append(
            f"| {prov} | {d['total']} | {d['active']} | {d['restricted']} | {d['inaccessible']} |"
        )
    lines.append("")

    # Full raw results table
    lines += [
        f"---", f"",
        f"## Full Court Status Table", f"",
        f"<details>",
        f"<summary>Click to expand all {total} courts</summary>", f"",
        f"| Court | Province | SIPP URL | Status | HTTP |",
        f"|-------|----------|----------|--------|------|",
    ]
    for r in sorted(current_results, key=lambda x: (x["province"], x["court_name"])):
        icon = "✅" if r["connectivity"] == "ACTIVE" else (
               "🔒" if r["connectivity"] == "ACTIVE_RESTRICTED" else (
               "🟢" if r["connectivity"] in ("REDIRECT", "REDIRECT_NONSTANDARD") else "❌"))
        lines.append(
            f"| {r['court_name']} | {r['province']} | {r['sipp_url']} "
            f"| {icon} `{r['connectivity']}` | {r.get('http_status', '-')} |"
        )
    lines += ["", "</details>", ""]

    lines += [
        f"---",
        f"",
        f"_Report generated by [sipp-monitor](https://github.com/epireve/sipp-monitor) · "
        f"{run_ts[:10]}_",
    ]

    return "\n".join(lines)

# ─── GitHub Issue Creation ────────────────────────────────────────────────────

def create_github_issue(court: dict, report_date: str):
    """Open a GitHub issue for a major jurisdiction that has been down > 24h."""
    import subprocess

    title = f"[SIPP DOWN] {court['court_name']} — {court['connectivity']} ({report_date})"
    hours = court.get("hours_down", "?")
    body = f"""## Major Jurisdiction SIPP Outage Detected

**Court:** {court['court_name']}  
**Province:** {court['province']}  
**High Court:** {court['high_court']}  
**SIPP URL:** {court['sipp_url']}  
**Failure mode:** `{court['connectivity']}`  
**HTTP status:** {court.get('http_status', 'N/A')}  
**First detected down:** {court.get('first_down_at', 'N/A')}  
**Duration:** ~{hours} hours  

### Impact

This is a **major jurisdiction** court. Prolonged SIPP unavailability means:
- Case tracking data for this region is inaccessible
- Research datasets depending on this URL will have gaps
- Manual verification may be required for data integrity

### Recommended Actions

1. Verify manually: [{court['sipp_url']}]({court['sipp_url']})
2. Check Mahkamah Agung announcements: https://www.mahkamahagung.go.id
3. Contact the high court ({court['high_court']}) IT division if outage persists > 48h
4. Update `data/sipp_courts.csv` if the URL has permanently changed

---
_Auto-opened by [sipp-monitor](https://github.com/epireve/sipp-monitor) on {report_date}_
"""

    gh_repo = os.environ.get("GH_REPO", "epireve/sipp-monitor")
    label_args = ["--label", "sipp-outage", "--label", "major-jurisdiction"]

    cmd = [
        "gh", "issue", "create",
        "--repo", gh_repo,
        "--title", title,
        "--body", body,
    ] + label_args

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"  ✅ Issue created: {result.stdout.strip()}")
        else:
            # Check if a duplicate issue already exists
            if "already exists" in result.stderr.lower() or result.returncode != 0:
                print(f"  ℹ️  Issue may already exist for {court['court_name']}: {result.stderr[:100]}")
    except Exception as e:
        print(f"  ❌ Failed to create issue for {court['court_name']}: {e}")


def check_existing_issue(court_name: str) -> bool:
    """Returns True if an open issue already exists for this court."""
    import subprocess
    gh_repo = os.environ.get("GH_REPO", "epireve/sipp-monitor")
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", gh_repo,
             "--label", "sipp-outage", "--state", "open",
             "--search", court_name, "--json", "title"],
            capture_output=True, text=True, timeout=20
        )
        issues = json.loads(result.stdout or "[]")
        return any(court_name in i.get("title", "") for i in issues)
    except Exception:
        return False

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SIPP court connectivity checker")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--courts-csv", type=Path, default=COURTS_CSV)
    parser.add_argument("--no-issues", action="store_true",
                        help="Skip GitHub issue creation")
    args = parser.parse_args()

    # Load courts
    rows = []
    with open(args.courts_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Normalize: add missing fields
    for r in rows:
        r.setdefault("http_status", "")
        r.setdefault("connectivity", "PENDING")
        r.setdefault("notes", "")
        r.setdefault("final_url", r.get("sipp_url", ""))

    print(f"Loaded {len(rows)} courts from {args.courts_csv}")

    # Load previous state
    prev_state = load_state(args.state_file)
    print(f"Loaded previous state for {len(prev_state)} courts")

    # Run checks
    import time
    start = time.time()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"Starting connectivity check at {now}...")

    results = asyncio.run(run_checks(rows))
    elapsed = time.time() - start

    # Name-order sort to match original CSV
    name_order = {r["court_name"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: name_order.get(r["court_name"], 9999))

    print(f"\nCompleted {len(results)} checks in {elapsed:.1f}s")

    # Connectivity summary
    by_conn = Counter(r["connectivity"] for r in results)
    active = sum(by_conn.get(s, 0) for s in REACHABLE_STATUSES) + by_conn.get("REDIRECT_NONSTANDARD", 0)
    print(f"Active: {active}, Inaccessible: {sum(by_conn.get(s,0) for s in INACCESSIBLE_STATUSES)}")

    # Diff against previous state
    diff = diff_results(results, prev_state, now)
    print(f"Changes — went down: {len(diff['went_down'])}, "
          f"came back: {len(diff['came_back'])}, "
          f"still down: {len(diff['still_down'])}, "
          f"major alerts: {len(diff['major_down_24h'])}")

    # Save new state
    save_state(diff["new_state"], args.state_file)
    print(f"State saved to {args.state_file}")

    # Generate markdown report
    args.output_dir.mkdir(parents=True, exist_ok=True)
    date_slug = now[:10]
    report_path = args.output_dir / f"{date_slug}.md"
    report_md = generate_report(results, diff, now, elapsed)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"Report written to {report_path}")

    # Also write latest.md for easy linking
    latest_path = args.output_dir / "latest.md"
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    # Update sipp_courts.csv with latest connectivity status
    fieldnames = list(rows[0].keys())
    if "access_category" not in fieldnames:
        fieldnames.append("access_category")
    results_by_name = {r["court_name"]: r for r in results}
    for r in rows:
        updated = results_by_name.get(r["court_name"], {})
        r.update({k: updated.get(k, r.get(k, "")) for k in ("http_status", "connectivity", "notes")})
        conn = r.get("connectivity", "")
        if conn in REACHABLE_STATUSES:
            r["access_category"] = "ACTIVE"
        elif conn == "ACTIVE_RESTRICTED":
            r["access_category"] = "ACTIVE_RESTRICTED"
        elif conn in INACCESSIBLE_STATUSES:
            r["access_category"] = "INACCESSIBLE"
        else:
            r["access_category"] = conn

    updated_csv = args.courts_csv
    with open(updated_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Updated courts CSV: {updated_csv}")

    # GitHub issue creation for major 24h outages
    if not args.no_issues and diff["major_down_24h"]:
        print(f"\nCreating GitHub issues for {len(diff['major_down_24h'])} major outages...")
        for court in diff["major_down_24h"]:
            if not check_existing_issue(court["court_name"]):
                create_github_issue(court, date_slug)
            else:
                print(f"  ℹ️  Issue already open for {court['court_name']}, skipping")

    # Output summary for Actions step summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(f"## SIPP Check — {date_slug}\n\n")
            f.write(f"- **Active:** {active}/{len(results)}\n")
            f.write(f"- **Newly down:** {len(diff['went_down'])}\n")
            f.write(f"- **Recovered:** {len(diff['came_back'])}\n")
            f.write(f"- **Major alerts:** {len(diff['major_down_24h'])}\n\n")
            if diff["went_down"]:
                f.write("### Newly Inaccessible\n")
                for r in diff["went_down"]:
                    f.write(f"- {r['court_name']} ({r['province']}) — `{r['connectivity']}`\n")

    # Exit 1 if any major jurisdiction is newly down (signals workflow to send alert email)
    if diff["went_down"] or diff["major_down_24h"]:
        sys.exit(0)  # Still succeed; Actions handles email separately
    sys.exit(0)


if __name__ == "__main__":
    main()
