"""
University of Auckland Course Scraper
Scrapes course information from https://courseoutline.auckland.ac.nz

Collects: course code, title, credits/points, prerequisites, corequisites,
          restriction, description, offered semesters, faculty, and more.
"""

import csv
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

BASE_URL = "https://courseoutline.auckland.ac.nz"
COURSE_LIST_URL = f"{BASE_URL}/dco/course"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
}

# Polite crawl delay in seconds between requests
CRAWL_DELAY = 1.5


@dataclass
class Course:
    code: str = ""                   # e.g. COMPSCI 101
    subject: str = ""                # e.g. COMPSCI
    catalog_number: str = ""         # e.g. 101
    title: str = ""                  # Full course title
    points: Optional[int] = None     # Credits / points (e.g. 15)
    description: str = ""
    prerequisites: str = ""
    corequisites: str = ""
    restriction: str = ""            # Courses you cannot take alongside
    offered: str = ""                # Semester(s) offered
    faculty: str = ""
    department: str = ""
    level: Optional[int] = None      # e.g. 100, 200, 300
    url: str = ""
    year: str = ""


class UoAScraper:
    def __init__(self, delay: float = CRAWL_DELAY, max_courses: int = 0):
        """
        Args:
            delay: seconds to wait between requests (be polite)
            max_courses: cap on number of courses to scrape (0 = unlimited)
        """
        self.delay = delay
        self.max_courses = max_courses
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # Fetching helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, retries: int = 3) -> Optional[BeautifulSoup]:
        """Fetch a URL and return a BeautifulSoup object, or None on failure."""
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                return BeautifulSoup(resp.text, "lxml")
            except requests.HTTPError as e:
                log.warning("HTTP %s for %s (attempt %d/%d)", e.response.status_code, url, attempt, retries)
            except requests.RequestException as e:
                log.warning("Request error for %s: %s (attempt %d/%d)", url, e, attempt, retries)
            if attempt < retries:
                time.sleep(self.delay * attempt)
        return None

    # ------------------------------------------------------------------
    # Course list discovery
    # ------------------------------------------------------------------

    def get_subject_links(self) -> list[dict]:
        """
        The UoA course outline lists subjects (COMPSCI, BIOSCI, etc.) on the
        main index page. Returns list of {subject, url} dicts.
        """
        log.info("Fetching subject index: %s", COURSE_LIST_URL)
        soup = self._get(COURSE_LIST_URL)
        if soup is None:
            return []

        subjects = []
        # The index page has links like /dco/course/COMPSCI or /dco/course/BIOSCI
        for a in soup.select("a[href]"):
            href = a["href"]
            m = re.match(r"^/dco/course/([A-Z]+)/?$", href)
            if m:
                subjects.append({
                    "subject": m.group(1),
                    "url": urljoin(BASE_URL, href),
                })

        log.info("Found %d subjects", len(subjects))
        return subjects

    def get_course_links_for_subject(self, subject_url: str, subject: str) -> list[dict]:
        """
        Within a subject page, find individual course links.
        Returns list of {code, year, url} dicts.
        """
        time.sleep(self.delay)
        soup = self._get(subject_url)
        if soup is None:
            return []

        links = []
        # Links look like /dco/course/COMPSCI/101/2024
        pattern = re.compile(r"^/dco/course/([A-Z]+)/(\d+)/(\d{4})$")
        seen = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            m = pattern.match(href)
            if m and href not in seen:
                seen.add(href)
                links.append({
                    "subject": m.group(1),
                    "catalog_number": m.group(2),
                    "year": m.group(3),
                    "url": urljoin(BASE_URL, href),
                })

        log.info("  %s: found %d course links", subject, len(links))
        return links

    # ------------------------------------------------------------------
    # Individual course parsing
    # ------------------------------------------------------------------

    def parse_course_page(self, url: str) -> Optional[Course]:
        """Scrape a single course detail page and return a Course object."""
        time.sleep(self.delay)
        soup = self._get(url)
        if soup is None:
            return None

        course = Course(url=url)

        # ---- course code + title ----
        # Typically in an <h1> like "COMPSCI 101 - Introduction to Programming"
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(" ", strip=True)
            m = re.match(r"([A-Z]+)\s+(\d+)\s*[-–]\s*(.+)", text)
            if m:
                course.subject = m.group(1)
                course.catalog_number = m.group(2)
                course.code = f"{m.group(1)} {m.group(2)}"
                course.title = m.group(3).strip()
                course.level = int(m.group(2)[0]) * 100

        # ---- parse the detail table / definition list ----
        # UoA course outlines use a <table> or <dl> with labelled rows
        self._parse_detail_block(soup, course)

        return course

    def _parse_detail_block(self, soup: BeautifulSoup, course: Course) -> None:
        """
        Extract structured fields from the course detail block.
        Handles both <table> and <dl> layouts.
        """
        # --- Try table layout ---
        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower().rstrip(":")
                value = cells[1].get_text(" ", strip=True)
                self._assign_field(course, label, value)

        # --- Try definition list layout ---
        for dt in soup.select("dl dt"):
            label = dt.get_text(strip=True).lower().rstrip(":")
            dd = dt.find_next_sibling("dd")
            value = dd.get_text(" ", strip=True) if dd else ""
            self._assign_field(course, label, value)

        # --- Try labelled div / span layout ---
        # e.g. <span class="label">Points:</span> <span class="value">15</span>
        for label_el in soup.select(".label, .field-label, .course-label"):
            label = label_el.get_text(strip=True).lower().rstrip(":")
            value_el = label_el.find_next_sibling()
            value = value_el.get_text(" ", strip=True) if value_el else ""
            self._assign_field(course, label, value)

        # --- Description block ---
        desc_el = soup.find(class_=re.compile(r"description|overview|summary", re.I))
        if desc_el and not course.description:
            course.description = desc_el.get_text(" ", strip=True)

    def _assign_field(self, course: Course, label: str, value: str) -> None:
        """Map a label/value pair onto the correct Course field."""
        label = label.strip()
        value = value.strip()
        if not value:
            return

        if re.search(r"point|credit", label):
            m = re.search(r"\d+", value)
            if m:
                course.points = int(m.group())
        elif re.search(r"prereq", label):
            course.prerequisites = value
        elif re.search(r"coreq", label):
            course.corequisites = value
        elif re.search(r"restrict", label):
            course.restriction = value
        elif re.search(r"semester|offered|availab", label):
            course.offered = value
        elif re.search(r"faculty", label):
            course.faculty = value
        elif re.search(r"department|school", label):
            course.department = value
        elif re.search(r"description|overview", label):
            course.description = value
        elif re.search(r"year", label) and not course.year:
            course.year = value

    # ------------------------------------------------------------------
    # Main scrape loop
    # ------------------------------------------------------------------

    def scrape(self, subjects_filter: list[str] | None = None) -> list[Course]:
        """
        Scrape all (or filtered) subjects.

        Args:
            subjects_filter: list of subject codes to limit scraping (e.g. ["COMPSCI", "STATS"])
                             Pass None to scrape everything.
        Returns:
            List of Course objects.
        """
        subjects = self.get_subject_links()
        if subjects_filter:
            subjects = [s for s in subjects if s["subject"] in subjects_filter]
            log.info("Filtered to %d subjects: %s", len(subjects), subjects_filter)

        all_courses: list[Course] = []

        for subj in subjects:
            course_links = self.get_course_links_for_subject(subj["url"], subj["subject"])

            for link in course_links:
                if self.max_courses and len(all_courses) >= self.max_courses:
                    log.info("Reached max_courses limit (%d)", self.max_courses)
                    return all_courses

                log.info("  Scraping %s %s (%s)…", link["subject"], link["catalog_number"], link["year"])
                course = self.parse_course_page(link["url"])
                if course:
                    # Fill in fields not on the detail page
                    if not course.subject:
                        course.subject = link["subject"]
                    if not course.catalog_number:
                        course.catalog_number = link["catalog_number"]
                    if not course.code:
                        course.code = f"{link['subject']} {link['catalog_number']}"
                    if not course.year:
                        course.year = link["year"]
                    all_courses.append(course)

        log.info("Total courses scraped: %d", len(all_courses))
        return all_courses


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

