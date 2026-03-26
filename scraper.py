"""
University of Auckland Undergraduate Programme Scraper (Playwright)

Scrapes bachelor degree pages from the UoA study options listing.
Collects: name, abbreviation, description, duration, points, faculty,
          NCEA rank score, required subjects, additional requirements,
          domestic fees, and scholarships.

Output: uoa_courses.csv (written incrementally — crash-safe)
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
TAB_WAIT = 1.5          # seconds to sleep after clicking a tab
MAX_RETRIES = 3         # page-level retries


@dataclass
class Programme:
    full_name: str = ""
    short_name: str = ""
    description: str = ""
    duration: str = ""
    points: str = ""
    faculty: str = ""
    ncea_rank_score: str = ""
    required_subjects: str = ""
    additional_requirements: str = ""
    domestic_fees: str = ""
    scholarships: str = ""
    url: str = ""


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text or "").strip()


async def _text(page: Page, selector: str) -> str:
    """Instant query_selector — returns inner text or ''."""
    try:
        el = await page.query_selector(selector)
        if el:
            return _clean(await el.inner_text())
    except Exception:
        pass
    return ""


async def _texts(page: Page, selector: str) -> list[str]:
    """Return inner texts for *all* matching elements (instant)."""
    try:
        els = await page.query_selector_all(selector)
        out = []
        for el in els:
            t = _clean(await el.inner_text())
            if t:
                out.append(t)
        return out
    except Exception:
        return []


async def _page_text(page: Page) -> str:
    """Get the full visible text of the page body (instant JS call)."""
    try:
        return await page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


async def _click_tab(page: Page, *labels: str) -> bool:
    """Click the first tab whose text matches any of *labels*. Instant."""
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
# 1. Link discovery
# ---------------------------------------------------------------------------

async def get_programme_links(page: Page) -> list[str]:
    """Load the study-options index and collect all bachelor programme URLs."""
    log.info("Loading study options index …")
    await page.goto(STUDY_OPTIONS_URL, wait_until="domcontentloaded",
                    timeout=MAX_TIMEOUT)

    # One wait for the page to have some links rendered
    try:
        await page.wait_for_selector("a[href]", timeout=MAX_TIMEOUT)
    except PlaywrightTimeout:
        log.warning("Timed out waiting for links on index page")

    # Scroll to trigger any lazy-load
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(1)

    all_links: list[str] = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a[href]'))
            .map(a => a.href)
            .filter(h =>
                h.includes('/find-a-study-option/') &&
                h.endsWith('.html') &&
                !h.endsWith('find-a-study-option.html')
            );
    }""")

    # Deduplicate, keep only bachelor URLs
    seen: set[str] = set()
    unique: list[str] = []
    for link in all_links:
        clean = link.split("?")[0].split("#")[0]
        if clean not in seen and "bachelor" in clean.lower():
            seen.add(clean)
            unique.append(clean)

    log.info("Found %d bachelor programme links", len(unique))
    return unique


# ---------------------------------------------------------------------------
# 2. Main page scraping  (instant — no waits)
# ---------------------------------------------------------------------------

