"""
University of Auckland Undergraduate Programme Scraper
Scrapes degree programme information from the UoA study options page.

Collects: programme name, abbreviation, description, duration, points,
          faculty, entry requirements (NCEA, subjects, additional),
          domestic fees, and scholarships.

Output: uoa_courses.csv
"""

import asyncio
import csv
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

STUDY_OPTIONS_URL = (
    "https://www.auckland.ac.nz/en/study/study-options/find-a-study-option.html"
)

# Seconds to wait between navigating to each programme page
PAGE_DELAY = 2

OUTPUT_CSV = "uoa_courses.csv"


@dataclass
class Programme:
    full_name: str = ""
    short_name: str = ""
    description: str = ""
    duration: str = ""
    points: str = ""
    faculty: str = ""
    # Entry requirements
    ncea_rank_score: str = ""
    required_subjects: str = ""
    additional_requirements: str = ""
    # Fees & scholarships
    domestic_fees: str = ""
    scholarships: str = ""
    url: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text or "").strip()


async def _safe_text(page: Page, selector: str, timeout: int = 5000) -> str:
    """Return inner text for the first matching element, or '' on failure."""
    try:
        el = await page.wait_for_selector(selector, timeout=timeout)
        if el:
            return _clean(await el.inner_text())
    except (PlaywrightTimeout, Exception):
        pass
    return ""


async def _all_text(page: Page, selector: str) -> list[str]:
    """Return a list of inner texts for all matching elements."""
    try:
        elements = await page.query_selector_all(selector)
        texts = []
        for el in elements:
            t = _clean(await el.inner_text())
            if t:
                texts.append(t)
        return texts
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Link discovery
# ---------------------------------------------------------------------------

async def get_programme_links(page: Page) -> list[str]:
    """
    Navigate to the study-options index page and collect all programme URLs.
    The page uses JavaScript to render a filterable list of study options.
    """
    log.info("Loading study options index: %s", STUDY_OPTIONS_URL)
    await page.goto(STUDY_OPTIONS_URL, wait_until="networkidle", timeout=60000)

    # Wait for some course links to appear
    try:
        await page.wait_for_selector("a[href]", timeout=15000)
    except PlaywrightTimeout:
        log.warning("Timed out waiting for links on index page")

    # Scroll to bottom to trigger lazy-load
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(2)

    # Collect all internal programme links
    # UoA study option URLs typically match:
    #   /en/study/study-options/find-a-study-option/<programme-slug>.html
    all_links: list[str] = await page.evaluate(
        """() => {
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            return anchors
                .map(a => a.href)
                .filter(href =>
                    href.includes('/find-a-study-option/') &&
                    href.endsWith('.html') &&
                    !href.endsWith('find-a-study-option.html')
                );
        }"""
    )

    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for link in all_links:
        # Drop fragment / query string variations
        clean = link.split("?")[0].split("#")[0]
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)

    log.info("Found %d programme links", len(unique))
    return unique


# ---------------------------------------------------------------------------
# Main page scraping
# ---------------------------------------------------------------------------

