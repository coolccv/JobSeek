#!/usr/bin/env python3
"""
APS Jobs scraper using Playwright browser automation.

Uses two strategies:
  1. API capture  — intercepts Salesforce/LWC XHR calls to grab raw JSON job data
  2. DOM parse    — falls back to parsing rendered HTML card elements

Run this locally (the site IP-blocks server environments).
"""

import argparse
import json
import re
import time
from dataclasses import dataclass, asdict

from playwright.sync_api import sync_playwright, Page, Request, Response, TimeoutError as PlaywrightTimeout

CHROME_BINARY = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
BASE_URL = "https://www.apsjobs.gov.au/s/job-search"


@dataclass
class Job:
    title: str
    agency: str
    location: str
    classification: str
    salary: str
    closing_date: str
    job_type: str
    url: str


# ---------------------------------------------------------------------------
# API capture helpers
# ---------------------------------------------------------------------------

def _extract_jobs_from_api_payload(data: dict | list) -> list[Job]:
    """Recursively search a Salesforce API response for job records."""
    jobs: list[Job] = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            # Heuristic: a job record has a title-like field
            title = (
                obj.get("Job_Title__c") or obj.get("Name") or obj.get("title") or
                obj.get("Position_Title__c") or obj.get("Vacancy_Title__c") or ""
            )
            if title and isinstance(title, str) and len(title) > 3:
                def g(*keys):
                    for k in keys:
                        v = obj.get(k)
                        if v:
                            return str(v)
                    return ""

                agency = g("Agency_Name__c", "Department__c", "agency", "Organisation__c")
                location = g("Location__c", "Work_Location__c", "location", "State__c")
                classification = g(
                    "APS_Level__c", "Classification__c", "classification",
                    "Salary_Classification__c", "Level__c"
                )
                salary = g("Salary_Range__c", "Salary__c", "salary", "Remuneration__c")
                closing_date = g(
                    "Closing_Date__c", "Application_Close_Date__c",
                    "closing_date", "Close_Date__c"
                )
                job_type = g("Employment_Type__c", "Ongoing_Status__c", "job_type", "Type__c")
                job_id = g("Id", "id", "Job_ID__c")
                url = (
                    f"https://www.apsjobs.gov.au/s/job-detail?jobId={job_id}"
                    if job_id else ""
                )
                jobs.append(Job(
                    title=title, agency=agency, location=location,
                    classification=classification, salary=salary,
                    closing_date=closing_date, job_type=job_type, url=url,
                ))
            else:
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        walk(v)

    walk(data)
    return jobs


# ---------------------------------------------------------------------------
# DOM parse helpers
# ---------------------------------------------------------------------------

def _extract_jobs_from_dom(page: Page) -> list[Job]:
    """Parse job cards from the rendered DOM."""
    jobs: list[Job] = []
    current_url = page.url

    card_selectors = [
        "c-job-search-result-item",
        "[class*='job-card']",
        "[class*='jobCard']",
        ".slds-card",
        "article",
        "li[class*='result']",
    ]

    cards = []
    for sel in card_selectors:
        cards = page.query_selector_all(sel)
        if cards:
            break

    for card in cards:
        try:
            text = card.inner_text().strip()
            if not text:
                continue

            title = agency = location = classification = ""
            salary = closing_date = job_type = ""
            job_url = current_url

            for sel in ["h2", "h3", "h4", "h5", "[class*='title']", "a"]:
                node = card.query_selector(sel)
                if node:
                    title = (node.inner_text() or "").strip()
                    if title:
                        break

            link = card.query_selector("a[href]")
            if link:
                href = link.get_attribute("href") or ""
                if href:
                    job_url = href if href.startswith("http") else f"https://www.apsjobs.gov.au{href}"

            lines = [l.strip() for l in text.split("\n") if l.strip() and l.strip() != title]
            agency_set = False
            for line in lines:
                lower = line.lower()
                if not agency_set and not any(
                    kw in lower for kw in ["$", "aps ", "el ", "el1", "el2", "ses", "closes", "ongoing", "act", "nsw", "vic"]
                ):
                    agency = line
                    agency_set = True
                if any(kw in lower for kw in ["aps ", "el ", "el1", "el2", "ses", "level"]) and not classification:
                    classification = line
                if "$" in line and not salary:
                    salary = line
                if any(kw in lower for kw in ["closes", "closing", "applications close"]) and not closing_date:
                    closing_date = line
                if any(kw in lower for kw in ["ongoing", "non-ongoing", "non ongoing", "casual", "contract"]) and not job_type:
                    job_type = line
                if any(kw in lower for kw in [
                    "canberra", "act", "sydney", "nsw", "melbourne", "vic", "brisbane", "qld",
                    "perth", "wa", "adelaide", "sa", "darwin", "nt", "hobart", "tas", "multiple",
                ]) and not location:
                    location = line

            if title:
                jobs.append(Job(
                    title=title, agency=agency, location=location,
                    classification=classification, salary=salary,
                    closing_date=closing_date, job_type=job_type, url=job_url,
                ))
        except Exception:
            continue

    return jobs


