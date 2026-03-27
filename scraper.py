"""
University of Auckland Undergraduate Programme Scraper (Playwright)

Scrapes bachelor degree pages from the UoA study options listing.
Output: uoa_courses.csv  (written incrementally — crash-safe)
"""

import asyncio
import csv
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError as PlaywrightTimeout,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STUDY_OPTIONS_URL = (
    "https://www.auckland.ac.nz/en/study/study-options/find-a-study-option.html"
)
OUTPUT_CSV = "uoa_courses.csv"
PAGE_DELAY = 0.5        # seconds between pages
MAX_TIMEOUT = 8000      # ms — hard cap for every Playwright timeout
TAB_WAIT = 1.5          # seconds to sleep after clicking a tab (then proceed regardless)
MAX_RETRIES = 3         # page-level retries on error/timeout

# Degree-subject keywords used to detect conjoint URLs (issue 6)
_SUBJECTS = [
    "arts", "commerce", "science", "engineering",
    "design", "education", "music", "laws",
]


# ---------------------------------------------------------------------------
# Data model  — points hardcoded to 360 (issue 4)
# ---------------------------------------------------------------------------

@dataclass
class Programme:
    full_name: str = ""
    short_name: str = ""
    description: str = ""
    duration: str = ""
    points: str = "360"     # all UoA undergrad degrees are 360 points
    faculty: str = ""
    ncea_rank_score: str = ""
    required_subjects: str = ""
    additional_requirements: str = ""
    domestic_fees: str = ""
    scholarships: str = ""
    url: str = ""


# ---------------------------------------------------------------------------
# DOM helpers — all use query_selector (instant, no blocking)
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


async def _text(page: Page, selector: str) -> str:
    """Return inner text of the first matching element, or ''."""
    try:
        el = await page.query_selector(selector)
        if el:
            return _clean(await el.inner_text())
    except Exception:
        pass
    return ""


async def _texts(page: Page, selector: str) -> list[str]:
    """Return inner texts of all matching elements."""
    try:
        els = await page.query_selector_all(selector)
        return [_clean(await el.inner_text()) for el in els if await el.inner_text()]
    except Exception:
        return []


async def _body_text(page: Page) -> str:
    """Full visible body text — instant JS call."""
    try:
        return await page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


async def _tab_panel_text(page: Page) -> str:
    """
    Text of the currently active/visible tab panel only.
    Falls back to body text if no panel can be identified.
    Used so that keywords (e.g. UCAT) from *other* tabs don't bleed in.
    """
    try:
        return await page.evaluate("""() => {
            const candidates = [
                '[role="tabpanel"]:not([hidden]):not([aria-hidden="true"])',
                '.tab-content .tab-pane.active',
                '.tab-content.active',
                '[class*="tab"][class*="active"]',
            ];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) return el.innerText;
            }
            return document.body.innerText;
        }""")
    except Exception:
        return await _body_text(page)


