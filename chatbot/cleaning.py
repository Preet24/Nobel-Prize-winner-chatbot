"""
Cleaning and fact extraction for the scraped Wikipedia laureate files.

The raw JSON is messy (see CLAUDE.md), so before anything is embedded we:
  1. clean the text  -> remove footnote markers, fix odd spacing
  2. extract facts   -> Nobel prize category + years, birth/death years
These facts become metadata in the vector store (so searches can be filtered,
e.g. "only Physics laureates") and columns in laureates.csv.

Run this file directly to print extraction statistics without building
anything:   D:\\python_evenv\\Scripts\\python.exe -m chatbot.cleaning
"""

import csv
import json
import re
from pathlib import Path

from chatbot.config import DATA_DIR, PRIZE_OVERRIDES_CSV

# ===========================================================================
# 1. Text cleaning
# ===========================================================================

# Footnote markers left by the scraper: "[ 9 ]", "[ a ]", "[ fr ]",
# "[ note 1 ]", "[ citation needed ]". They carry no meaning for search.
FOOTNOTE_RE = re.compile(
    r"\[\s*(?:\d{1,3}|[a-z]{1,2}|note\s*\d+|nb\s*\d+|citation needed)\s*\]",
    re.IGNORECASE,
)

# The scraper puts a space before punctuation: "Copenhagen , Denmark".
SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?)\]])")
# ...and after opening brackets: "( 1922-06-19 )".
SPACE_AFTER_OPEN_RE = re.compile(r"([(\[])\s+")
# Runs of spaces/tabs (newlines are kept: they separate paragraphs).
MULTI_SPACE_RE = re.compile(r"[ \t]+")


def clean_text(text: str) -> str:
    """Return a tidied copy of a scraped string."""
    text = text.replace("\xa0", " ")           # non-breaking space -> space
    text = text.replace("\u200b", "")          # zero-width space -> nothing
    text = FOOTNOTE_RE.sub("", text)
    text = SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = SPACE_AFTER_OPEN_RE.sub(r"\1", text)
    text = MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def clean_key(key: str) -> str:
    """Infobox keys sometimes contain a non-breaking space ("Known\\xa0for")."""
    return key.replace("\xa0", " ").strip()