async def scrape_main_info(page: Page, prog: Programme) -> None:
    """Extract name, description, duration, points, faculty from the main page."""

    # --- Full name: usually in the main heading ---
    for sel in ["h1.page-header__title", "h1", ".programme-title", ".study-option-title"]:
        text = await _safe_text(page, sel, timeout=5000)
        if text:
            prog.full_name = text
            break

    # --- Short name / abbreviation ---
    # Often appears in parentheses after the name, or in a dedicated element
    for sel in [
        ".programme-abbreviation",
        ".short-name",
        "[class*='abbreviation']",
        "[class*='short-name']",
    ]:
        text = await _safe_text(page, sel, timeout=2000)
        if text:
            prog.short_name = text
            break

    # Fallback: extract abbreviation from the page heading if bracketed form present
    if not prog.short_name and prog.full_name:
        m = re.search(r"\(([A-Z][A-Za-z()\s]{1,30})\)", prog.full_name)
        if m:
            prog.short_name = m.group(1).strip()
            prog.full_name = prog.full_name[: prog.full_name.rfind("(")].strip()

    # --- Description / overview ---
    for sel in [
        ".programme-description",
        ".programme-overview",
        "[class*='overview'] p",
        ".content-block p",
        ".rich-text p",
        "main p",
    ]:
        texts = await _all_text(page, sel)
        if texts:
            prog.description = " ".join(texts[:3])  # first few paragraphs
            break

    # --- Duration, Points, Faculty ---
    # These often appear in a structured "key facts" panel / definition list
    key_facts_html: str = await page.evaluate(
        """() => {
            // Try several candidate containers
            const candidates = [
                document.querySelector('.key-facts'),
                document.querySelector('.programme-details'),
                document.querySelector('[class*="key-fact"]'),
                document.querySelector('[class*="programme-info"]'),
                document.querySelector('.study-details'),
                document.querySelector('table'),
                document.querySelector('dl'),
            ];
            for (const el of candidates) {
                if (el) return el.innerHTML;
            }
            return document.body.innerHTML;
        }"""
    )

    # Parse key facts via regex on the HTML text representation
    plain = re.sub(r"<[^>]+>", " ", key_facts_html)
    plain = re.sub(r"\s+", " ", plain)

    dur_m = re.search(
        r"(?:Duration|Length)[:\s]+([0-9]+(?:\.[0-9]+)?\s*years?[^<\n]{0,60})",
        plain, re.I
    )
    if dur_m:
        prog.duration = _clean(dur_m.group(1))

    pts_m = re.search(
        r"(?:Points|Credits)[:\s]+([0-9]+(?:\s*points?)?)",
        plain, re.I
    )
    if pts_m:
        prog.points = _clean(pts_m.group(1))

    fac_m = re.search(
        r"(?:Faculty|School)[:\s]+([A-Za-z &,]+?)(?:\s{2,}|[|]|\n|$)",
        plain, re.I
    )
    if fac_m:
        prog.faculty = _clean(fac_m.group(1))

    # Also check dedicated selectors
    for sel, attr in [
        (".duration", "duration"),
        (".points", "points"),
        (".faculty", "faculty"),
        ("[class*='duration']", "duration"),
        ("[class*='points']", "points"),
        ("[class*='faculty']", "faculty"),
    ]:
        if getattr(prog, attr):
            continue
        text = await _safe_text(page, sel, timeout=2000)
        if text:
            setattr(prog, attr, text)


# ---------------------------------------------------------------------------
# Tab helpers
# ---------------------------------------------------------------------------

async def _click_tab(page: Page, *label_patterns: str) -> bool:
    """
    Try to click a tab whose visible text matches any of the given patterns.
    Returns True if a tab was found and clicked.
    """
    for pattern in label_patterns:
        # Look for tab-like elements containing the label text
        try:
            # Try common tab selectors
            for sel in [
                f"button:has-text('{pattern}')",
                f"a:has-text('{pattern}')",
                f"[role='tab']:has-text('{pattern}')",
                f"li:has-text('{pattern}')",
                f".tab:has-text('{pattern}')",
                f"[class*='tab']:has-text('{pattern}')",
            ]:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    await asyncio.sleep(1.5)
                    return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Entry requirements tab
# ---------------------------------------------------------------------------

async def scrape_entry_requirements(page: Page, prog: Programme) -> None:
    """Click the Entry Requirements tab and extract relevant fields."""
    clicked = await _click_tab(
        page,
        "Entry requirements",
        "Entry Requirements",
        "Admission",
        "Requirements",
    )
    if not clicked:
        log.debug("No entry requirements tab found for %s", prog.full_name)
        return

    # Wait for content to load
    await asyncio.sleep(1)

    # Get the full text of the tab panel / page section
    try:
        panel_text: str = await page.evaluate(
            """() => {
                // Find the active/visible tab panel
                const selectors = [
                    '[role="tabpanel"]:not([hidden])',
                    '.tab-content.active',
                    '.tab-pane.active',
                    '.entry-requirements',
                    '[class*="entry-req"]',
                    '[class*="requirements"]',
                    'main',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) return el.innerText;
                }
                return document.body.innerText;
            }"""
        )
    except Exception:
        panel_text = ""

    panel_text = re.sub(r"\s+", " ", panel_text)

    # NCEA rank score
    rank_m = re.search(
        r"(?:rank\s*score|NCEA\s*rank)[^\d]*(\d{1,3})",
        panel_text, re.I
    )
    if rank_m:
        prog.ncea_rank_score = rank_m.group(1)

    # Required subjects — collect lines mentioning NCEA Level 3 subjects
    subj_matches = re.findall(
        r"(?:NCEA\s*)?Level\s*3[^.;,\n]{0,80}",
        panel_text, re.I
    )
    if subj_matches:
        prog.required_subjects = "; ".join(_clean(s) for s in subj_matches[:8])

    # Additional requirements keywords
    extras: list[str] = []
    for keyword in ["UCAT", "portfolio", "audition", "interview", "entrance examination",
                    "CASPer", "IELTS", "English proficiency", "health check"]:
        if keyword.lower() in panel_text.lower():
            extras.append(keyword)
    if extras:
        prog.additional_requirements = ", ".join(extras)