async def _click_tab(page: Page, *labels: str) -> bool:
    """Click the first tab element whose text matches any label. Instant."""
    for label in labels:
        for sel in [
            f"a:has-text('{label}')",
            f"button:has-text('{label}')",
            f"[role='tab']:has-text('{label}')",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------------------
# URL filtering (issues 6 & 7)
# ---------------------------------------------------------------------------

def _should_skip_url(url: str) -> bool:
    """
    Return True for URLs that should be excluded:
      - Conjoint / double-degree URLs (issue 6)
      - Postgraduate honours extensions, keeping only engineering & laws (issue 7)
    """
    slug = url.lower().rstrip("/").split("/")[-1].replace(".html", "")

    # Issue 6: conjoint / double-degree
    if "conjoint" in slug:
        return True
    if slug.count("bachelor") > 1:
        return True
    if sum(1 for s in _SUBJECTS if s in slug) > 1:
        return True

    # Issue 7: postgrad honours — only keep engineering and laws honours
    if "honours" in slug and "engineering" not in slug and "laws" not in slug:
        return True

    return False


# ---------------------------------------------------------------------------
# 1. Link discovery
# ---------------------------------------------------------------------------

async def get_programme_links(page: Page) -> list[str]:
    """Load the study-options index and return filtered bachelor programme URLs."""
    log.info("Loading study options index …")
    await page.goto(STUDY_OPTIONS_URL, wait_until="domcontentloaded", timeout=MAX_TIMEOUT)

    try:
        await page.wait_for_selector("a[href]", timeout=MAX_TIMEOUT)
    except PlaywrightTimeout:
        log.warning("Timed out waiting for links on index page")

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(1)

    all_links: list[str] = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('a[href]'))
            .map(a => a.href)
            .filter(h =>
                h.includes('/find-a-study-option/') &&
                h.endsWith('.html') &&
                !h.endsWith('find-a-study-option.html')
            )
    """)

    seen: set[str] = set()
    unique: list[str] = []
    for link in all_links:
        clean = link.split("?")[0].split("#")[0]
        if (
            clean not in seen
            and "bachelor" in clean.lower()
            and not _should_skip_url(clean)
        ):
            seen.add(clean)
            unique.append(clean)

    log.info("Found %d bachelor programme links (after filtering)", len(unique))
    return unique


# ---------------------------------------------------------------------------
# 2. Main page scraping
# ---------------------------------------------------------------------------

def _parse_name_and_abbreviation(heading: str) -> tuple[str, str]:
    """
    Split the h1 heading into (full_name, short_name).

    Handles patterns like:
      "Bachelor of Arts BA"              → ("Bachelor of Arts", "BA")
      "Bachelor of Science BSc"          → ("Bachelor of Science", "BSc")
      "Bachelor of Engineering (Honours) BE(Hons)"
                                         → ("Bachelor of Engineering (Honours)", "BE(Hons)")
      "Bachelor of Laws (LLB)"           → ("Bachelor of Laws", "LLB")

    The abbreviation is identified as a trailing token that:
      • Starts with 1–4 uppercase letters
      • May have up to 4 lowercase letters (e.g. BSc, BCom, BEd)
      • May be followed by a parenthesised suffix like (Hons)
      • Is at the very end of the string (optionally inside outer parentheses)
    """
    heading = heading.strip()

    # Pattern A: abbreviation wrapped in its own trailing parens, e.g. "(BE(Hons))" or "(BA)"
    m = re.search(r'\s+\(([A-Z]{1,4}[a-z]{0,4}(?:\([A-Za-z]+\))?)\)\s*$', heading)
    if m:
        return heading[: m.start()].strip(), m.group(1)

    # Pattern B: abbreviation as a bare trailing token, e.g. "... BE(Hons)" or "... BA"
    m = re.search(r'\s+([A-Z]{1,4}[a-z]{0,4}(?:\([A-Za-z]+\))?)\s*$', heading)
    if m:
        return heading[: m.start()].strip(), m.group(1)

    return heading, ""


async def scrape_main_page(page: Page, prog: Programme) -> None:
    """Extract name, description, duration, faculty from the main page."""

    # --- Name + abbreviation (issue 5) ---
    heading = ""
    for sel in ["h1.page-header__title", "h1"]:
        heading = await _text(page, sel)
        if heading:
            break
    prog.full_name, prog.short_name = _parse_name_and_abbreviation(heading)

    # --- Description — skip breadcrumbs and bare faculty labels (issue 3) ---
    desc = ""
    for sel in [
        "[class*='programme-overview'] p",
        "[class*='overview'] p",
        ".rich-text > p",
        ".rich-text p",
    ]:
        t = await _text(page, sel)
        if t and "Breadcrumbs" not in t and len(t) > 60:
            desc = t
            break

    if not desc:
        for p in await _texts(page, "main p"):
            if "Breadcrumbs" in p:
                continue
            # Skip short strings that are just a faculty/school name
            if len(p) < 60 and re.match(r'^[A-Z][A-Za-z\s&]+$', p):
                continue
            desc = p
            break

    # Strip any "Breadcrumbs List." prefix that slipped through
    desc = re.sub(r"^Breadcrumbs\s+List\.?\s*", "", desc, flags=re.I).strip()
    prog.description = _clean(desc)

    # --- Duration (issue 2) — page shows "Full-time: 3 years" ---
    body = await _body_text(page)

    dur = re.search(r"Full[\s-]*time[:\s]+(\d+(?:\.\d+)?)\s*years?", body, re.I)
    if not dur:
        dur = re.search(r"Duration[:\s]+(\d+(?:\.\d+)?)\s*years?", body, re.I)
    if dur:
        prog.duration = dur.group(1) + " years"

    # --- Faculty ---
    fac = re.search(
        r"(?:Faculty|School|Taught by)[:\s]+([A-Z][A-Za-z &,()]+?)(?:\s{2,}|\n|$)",
        body, re.I,
    )
    if fac:
        prog.faculty = _clean(fac.group(1))


# ---------------------------------------------------------------------------
# 3. Entry Requirements tab
# ---------------------------------------------------------------------------

def _extract_rank_score(text: str) -> str:
    """
    Extract the NCEA rank score from entry requirements text.

    UoA pages show a card: Qualification → NCEA → Score required → <number>

    We ONLY return a number when the "Score required" label is present nearby.
    We do NOT fall back to any random number — if the label isn't found,
    return '' rather than guess (issue 1).
    """
    # Pattern 1: "Score required" immediately followed by the number
    m = re.search(r"Score\s+required[:\s]*(\d{2,3})", text, re.I)
    if m and 100 <= int(m.group(1)) <= 320:
        return m.group(1)

    # Pattern 2: "NCEA" appears, then "Score required", then the number
    # (handles multi-line card layout where they're on separate lines)
    m = re.search(
        r"NCEA.{0,150}Score\s+required[:\s]*(\d{2,3})",
        text, re.I | re.S,
    )
    if m and 100 <= int(m.group(1)) <= 320:
        return m.group(1)

    # Pattern 3: "rank score" label near a number
    m = re.search(r"rank\s+score[^\d]{0,30}(\d{2,3})", text, re.I)
    if m and 100 <= int(m.group(1)) <= 320:
        return m.group(1)

    # No labelled rank score found — leave blank (do NOT default to 120)
    return ""


async def scrape_entry_requirements(page: Page, prog: Programme) -> None:
    """Click the Entry Requirements tab, wait TAB_WAIT, then scrape."""
    clicked = await _click_tab(
        page,
        "Entry requirements",
        "Entry Requirements",
        "Admission",
        "Requirements",
    )
    if not clicked:
        return

    # Fixed wait — do NOT use wait_for_selector after tab click
    await asyncio.sleep(TAB_WAIT)

    # Use tab panel text only (issue 10) so other tabs' keywords can't bleed in
    panel = await _tab_panel_text(page)
    panel = re.sub(r"\s+", " ", panel)

    # --- Rank score (issue 1) ---
    prog.ncea_rank_score = _extract_rank_score(panel)

    # --- Required subjects at Level 3 ---
    subj_matches = re.findall(r"(?:NCEA\s*)?Level\s*3[^.;\n]{0,100}", panel, re.I)
    if subj_matches:
        prog.required_subjects = "; ".join(_clean(s) for s in subj_matches[:8])

    # --- Additional requirements — only keywords explicitly on this page (issue 10) ---
    extras: list[str] = []
    for kw in [
        "UCAT", "portfolio", "audition", "interview",
        "entrance examination", "CASPer", "IELTS",
        "English proficiency", "health check",
    ]:
        if re.search(r"\b" + re.escape(kw) + r"\b", panel, re.I):
            extras.append(kw)
    if extras:
        prog.additional_requirements = ", ".join(extras)


# ---------------------------------------------------------------------------
# 4. Fees & Scholarships tab
# ---------------------------------------------------------------------------

# Generic scholarship phrases to exclude (issue 8)
_GENERIC_SCHOL_RE = re.compile(
    r"Find\s+out|financial\s+support\s+information|"
    r"^University\s+of\s+Auckland\s+Scholarship\s*$",
    re.I,
)


async def scrape_fees_scholarships(page: Page, prog: Programme) -> None:
    """Click the Fees & Scholarships tab, wait TAB_WAIT, then scrape."""
    clicked = await _click_tab(
        page,
        "Fees and scholarships",
        "Fees & Scholarships",
        "Fees",
        "Tuition",
    )
    if not clicked:
        return

    await asyncio.sleep(TAB_WAIT)

    panel = await _tab_panel_text(page)

    # --- Domestic fees ---
    fee_m = re.search(
        r"(?:domestic|NZ\s*citizen|resident)[^\$\n]{0,120}\$\s*([\d,]+(?:\.\d{2})?)",
        panel, re.I,
    )
    if not fee_m:
        fee_m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", panel)
    if fee_m:
        prog.domestic_fees = "$" + fee_m.group(1)

    # --- Named scholarships only (issue 8) ---
    raw_schols = re.findall(
        r"[A-Z][A-Za-z\s&']{5,60}(?:Scholarship|Award|Bursary|Prize)",
        panel,
    )
    named = []
    seen_schols: set[str] = set()
    for s in raw_schols:
        s = _clean(s)
        if s in seen_schols:
            continue
        seen_schols.add(s)
        if not _GENERIC_SCHOL_RE.search(s):
            named.append(s)
    prog.scholarships = "; ".join(named[:5])


# ---------------------------------------------------------------------------
# 5. Per-programme orchestrator
# ---------------------------------------------------------------------------

async def scrape_programme(page: Page, url: str) -> Optional[Programme]:
    """Scrape one programme page; retry up to MAX_RETRIES times on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        prog = Programme(url=url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=MAX_TIMEOUT)
            # One wait to confirm the DOM is ready before any query_selector calls
            await page.wait_for_selector("h1, main", timeout=MAX_TIMEOUT)

            await scrape_main_page(page, prog)

            # Issue 9: skip index page or misloaded pages
            if not prog.full_name or prog.full_name.lower() == "find a study option":
                log.warning("  Skipping non-programme page: %s", url)
                return None

            await scrape_entry_requirements(page, prog)
            await scrape_fees_scholarships(page, prog)
            return prog

        except (PlaywrightTimeout, asyncio.TimeoutError):
            log.warning("  Timeout (attempt %d/%d): %s", attempt, MAX_RETRIES, url)
        except Exception as e:
            log.warning("  Error (attempt %d/%d) %s: %s", attempt, MAX_RETRIES, url, e)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(1)

    log.error("  GAVE UP after %d attempts: %s", MAX_RETRIES, url)
    return None