async def scrape_main_page(page: Page, prog: Programme) -> None:
    # --- Name ---
    for sel in ["h1.page-header__title", "h1"]:
        t = await _text(page, sel)
        if t:
            prog.full_name = t
            break

    # Strip abbreviation from name: "Bachelor of Foo (BFoo)" → name + short
    if prog.full_name:
        m = re.search(r"\(([A-Z][A-Za-z()/ ]{1,30})\)\s*$", prog.full_name)
        if m:
            prog.short_name = m.group(1).strip()
            prog.full_name = prog.full_name[: m.start()].strip()

    # --- Programme overview paragraph ---
    # Try the "Programme overview" section first
    desc = await _text(page, "[id*='overview'] p, [id*='Overview'] p")
    if not desc:
        desc = await _text(page, ".rich-text p")
    if not desc:
        parts = await _texts(page, "main p")
        desc = " ".join(parts[:3])
    prog.description = desc

    # --- Duration, Points, Faculty from whole page text ---
    body = await _page_text(page)

    dur = re.search(r"(\d+(?:\.\d+)?)\s*years?\s*full[\s-]*time", body, re.I)
    if not dur:
        dur = re.search(r"Duration[:\s]+(\d+(?:\.\d+)?\s*years?)", body, re.I)
    if dur:
        prog.duration = _clean(dur.group(0))

    pts = re.search(r"(\d{2,3})\s*points", body, re.I)
    if pts:
        prog.points = pts.group(1)

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
    Pull NCEA rank score from the entry requirements text.

    On UoA pages the rank score appears in a styled card:
        Qualification → NCEA → Score required → 150
    So we look for "Score required" near a 3-digit number first,
    then fall back to broader patterns.
    """
    # Pattern 1: "Score required" followed by a number 100-320
    m = re.search(r"Score\s*required[:\s]*(\d{2,3})", text, re.I)
    if m and 100 <= int(m.group(1)) <= 320:
        return m.group(1)

    # Pattern 2: "NCEA" nearby then a standalone number 100-320
    m = re.search(r"NCEA[^0-9]{0,80}?(\d{3})", text, re.I)
    if m and 100 <= int(m.group(1)) <= 320:
        return m.group(1)

    # Pattern 3: "rank score" near a number
    m = re.search(r"rank\s*score[^\d]{0,30}(\d{2,3})", text, re.I)
    if m and 100 <= int(m.group(1)) <= 320:
        return m.group(1)

    # Pattern 4: any standalone 3-digit number in the 120-320 range
    for m in re.finditer(r"\b(\d{3})\b", text):
        v = int(m.group(1))
        if 120 <= v <= 320:
            return m.group(1)

    return ""


async def scrape_entry_requirements(page: Page, prog: Programme) -> None:
    """Click Entry Requirements tab, wait TAB_WAIT, then scrape."""
    clicked = await _click_tab(
        page,
        "Entry requirements",
        "Entry Requirements",
        "Admission",
        "Requirements",
    )
    if not clicked:
        return

    # Fixed wait — no wait_for_selector
    await asyncio.sleep(TAB_WAIT)

    # --- Rank score: try targeted selectors first ---
    for sel in [
        ".score-required",
        "[class*='score']",
        "[class*='rank']",
    ]:
        t = await _text(page, sel)
        if t:
            m = re.search(r"(\d{2,3})", t)
            if m and 100 <= int(m.group(1)) <= 320:
                prog.ncea_rank_score = m.group(1)
                break

    # Fall back to full-text search of the visible tab content
    panel = await _page_text(page)

    if not prog.ncea_rank_score:
        prog.ncea_rank_score = _extract_rank_score(panel)

    # --- Required subjects at Level 3 ---
    subj_matches = re.findall(
        r"(?:NCEA\s*)?Level\s*3[^.;\n]{0,100}",
        panel, re.I,
    )
    if subj_matches:
        prog.required_subjects = "; ".join(_clean(s) for s in subj_matches[:8])

    # --- Additional requirements (keywords) ---
    extras: list[str] = []
    for kw in [
        "UCAT", "portfolio", "audition", "interview",
        "entrance examination", "CASPer", "IELTS",
        "English proficiency", "health check",
    ]:
        if kw.lower() in panel.lower():
            extras.append(kw)
    if extras:
        prog.additional_requirements = ", ".join(extras)


# ---------------------------------------------------------------------------
# 4. Fees & Scholarships tab
# ---------------------------------------------------------------------------

async def scrape_fees_scholarships(page: Page, prog: Programme) -> None:
    """Click Fees tab, wait TAB_WAIT, then scrape."""
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

    panel = await _page_text(page)

    # --- Domestic fees ---
    fee_m = re.search(
        r"(?:domestic|NZ\s*citizen|resident)[^\$\n]{0,120}\$\s*([\d,]+(?:\.\d{2})?)",
        panel, re.I,
    )
    if not fee_m:
        fee_m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", panel)
    if fee_m:
        prog.domestic_fees = "$" + fee_m.group(1)

    # --- Scholarships ---
    schol = re.findall(
        r"[A-Z][A-Za-z\s&']{5,60}(?:Scholarship|Award|Bursary|Prize)",
        panel,
    )
    if schol:
        prog.scholarships = "; ".join(_clean(s) for s in schol[:5])


# ---------------------------------------------------------------------------
# 5. Per-programme orchestrator (with retries)
# ---------------------------------------------------------------------------

async def scrape_programme(page: Page, url: str) -> Optional[Programme]:
    """Load one programme page, scrape all three sections. Retries on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        prog = Programme(url=url)
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=MAX_TIMEOUT)
            # Single wait to confirm the page rendered
            await page.wait_for_selector("h1, main", timeout=MAX_TIMEOUT)

            await scrape_main_page(page, prog)
            await scrape_entry_requirements(page, prog)
            await scrape_fees_scholarships(page, prog)
            return prog

        except (PlaywrightTimeout, asyncio.TimeoutError):
            log.warning("  Timeout (attempt %d/%d) %s", attempt, MAX_RETRIES, url)
        except Exception as e:
            log.warning("  Error (attempt %d/%d) %s: %s", attempt, MAX_RETRIES, url, e)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(1)

    log.error("  GAVE UP on %s after %d attempts", url, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# 6. CSV helpers
# ---------------------------------------------------------------------------

def _open_csv(path: str):
    """Delete any existing file, open fresh, write header. Return (file, writer)."""
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
    log.info("Fresh %s created for incremental writing", OUTPUT_CSV)
    count = 0

    try:
        async with async_playwright() as pw:
            # Prefer a pre-cached Chromium if the expected version is missing
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
                log.info("Using Chromium at: %s", chrome_path)
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

            # ---- discover links ----
            links = await get_programme_links(page)
            if not links:
                log.error("No programme links found — check the index page.")
                await browser.close()
                return

            # ---- scrape each programme ----
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