def load_record(path: Path) -> dict:
    """
    Load one laureate JSON file and return it with clean infobox keys/values
    and clean section text. The original structure is kept:
        {url, name, infobox: {...}, content_sections: [{type, heading, text}]}
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    infobox = {}
    for key, value in raw.get("infobox", {}).items():
        key = clean_key(key)
        # European_Union.json has an empty-string key; skip keys like that.
        if key:
            infobox[key] = clean_text(value)

    sections = [
        {
            "type": s.get("type", "h2"),
            "heading": clean_text(s.get("heading", "")),
            "text": clean_text(s.get("text", "")),
        }
        for s in raw.get("content_sections", [])
    ]

    return {
        "file_stem": path.stem,  # e.g. "Aage_Bohr"; used to build stable IDs
        "url": raw.get("url", ""),
        "name": clean_text(raw.get("name", path.stem.replace("_", " "))),
        "infobox": infobox,
        "content_sections": sections,
    }


# ===========================================================================
# 2. Nobel prize extraction
# ===========================================================================
# The data has no "prize" field, so we search the text for phrases such as
#   "Nobel Prize in Physics (1975)"             (infobox award lists)
#   "Nobel Prize for Chemistry (1958, 1980)"    (double winners)
#   "Nobel Peace Prize (1917, 1944, 1963)"      (organisations)
#   "the 1982 Nobel Prize for Physiology or Medicine"  (intro text)
#   "Nobel Laureate in Physiology or Medicine"

_CATEGORY = (
    r"(Physics|Chemistry|Literature|Physiology or Medicine|Medicine|"
    r"Physiology|Economic Sciences|Economics|Peace)"
)
PRIZE_MENTION_RE = re.compile(
    r"Nobel\s+(?:Memorial\s+)?"
    r"(?:(Peace)\s+Prize"                                    # Nobel Peace Prize
    r"|Prize\s+(?:in|for)\s+(?:the\s+)?" + _CATEGORY +       # Nobel Prize in X
    r"|[Ll]aureate\s+in\s+" + _CATEGORY + r")"               # Nobel Laureate in X
    r"|(Sveriges Riksbank Prize in Economic Sciences)",      # official econ name
    re.IGNORECASE,
)

# Map every spelling found in the text to one official category name.
CATEGORY_NAMES = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "literature": "Literature",
    "physiology or medicine": "Physiology or Medicine",
    "medicine": "Physiology or Medicine",
    "physiology": "Physiology or Medicine",
    "economic sciences": "Economic Sciences",
    "economics": "Economic Sciences",
    "peace": "Peace",
    "sveriges riksbank prize in economic sciences": "Economic Sciences",
}

YEAR_RE = re.compile(r"\b(19\d\d|20[0-2]\d)\b")
# A bracket right after the mention, e.g. "(1956, 1972)".
PAREN_RE = re.compile(r"^\s*\(([^)]*)\)")


def _years_near(text: str, start: int, end: int) -> list[int]:
    """
    Find the prize year(s) for a mention located at text[start:end].
    Order of preference:
      1. a bracket straight after it: "(1956, 1972)" -> [1956, 1972]
      2. the first year within 40 characters after: "... Physics in 1974"
      3. the last year within 25 characters before: "the 1982 Nobel Prize"
    Years before 1901 (the first Nobel Prizes) are ignored.
    """
    paren = PAREN_RE.match(text[end:])
    if paren and YEAR_RE.search(paren.group(1)):
        years = [int(y) for y in YEAR_RE.findall(paren.group(1))]
    else:
        after = YEAR_RE.findall(text[end:end + 40])
        before = YEAR_RE.findall(text[max(0, start - 25):start])
        years = [int(after[0])] if after else ([int(before[-1])] if before else [])
    return [y for y in years if y >= 1901]


def find_prizes(text: str, first_only: bool) -> dict[str, set[int]]:
    """
    Return {category: {years}} for every Nobel mention in `text`.

    first_only=True keeps only the first mention. We use it for article
    prose, because later mentions are often about *other* people
    (e.g. Alva Myrdal's intro also mentions her husband's Economics prize).
    """
    found: dict[str, set[int]] = {}
    for match in PRIZE_MENTION_RE.finditer(text):
        raw_category = next(g for g in match.groups() if g)
        category = CATEGORY_NAMES[raw_category.lower()]
        years = _years_near(text, match.start(), match.end())
        found.setdefault(category, set()).update(years)
        if first_only:
            break
    return found


def extract_prizes(record: dict) -> tuple[dict[str, list[int]], str]:
    """
    Work out which Nobel prize(s) a laureate won.

    Sources are tried from most to least reliable, and the first one that
    mentions a prize wins:
      "infobox" - award lists, cleanest data, may list several prizes
      "intro"   - first paragraph of the article (first mention only)
      "body"    - the next few sections (first mention only)
    Returns ({category: sorted years}, source_name) or ({}, "none").
    """
    sections = record["content_sections"]
    intro = sections[0]["text"] if sections else ""
    body = " ".join(s["text"] for s in sections[1:4])

    sources = [
        ("infobox", " ".join(record["infobox"].values()), False),
        ("intro", intro, True),
        ("body", body, True),
    ]

    prizes: dict[str, set[int]] = {}
    source = "none"
    for source_name, text, first_only in sources:
        prizes = find_prizes(text, first_only)
        if prizes:
            source = source_name
            break

    # Some infobox lists say "Nobel Peace Prize" without a year. For those,
    # look through *all* mentions in the article text for the same category.
    for category, years in prizes.items():
        if not years:
            for text in (intro, body):
                years.update(find_prizes(text, first_only=False).get(category, set()))
                if years:
                    break

    return {cat: sorted(years) for cat, years in prizes.items()}, source


def load_prize_overrides() -> dict[str, dict[str, list[int]]]:
    """
    Read prize_overrides.csv: hand-checked prizes (from nobelprize.org) for
    laureates whose file never mentions their prize. One row per prize:
        file_stem,prize_category,prize_years,source_url
        Al_Gore,Peace,2007,https://www.nobelprize.org/...
    prize_years may hold several years separated by ";".
    Returns {file_stem: {category: [years]}}.
    """
    overrides: dict[str, dict[str, list[int]]] = {}
    if not PRIZE_OVERRIDES_CSV.exists():
        return overrides
    with open(PRIZE_OVERRIDES_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            years = [int(y) for y in row["prize_years"].split(";") if y.strip()]
            overrides.setdefault(row["file_stem"], {})[row["prize_category"]] = years
    return overrides


# ===========================================================================
# 3. Life facts (birth/death years, birthplace)
# ===========================================================================
ISO_DATE_RE = re.compile(r"\b(\d{4})-\d{2}-\d{2}\b")
# Text to strip from "Born" when looking for the place: brackets, ages and
# written dates such as "19 June 1922" or "April 10, 1887".
_BRACKETS_RE = re.compile(r"\([^)]*\)")
_WRITTEN_DATE_RE = re.compile(
    r"\b(?:\d{1,2}\s+)?(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)(?:\s+\d{1,2},?)?\s+\d{4}\b"
)


def _year_from_field(value: str) -> int | None:
    """Prefer the ISO date "(1922-06-19)"; otherwise the first 4-digit year."""
    iso = ISO_DATE_RE.search(value)
    if iso:
        return int(iso.group(1))
    any_year = re.search(r"\b(1[6-9]\d\d|20[0-2]\d)\b", value)
    return int(any_year.group(1)) if any_year else None


def extract_life_facts(record: dict) -> dict:
    """
    Pull birth/death years and a best-effort birthplace from the infobox.
    Organisations and some people have no "Born" field: values are then None.
    Living laureates have no "Died" field, so death_year is None.
    """
    born = record["infobox"].get("Born", "")
    died = record["infobox"].get("Died", "")

    # Birthplace = whatever is left of "Born" after removing dates/brackets.
    # The last comma-separated part is usually the country ("..., Denmark"),
    # but this is best effort: historical places can be cities or regions.
    place = _WRITTEN_DATE_RE.sub("", _BRACKETS_RE.sub("", born))
    place = re.sub(r"\s+", " ", place).strip(" ,")
    country = place.split(",")[-1].strip() if "," in place else ""

    return {
        "birth_year": _year_from_field(born) if born else None,
        "death_year": _year_from_field(died) if died else None,
        "birth_place": place,
        "birth_country": country,
    }


# ===========================================================================
# Quick self-check: python -m chatbot.cleaning
# ===========================================================================
if __name__ == "__main__":
    from collections import Counter

    overrides = load_prize_overrides()
    sources, missing_year, multi, no_prize = Counter(), [], [], []

    for path in sorted(DATA_DIR.glob("*.json")):
        rec = load_record(path)
        prizes, source = extract_prizes(rec)
        if path.stem in overrides:
            prizes, source = overrides[path.stem], "override"
        sources[source] += 1
        if prizes and not all(prizes.values()):
            missing_year.append(rec["name"])
        if len(prizes) > 1 or any(len(y) > 1 for y in prizes.values()):
            multi.append(f"{rec['name']}: {prizes}")
        if not prizes:
            no_prize.append(path.stem)

    print("Prize found via:", dict(sources))
    print(f"\nMultiple prizes ({len(multi)}):", *multi, sep="\n  ")
    print(f"\nCategory but no year ({len(missing_year)}):", missing_year)
    print(f"\nNo prize found ({len(no_prize)}):", no_prize)
