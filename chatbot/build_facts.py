"""
Build laureate_prizes.csv: the table used for counting / ranking questions
("How many laureates were born in Spain?", "Who was the youngest?").

Vector search can't count, so these questions go to a table instead.

Sources:
  * The official Nobel Prize API (api.nobelprize.org): every laureate, exact
    birth date, birth country (today's name), gender, prize date, affiliation.
    Downloaded once into nobel_api_laureates.json, so later runs work offline.
  * Our Wikipedia files (laureates_data/): only the Education / Alma mater
    infobox fields, for questions about universities.

One row per PRIZE (Marie Curie has two rows), so ages at award are per
prize. Counting code counts unique laureates (nobel_id).

Run:
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.build_facts            # uses the cached download
    D:\\python_evenv\\Scripts\\python.exe -m chatbot.build_facts --refresh  # download again
"""

import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date

from chatbot.cleaning import extract_prizes, load_prize_overrides, load_record
from chatbot.config import DATA_DIR, NOBEL_API_CACHE, PRIZE_OVERRIDES_CSV, PRIZES_CSV

API_URL = "https://api.nobelprize.org/2.1/laureates?limit=100&offset={offset}"
# The API answers 403 Forbidden to requests without a User-Agent.
HEADERS = {"User-Agent": "nobel-laureate-rag-study/1.0"}