# ---------------------------------------------------------------------------
# 6. CSV helpers
# ---------------------------------------------------------------------------

def _open_csv(path: str):
    """Delete existing file, open fresh, write header. Return (file, writer)."""
    Path(path).unlink(missing_ok=True)
    fieldnames = list(asdict(Programme()).keys())
    f = open(path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    f.flush()
    return f, writer


def _append_row(writer, f, prog: Programme) -> None:
    """Write one row and flush to disk immediately."""
    writer.writerow(asdict(prog))
    f.flush()


# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------

async def main() -> None:
    csv_file, csv_writer = _open_csv(OUTPUT_CSV)
    log.info("Fresh %s opened for incremental writing", OUTPUT_CSV)
    count = 0

    try:
        async with async_playwright() as pw:
            # Use pre-cached Chromium if the expected version isn't downloaded
            chrome_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")
            if not chrome_path:
                for candidate in [
                    "/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome",
                    "/root/.cache/ms-playwright/chromium_headless_shell-1194/"
                    "chrome-linux/chrome-headless-shell",
                ]:
                    if os.path.exists(candidate):
                        chrome_path = candidate
                        break

            launch_kw: dict = {"headless": True}
            if chrome_path:
                log.info("Using Chromium: %s", chrome_path)
                launch_kw["executable_path"] = chrome_path

            browser = await pw.chromium.launch(**launch_kw)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-NZ",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            links = await get_programme_links(page)
            if not links:
                log.error("No programme links found — check the index page.")
                await browser.close()
                return

            for i, url in enumerate(links, 1):
                log.info("[%d/%d] %s", i, len(links), url)

                prog = await scrape_programme(page, url)
                if prog:
                    _append_row(csv_writer, csv_file, prog)
                    count += 1
                    log.info(
                        "  -> #%d  name=%r  rank=%r  fees=%r  duration=%r",
                        count,
                        prog.full_name,
                        prog.ncea_rank_score,
                        prog.domestic_fees,
                        prog.duration,
                    )

                if i < len(links):
                    await asyncio.sleep(PAGE_DELAY)

            await browser.close()
    finally:
        csv_file.close()

    log.info("Done. %d programmes saved to %s", count, OUTPUT_CSV)


if __name__ == "__main__":
    asyncio.run(main())