def save_json(courses: list[Course], path: str = "courses.json") -> None:
    data = [asdict(c) for c in courses]
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log.info("Saved %d courses to %s", len(courses), path)


def save_csv(courses: list[Course], path: str = "courses.csv") -> None:
    if not courses:
        log.warning("No courses to save.")
        return
    fieldnames = list(asdict(courses[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(c) for c in courses)
    log.info("Saved %d courses to %s", len(courses), path)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UoA Course Scraper")
    parser.add_argument(
        "--subjects", nargs="*", metavar="SUBJ",
        help="Limit to specific subject codes, e.g. COMPSCI STATS BIOSCI"
    )
    parser.add_argument(
        "--max", type=int, default=0,
        help="Max number of courses to scrape (0 = unlimited)"
    )
    parser.add_argument(
        "--delay", type=float, default=CRAWL_DELAY,
        help=f"Seconds between requests (default: {CRAWL_DELAY})"
    )
    parser.add_argument(
        "--output", default="courses",
        help="Output file base name (default: courses → courses.json + courses.csv)"
    )
    args = parser.parse_args()

    scraper = UoAScraper(delay=args.delay, max_courses=args.max)
    courses = scraper.scrape(subjects_filter=args.subjects)

    save_json(courses, f"{args.output}.json")
    save_csv(courses, f"{args.output}.csv")