# ===========================================================================
# Step 1: download (or reuse) the official data
# ===========================================================================
def download_laureates() -> list[dict]:
    laureates, offset = [], 0
    while True:
        req = urllib.request.Request(API_URL.format(offset=offset), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = json.load(resp)
        laureates += page["laureates"]
        offset += 100
        if offset >= page["meta"]["count"]:
            return laureates


def load_api_laureates(refresh: bool) -> list[dict]:
    if refresh or not NOBEL_API_CACHE.exists():
        print("Downloading from api.nobelprize.org ...")
        data = download_laureates()
        NOBEL_API_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return json.loads(NOBEL_API_CACHE.read_text(encoding="utf-8"))


# ===========================================================================
# Step 2: education from the Wikipedia infobox
# ===========================================================================
# A school followed by its degrees in brackets: "Harvard University (AB, AM)".
_SCHOOL_WITH_DEGREES = re.compile(r"([A-Z][^()]*?)\s*\(([^)]*)\)")
# Bachelor's (first university) degrees: BA, BS, BSc, BEng, BSEE, AB, SB, LLB, MB...
_OXBRIDGE = re.compile(r"Oxford|University of Cambridge|College,\s*Cambridge|Cambridge University")
_UNDERGRAD = re.compile(r"^(?:B\.?\s?[A-Z]?[A-Za-z.]{0,5}|A\.?B\.?|S\.?B\.?|LL\.?B\.?|M\.?B\.?)$")


def education_fields(record: dict) -> tuple[str, str]:
    """
    Returns (education text, undergraduate schools joined with '; ').
    'Undergraduate' is only known when the infobox lists a bachelor's degree,
    e.g. "Harvard University (AB) Columbia University (PhD)" -> "Harvard University".
    """
    box = record["infobox"]
    text = " ".join(box.get(k, "") for k in ("Education", "Alma mater")).strip()
    undergrad = []
    for school, degrees in _SCHOOL_WITH_DEGREES.findall(text):
        tokens = [d.strip() for d in re.split(r"[,;/]| and ", degrees)]
        # Oxford and Cambridge give their BA graduates an "MA" a few years
        # later without further study, so an Oxbridge MA is an undergraduate degree.
        oxbridge_ma = bool(_OXBRIDGE.search(school)) and any(re.fullmatch(r"M\.?A\.?", t) for t in tokens)
        if oxbridge_ma or any(_UNDERGRAD.match(t) for t in tokens if t):
            name = school.strip(" ,;")
            if name not in undergrad:
                undergrad.append(name)
    return text, "; ".join(undergrad)


# ===========================================================================
# Step 3: match API laureates to our Wikipedia files
# ===========================================================================
def _norm_url(url: str) -> str:
    return urllib.parse.unquote(url).replace(" ", "_").lower().rstrip("/")


def match_records(api: list[dict]) -> dict[str, dict]:
    """
    api id -> our cleaned record. Matching order:
      1. the Wikipedia URL the API stores for the laureate
      2. the API id written in prize_overrides.csv (checked by hand)
      3. same family name + same prize year
    """
    records = [load_record(p) for p in sorted(DATA_DIR.glob("*.json"))]
    by_url = {_norm_url(r["url"]): r for r in records}
    by_stem = {r["file_stem"]: r for r in records}

    matched: dict[str, dict] = {}
    for lau in api:
        url = lau.get("wikipedia", {}).get("english")
        if url and _norm_url(url) in by_url:
            matched[lau["id"]] = by_url[_norm_url(url)]

    with open(PRIZE_OVERRIDES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            api_id = row["source_url"].rstrip("/").split("/")[-1]
            if api_id not in matched and row["file_stem"] in by_stem:
                matched[api_id] = by_stem[row["file_stem"]]

    used = {id(r) for r in matched.values()}
    overrides = load_prize_overrides()
    for lau in api:
        if lau["id"] in matched:
            continue
        family = (lau.get("familyName") or lau.get("orgName") or {}).get("en", "").lower()
        years = {p["awardYear"] for p in lau.get("nobelPrizes", [])}
        for r in records:
            if id(r) in used or not family or family not in r["name"].lower():
                continue
            prizes = overrides.get(r["file_stem"]) or extract_prizes(r)[0]
            if years & {str(y) for ys in prizes.values() for y in ys}:
                matched[lau["id"]] = r
                used.add(id(r))
                break
    return matched


# ===========================================================================
# Step 4: one row per prize
# ===========================================================================
def _parse_date(text: str | None) -> date | None:
    """'1943-11-07' -> date; '1900-00-00' (day/month unknown) -> None."""
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def build_rows(api: list[dict], matched: dict[str, dict]) -> list[dict]:
    rows = []
    for lau in api:
        is_person = "knownName" in lau
        name = (lau.get("knownName") or lau.get("orgName"))["en"]
        birth = lau.get("birth") or lau.get("founded") or {}
        place = birth.get("place", {})
        birth_date = _parse_date(birth.get("date"))
        record = matched.get(lau["id"])
        education, undergrad = education_fields(record) if record else ("", "")

        for prize in lau.get("nobelPrizes", []):
            awarded = _parse_date(prize.get("dateAwarded")) or _parse_date(f"{prize['awardYear']}-12-10")
            age, age_days = None, None
            if is_person and birth_date and awarded:
                # Exact age in whole years on the day the prize was awarded, plus
                # the age in days, which ranks people of the same age correctly.
                age = awarded.year - birth_date.year - ((awarded.month, awarded.day) < (birth_date.month, birth_date.day))
                age_days = (awarded - birth_date).days
            elif is_person and birth.get("year"):
                age = int(prize["awardYear"]) - int(birth["year"])  # birth day unknown: approximate
            affiliations = prize.get("affiliations", [])
            rows.append({
                "nobel_id": lau["id"],
                "name": name,
                "kind": "person" if is_person else "organization",
                "gender": lau.get("gender", ""),
                "birth_date": birth.get("date", ""),
                "birth_year": birth.get("year", ""),
                "birth_city": place.get("cityNow", place.get("city", {})).get("en", ""),
                "birth_country": place.get("countryNow", {}).get("en", ""),      # today's name
                "birth_country_then": place.get("country", {}).get("en", ""),    # e.g. "Prussia"
                # Full text, old and new names: "Breslau, Germany (now Wrocław, Poland)"
                "birth_location": place.get("locationString", {}).get("en", ""),
                "death_date": (lau.get("death") or {}).get("date", ""),
                "category": prize["category"]["en"],
                "year": int(prize["awardYear"]),
                "date_awarded": prize.get("dateAwarded", ""),
                "age_at_award": "" if age is None else age,
                "age_days_at_award": "" if age_days is None else age_days,
                "portion": prize.get("portion", ""),       # "1" = not shared, "1/2", "1/3", "1/4"
                "motivation": prize.get("motivation", {}).get("en", ""),
                "affiliation": "; ".join(a.get("name", {}).get("en", "") for a in affiliations),
                "affiliation_country": "; ".join(a.get("countryNow", a.get("country", {})).get("en", "") for a in affiliations),
                "education": education,
                "undergrad_schools": undergrad,
                "wikipedia_file": record["file_stem"] if record else "",
                "nobel_url": f"https://www.nobelprize.org/laureate/{lau['id']}",
            })
    return rows


def main():
    api = load_api_laureates(refresh="--refresh" in sys.argv)
    matched = match_records(api)
    rows = build_rows(api, matched)
    with open(PRIZES_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(api)} laureates from the Nobel API, {len(rows)} prize rows -> {PRIZES_CSV.name}")
    print(f"Matched to our Wikipedia files: {len(matched)} of {len(api)}")
    print(f"With education text: {sum(1 for r in rows if r['education'])} rows, "
          f"with a known undergraduate school: {sum(1 for r in rows if r['undergrad_schools'])}")
    missing = [r["name"] for r in rows if r["kind"] == "person" and r["age_at_award"] == ""]
    print(f"People without an age at award: {missing or 'none'}")


if __name__ == "__main__":
    main()