# ---------------------------------------------------------------------------
# Fees and scholarships tab
# ---------------------------------------------------------------------------

async def scrape_fees_scholarships(page: Page, prog: Programme) -> None:
    """Click the Fees & Scholarships tab and extract fee and scholarship info."""
    clicked = await _click_tab(
        page,
        "Fees and scholarships",
        "Fees & Scholarships",
        "Fees",
        "Tuition",
    )
    if not clicked:
        log.debug("No fees tab found for %s", prog.full_name)
        return

    await asyncio.sleep(1)

    try:
        panel_text: str = await page.evaluate(
            """() => {
                const selectors = [
                    '[role="tabpanel"]:not([hidden])',
                    '.tab-content.active',
                    '.tab-pane.active',
                    '[class*="fees"]',
                    '[class*="scholarship"]',
                    'main',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) return el.innerText;
                }
                return document.body.innerText;
            }"""
        )
    except Exception:
        panel_text = ""

    panel_text = re.sub(r"\s+", " ", panel_text)

    # Domestic fees — look for NZ dollar amounts near "domestic"
    fee_m = re.search(
        r"(?:domestic|NZ\s*citizen|resident)[^\$\n]{0,120}\$\s*([\d,]+(?:\.\d{2})?)",
        panel_text, re.I
    )
    if not fee_m:
        # Fallback: first dollar amount on the page
        fee_m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", panel_text)
    if fee_m:
        prog.domestic_fees = "$" + fee_m.group(1)

    # Scholarships — capture names/amounts near "scholarship" or "award"
    schol_matches = re.findall(
        r"(?:[A-Z][A-Za-z\s&']{5,60}(?:Scholarship|Award|Bursary|Prize)[^.\n]{0,120})",
        panel_text
    )
    if schol_matches:
        prog.scholarships = "; ".join(_clean(s) for s in schol_matches[:5])


# ---------------------------------------------------------------------------
# Per-programme scraper
# ---------------------------------------------------------------------------

async def scrape_programme(page: Page, url: str) -> Optional[Programme]:
    """Scrape a single programme page. Returns None on hard failure."""
    prog = Programme(url=url)
    try:
        log.info("  -> %s", url)
        await page.goto(url, wait_until="networkidle", timeout=60000)

        await scrape_main_info(page, prog)
        await scrape_entry_requirements(page, prog)
        await scrape_fees_scholarships(page, prog)

    except PlaywrightTimeout:
        log.warning("Timeout loading %s", url)
        if not prog.full_name:
            return None
    except Exception as e:
        log.warning("Error scraping %s: %s", url, e)
        if not prog.full_name:
            return None

    return prog


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def save_csv(programmes: list[Programme], path: str = OUTPUT_CSV) -> None:
    if not programmes:
        log.warning("No programmes to save.")
        return
    fieldnames = list(asdict(programmes[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in programmes:
            writer.writerow(asdict(p))
    log.info("Saved %d programmes to %s", len(programmes), path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    programmes: list[Programme] = []

    async with async_playwright() as pw:
        # Use pre-cached Chromium if the default version isn't downloaded
        import os
        chrome_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")
        if not chrome_path:
            candidates = [
                "/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome",
                "/root/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/chrome-headless-shell",
            ]
            for c in candidates:
                if os.path.exists(c):
                    chrome_path = c
                    break
        launch_kwargs = {"headless": True}
        if chrome_path:
            log.info("Using Chromium at: %s", chrome_path)
            launch_kwargs["executable_path"] = chrome_path
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-NZ",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Step 1: collect all programme links
        links = await get_programme_links(page)

        if not links:
            log.error("No programme links found — check the index page structure.")
            await browser.close()
            return

        # Step 2: scrape each programme
        for i, url in enumerate(links, 1):
            log.info("[%d/%d] Scraping: %s", i, len(links), url)

            prog = await scrape_programme(page, url)
            if prog:
                programmes.append(prog)
                log.info(
                    "    name=%r  duration=%r  points=%r  fees=%r",
                    prog.full_name, prog.duration, prog.points, prog.domestic_fees,
                )

            # Polite delay between pages
            if i < len(links):
                await asyncio.sleep(PAGE_DELAY)

        await browser.close()

    save_csv(programmes, OUTPUT_CSV)
    log.info("Done. %d programmes written to %s", len(programmes), OUTPUT_CSV)


if __name__ == "__main__":
    asyncio.run(main())