# ---------------------------------------------------------------------------
# Main scrape logic
# ---------------------------------------------------------------------------

def scrape_jobs(
    url: str,
    pages: int = 1,
    headless: bool = True,
    debug: bool = False,
) -> list[Job]:
    all_jobs: list[Job] = []
    captured_api_jobs: list[Job] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_BINARY,
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # --- Intercept API responses ---
        def on_response(response: Response):
            content_type = response.headers.get("content-type", "")
            req_url = response.url
            if response.status != 200:
                return
            # Target Salesforce Aura / Apex REST / LWC data endpoints
            if not any(pat in req_url for pat in [
                "aura", "apexrest", "/data/", "lwc", "job", "vacancy", "search"
            ]):
                return
            if "json" not in content_type and "javascript" not in content_type:
                return
            try:
                body = response.json()
                found = _extract_jobs_from_api_payload(body)
                if found:
                    print(f"  [API] Captured {len(found)} jobs from {req_url}")
                    captured_api_jobs.extend(found)
            except Exception:
                pass

        page.on("response", on_response)

        for page_num in range(pages):
            print(f"  Fetching page {page_num + 1}: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"  Navigation error: {e}")
                break

            # Wait for JS rendering
            page.wait_for_timeout(5_000)

            if debug:
                fname = f"debug_page_{page_num + 1}.html"
                with open(fname, "w") as f:
                    f.write(page.content())
                print(f"  Saved debug HTML to {fname}")

            # Try DOM parsing
            dom_jobs = _extract_jobs_from_dom(page)
            if dom_jobs:
                print(f"  [DOM] Found {len(dom_jobs)} jobs on page {page_num + 1}")
                all_jobs.extend(dom_jobs)
            else:
                print(f"  [DOM] No job cards found on page {page_num + 1}")

            # Paginate
            if page_num < pages - 1:
                next_btn = page.query_selector(
                    "button:has-text('Next'), [aria-label='Next'], [class*='next-page']"
                )
                if next_btn and next_btn.is_enabled():
                    next_btn.click()
                    page.wait_for_timeout(3_000)
                else:
                    print("  No more pages.")
                    break

        browser.close()

    # Prefer API-captured jobs (richer data); fall back to DOM
    if captured_api_jobs:
        print(f"\n  Using {len(captured_api_jobs)} jobs captured from API responses.")
        return captured_api_jobs
    return all_jobs


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_jobs(jobs: list[Job]) -> None:
    if not jobs:
        print("\nNo jobs found.")
        print(
            "\nNote: apsjobs.gov.au restricts access by IP. "
            "Run this script on a local machine with normal internet access."
        )
        return

    print(f"\n{'='*70}")
    print(f"  {len(jobs)} JOB LISTING(S) FOUND")
    print(f"{'='*70}\n")

    for i, job in enumerate(jobs, 1):
        print(f"[{i}] {job.title}")
        if job.agency:
            print(f"    Agency:         {job.agency}")
        if job.location:
            print(f"    Location:       {job.location}")
        if job.classification:
            print(f"    Classification: {job.classification}")
        if job.salary:
            print(f"    Salary:         {job.salary}")
        if job.job_type:
            print(f"    Type:           {job.job_type}")
        if job.closing_date:
            print(f"    Closing:        {job.closing_date}")
        if job.url:
            print(f"    URL:            {job.url}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape APS job listings using browser automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scraper.py
  python3 scraper.py --url "https://www.apsjobs.gov.au/s/job-search?category=Data"
  python3 scraper.py --pages 3 --output jobs.json
  python3 scraper.py --debug        # save page HTML for inspection
        """,
    )
    parser.add_argument(
        "--url",
        default=(
            "https://www.apsjobs.gov.au/s/job-search"
            "?category=Data%3BInfo%2FComm%20Tech%20%28ICT%29&state=ACT&offset=45"
        ),
        help="APS Jobs search URL (default: ICT/Data jobs in ACT, page 4)",
    )
    parser.add_argument("--pages", type=int, default=1, help="Number of pages to scrape")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print results as JSON")
    parser.add_argument("--debug", action="store_true", help="Save page HTML for inspection")
    parser.add_argument("--output", metavar="FILE", help="Save results to a JSON file")
    args = parser.parse_args()

    print(f"Scraping: {args.url}\n")
    jobs = scrape_jobs(
        url=args.url,
        pages=args.pages,
        headless=not args.no_headless,
        debug=args.debug,
    )

    if args.as_json or args.output:
        data = [asdict(j) for j in jobs]
        if args.output:
            with open(args.output, "w") as f:
                json.dump(data, f, indent=2)
            print(f"Saved {len(jobs)} jobs to {args.output}")
        if args.as_json:
            print(json.dumps(data, indent=2))
    else:
        print_jobs(jobs)


if __name__ == "__main__":
    main()
