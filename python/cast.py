#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""Build a character list for a video from its cast, via Brave Search.

Brave finds the show's cast page; the cast itself is read from that page's
table, because search snippets alone are too noisy to list a cast from (an
"<actor> as <character>" sweep over ten results returned four hits, one of
them "Ro Laren. If"). The table also says which seasons each character is in,
so a file named S02 does not get characters who joined in season 3.

Each character then gets one more Brave search for a line describing how they
look, since names alone are weak in a Box question: "the person who is
Lieutenant Worf" scores 0.06-0.15 against 0.42 with a description attached.
Snippets vary, so anything in KNOWN_LOOKS wins over the searched text.

    ./cast.py "Star Trek - TNG - S02E04 - The Outrageous Okona.mp4"

Writes cast.json beside this script, which app.py picks up automatically.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
WIKI_URL = "https://en.wikipedia.org/w/api.php"

# Wikipedia's User-Agent policy wants a way to contact whoever is calling, and
# rejects anything without a URL-shaped one: "jev-video-play/0.1" and
# "(contact: unset)" both return 403, "(+http://localhost)" does not. Set
# WIKI_CONTACT to your own URL if you run this anywhere shared.
def user_agent() -> str:
    contact = os.environ.get("WIKI_CONTACT", "+http://localhost")
    return f"jev-video-play/0.1 ({contact})"

# Brave's free tier allows about one request a second.
RATE_LIMIT_SECONDS = 1.1


# Words that make a snippet worth keeping as a description of how someone looks.
# Sentences about the role rather than the person. A Doctor Who search turned
# up "gone from a young man to a much older one despite being renewed", which
# describes a previous incarnation and made the presence gate answer 0.20 on a
# frame the Box had already found with 0.76 confidence.
META_WORDS = (
    "incarnation", "regenerat", "viewers", "episode", "series", "season",
    "actor", "actress", "cast ", "casting", "showrunner", "fans", "this version",
    "spin-off", "reprise", "announced", "interview", "review",
)

LOOK_WORDS = (
    "hair", "skin", "eyes", "beard", "bald", "uniform", "wears", "wearing",
    "forehead", "visor", "android", "tall", "complexion", "face", "ridged",
    "blonde", "dark", "young", "older", "woman", "man", "alien",
)

# Searched snippets are biography more often than appearance. These win when
# the character matches, so a known cast stays as sharp as a hand-written list.
KNOWN_LOOKS = {
    "jean-luc picard": "older, bald, white, red-shouldered command uniform",
    "william riker": "tall white man with dark hair, often bearded, red-shouldered command uniform",
    "data": "an android with pale golden-white skin and yellow eyes, gold-shouldered uniform",
    "worf": "a Klingon with a heavily ridged forehead and long dark hair, gold-shouldered uniform with a metallic sash across the chest",
    "deanna troi": "woman with long dark curly hair, usually in a coloured jumpsuit rather than a uniform",
    "geordi la forge": "Black man wearing a metallic VISOR across his eyes",
    "beverly crusher": "white woman with red hair, blue-shouldered medical uniform",
    "katherine pulaski": "older white woman with short fair hair, blue-shouldered medical uniform",
    "wesley crusher": "a teenage boy, usually in a grey-and-black cadet jumpsuit",
    "guinan": "Black woman wearing a very large wide-brimmed hat",
    "miles o'brien": "white man with short brown hair, gold-shouldered uniform",
    "tasha yar": "white woman with very short blonde hair, gold-shouldered uniform",
}


def slugify(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_")


HONORIFICS = {
    "captain", "commander", "lieutenant", "lt", "doctor", "dr", "mr", "mrs",
    "ms", "chief", "ensign", "counselor", "counsellor", "admiral", "professor",
    "cadet", "sub-commander", "general", "colonel",
}

# Surname particles that belong with the name after them.
PARTICLES = {"la", "le", "van", "von", "de", "del", "der", "da", "di"}


ARTICLES = {"the", "a", "an"}


def base_name(formal: str) -> str:
    """Drop ranks and titles, unless they are the name.

    "The Doctor" is a name, not a rank: stripping the honorific left "The",
    which is what the overlay then called him.
    """
    words = [w for w in formal.split() if w.strip(".").lower() not in HONORIFICS]
    if not words or all(w.lower() in ARTICLES for w in words):
        return formal
    return " ".join(words)


def surname(formal: str) -> str:
    """'Geordi La Forge' -> 'La Forge'; 'Lieutenant Worf' -> 'Worf'."""
    words = base_name(formal).split()
    if len(words) < 2:
        return words[0] if words else formal
    # "The Doctor" and "The Master" are whole names, not title plus surname.
    if words[0].lower() in ARTICLES:
        return " ".join(words)
    if words[-2].lower().rstrip(".") in PARTICLES:
        return " ".join(words[-2:])
    return words[-1]


def display_names(formals: list[str]) -> dict[str, str]:
    """Surnames where they are unique, full names where they would collide.

    A cast can hold two Trois, and labelling both boxes "Troi" would make the
    overlay lie about which one was found.
    """
    counts: dict[str, int] = {}
    for formal in formals:
        counts[surname(formal)] = counts.get(surname(formal), 0) + 1
    return {
        formal: (base_name(formal) if counts[surname(formal)] > 1 else surname(formal))
        for formal in formals
    }


# Scene-release noise. Everything from the first of these onwards is dropped.
RELEASE_TAGS = {
    "480p", "576p", "720p", "1080p", "1440p", "2160p", "4k", "uhd", "hd",
    "bluray", "blu", "ray", "brrip", "bdrip", "bdremux", "remux", "webrip",
    "web", "webdl", "hdtv", "dvdrip", "dvd", "hdrip", "cam", "ts",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx",
    "aac", "ac3", "eac3", "dts", "ddp", "truehd", "atmos", "flac", "mp3",
    "hdr", "hdr10", "dv", "sdr", "10bit", "8bit",
    "proper", "repack", "internal", "limited", "extended", "unrated",
    "remastered", "imax", "multi", "dual", "subbed", "dubbed",
}


def is_tag(token: str) -> bool:
    cleaned = token.strip("()[]{}").lower()
    if not cleaned:
        return False
    return any(part in RELEASE_TAGS for part in re.split(r"[-+]", cleaned) if part)


def is_year(token: str) -> bool:
    return bool(re.fullmatch(r"\(?(19|20)\d{2}\)?", token.strip("[]{}")))


def year_of(token: str) -> int:
    return int(re.search(r"(19|20)\d{2}", token).group(0))


def clean_title(stem: str) -> tuple[str, int | None]:
    """Turn a release name into a searchable title and its year.

    The release year is the last year before the first quality tag, which is
    what keeps `Blade.Runner.2049.2017.2160p` as "Blade Runner 2049" from 2017
    rather than a film called "Blade Runner" made in 2049.
    """
    tokens = [t for t in re.split(r"[.\s_]+", stem) if t]

    cut = next((i for i, t in enumerate(tokens) if is_tag(t)), len(tokens))
    head = tokens[:cut]

    years = [i for i, t in enumerate(head) if is_year(t)]
    year = None
    if years:
        last = years[-1]
        year = year_of(head[last])
        head = head[:last]

    head = [t for t in head if t not in {"-", "--"}]
    title = " ".join(head).strip(" -_")
    title = re.sub(r"\s*-\s*$", "", title)
    return re.sub(r"\s+", " ", title), year


def parse_filename(name: str) -> tuple[str, int | None, int | None]:
    """Pull a show or film title, a season number and a year out of a filename."""
    stem = Path(name).stem

    season = None
    match = re.search(r"[Ss](\d{1,2})[\s._-]?[Ee]\d{1,3}", stem)
    if match:
        season = int(match.group(1))
        stem = stem[: match.start()]
    else:
        match = re.search(r"\bseason[\s._-]*(\d{1,2})\b", stem, re.I)
        if match:
            season = int(match.group(1))
            stem = stem[: match.start()]

    title, year = clean_title(stem)
    return title, season, year


class Brave:
    def __init__(self, key: str, client: httpx.Client) -> None:
        self.key = key
        self.client = client
        self.last_call = 0.0

    def search(self, query: str, count: int = 8) -> list[dict]:
        wait = RATE_LIMIT_SECONDS - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.monotonic()

        response = self.client.get(
            BRAVE_URL,
            params={"q": query, "count": count},
            headers={"Accept": "application/json", "X-Subscription-Token": self.key},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Brave search failed ({response.status_code}): {response.text[:200]}")
        return ((response.json().get("web") or {}).get("results")) or []


def strip_html(text: str) -> str:
    import html as html_module

    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def find_cast_page(brave: Brave, title: str) -> str | None:
    """Ask Brave for the show's cast page and take the Wikipedia one."""
    results = brave.search(f"{title} list of cast members wikipedia")
    for result in results:
        url = result.get("url") or ""
        if "en.wikipedia.org/wiki/" in url:
            page = url.rsplit("/wiki/", 1)[-1]
            if "cast" in page.lower():
                return page
    for result in results:
        url = result.get("url") or ""
        if "en.wikipedia.org/wiki/" in url:
            return url.rsplit("/wiki/", 1)[-1]
    return None


def fetch_cast_table(client: httpx.Client, page: str) -> str:
    response = client.get(WIKI_URL, params={
        "action": "parse", "page": page.replace("_", " "),
        "prop": "wikitext", "format": "json", "redirects": "1",
    })
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"Wikipedia: {data['error'].get('info')}")
    return data["parse"]["wikitext"]["*"]


def split_top_level(text: str, separator: str) -> list[str]:
    """Split on a separator that is not inside [[links]] or {{templates}}."""
    parts, depth, start = [], 0, 0
    index = 0
    while index < len(text):
        pair = text[index:index + 2]
        if pair in ("[[", "{{"):
            depth += 1
            index += 2
            continue
        if pair in ("]]", "}}"):
            depth = max(0, depth - 1)
            index += 2
            continue
        if depth == 0 and text.startswith(separator, index):
            parts.append(text[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    parts.append(text[start:])
    return parts


# A run of html-ish attributes at the start of a cell.
ATTRIBUTES = re.compile(r'^\s*((?:[a-zA-Z-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)\s*)+)')


def cell_body(raw: str) -> tuple[str, str]:
    """Separate a cell's attributes from its content.

    `| style="width:9%" | [[Jon Pertwee]]` separates them with a pipe, but
    `| colspan="11" {{CMain}}` does not, and missing the span there collapses
    the row so every later column reads from the wrong place.
    """
    pieces = split_top_level(raw, "|")
    if len(pieces) >= 2 and re.search(r"=", pieces[0]):
        return pieces[0], "|".join(pieces[1:]).strip()

    match = ATTRIBUTES.match(raw)
    if match:
        return match.group(1), raw[match.end():].strip()
    return "", raw.strip()


def row_cells(block: str, marker: str) -> list[str]:
    """Raw cells of one row. Wikitables allow `| a || b` as well as one per line."""
    cells: list[str] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith(marker) or line.startswith(marker + "-"):
            continue
        line = line[len(marker):]
        cells.extend(split_top_level(line, marker * 2))
    return cells


def span(attrs: str, name: str) -> int:
    match = re.search(name + r'\s*=\s*"?(\d+)"?', attrs)
    return max(1, int(match.group(1))) if match else 1


def grid(blocks: list[str], marker: str) -> list[list[str]]:
    """Lay rows out as a column grid, honouring colspan and rowspan.

    Without this the columns slide: `Character` carries colspan=2 and a long
    running character carries rowspan=3, so later rows have fewer cells than
    columns and everything after them lands in the wrong place.
    """
    rows: list[list[str]] = []
    carry: dict[int, list] = {}

    for block in blocks:
        raw = row_cells(block, marker)
        if not raw:
            continue

        row: dict[int, str] = {}
        for column, held in list(carry.items()):
            held[0] -= 1
            row[column] = held[1]
            if held[0] <= 0:
                del carry[column]

        column = 0
        for item in raw:
            attrs, content = cell_body(item)
            while column in row:
                column += 1
            width = span(attrs, "colspan")
            height = span(attrs, "rowspan")
            for offset in range(width):
                row[column + offset] = content
                if height > 1:
                    carry[column + offset] = [height - 1, content]
            column += width

        rows.append([row.get(i, "") for i in range(max(row) + 1)] if row else [])
    return rows


def link_text(cell: str) -> str | None:
    """The cell's own name: the first [[link]], or its plain text."""
    links = re.findall(r"\[\[([^\]]+)\]\]", cell)
    if links:
        target = links[0]
        return (target.split("|")[-1] if "|" in target else target).strip()

    plain = re.sub(r"\{\{[^}]*\}\}", " ", cell)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"[']{2,}", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain or None


# Things that turn up in a character column but are not characters.
NOT_A_NAME = re.compile(
    r"^\d{4}\s*[\u2013-]\s*\d{2,4}$"   # 2018-2025
    r"|^\d+$"
    r"|^(season|series|episode|special|appearances?|actor|character|notes?)\b"
    r"|^(the )?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth)$",
    re.I,
)


def looks_like_name(value: str | None) -> bool:
    if not value or len(value) > 60 or len(value) < 2:
        return False
    return not NOT_A_NAME.match(value.strip())


def season_number(header: str) -> int | None:
    """Season this header column stands for, from its link target or its text."""
    for target in re.findall(r"\[\[([^\]|]+)", header):
        match = re.search(r"(?:season|series)[ _](\d{1,2})\b", target, re.I)
        if match:
            return int(match.group(1))
    match = re.search(r"(?:season|series)\D{0,8}(\d{1,2})\b", header, re.I)
    return int(match.group(1)) if match else None


def header_year(header: str) -> int | None:
    """The year a season column is labelled with, e.g. (1973-74) or (2020)."""
    found = re.search(r"\((19|20)(\d{2})", header)
    return int(found.group(1) + found.group(2)) if found else None


def find_column(headers: list[list[str]], pattern: str) -> int | None:
    for row in headers:
        for index, cell in enumerate(row):
            if re.search(pattern, cell, re.I):
                return index
    return None


def parse_cast(
    wikitext: str, season: int | None, year: int | None = None,
) -> list[dict]:
    """Read every Actor/Character table on the page.

    Pages differ: one table with the seasons as numbered columns, or a dozen
    tables each covering a span of seasons with an extra `Appearances` column
    in between. Columns are located by their headers rather than by position.
    """
    cast: list[dict] = []
    seen: set[str] = set()

    tables = re.findall(r"\{\|.*?\n\|\}", wikitext, re.S)

    # A show that numbers both seasons and series from 1 offers two answers for
    # "12": Doctor Who's 1974 season and its 2020 series. The year in the
    # filename decides, and without one every match is kept.
    if season and year:
        dated = []
        for table in tables:
            for row in grid(table.split("|-"), "!"):
                for cell in row:
                    if season_number(cell) == season:
                        stamp = header_year(cell)
                        if stamp:
                            dated.append((abs(stamp - year), table))
                        break
        if dated:
            best = min(dated)[0]
            tables = [t for gap, t in dated if gap == best]

    for table in tables:
        blocks = table.split("|-")
        headers = grid(blocks, "!")
        if not headers:
            continue

        actor_column = find_column(headers, r"\b(actor|performer|cast member|played by)\b")
        character_column = find_column(headers, r"\bcharacter\b")
        if actor_column is None or character_column is None:
            continue

        season_columns: dict[int, int] = {}
        for row in headers:
            for index, cell in enumerate(row):
                number = season_number(cell)
                if number is not None:
                    season_columns.setdefault(number, index)

        # This table does not cover the season we want.
        if season and season_columns and season not in season_columns:
            continue

        for row in grid(blocks, "|"):
            if len(row) <= max(actor_column, character_column):
                continue
            actor = link_text(row[actor_column])
            character = link_text(row[character_column])
            if not looks_like_name(actor) or not looks_like_name(character):
                continue
            if character.lower() in seen or character.lower() == actor.lower():
                continue

            if season and season in season_columns:
                cell = row[season_columns[season]] if season_columns[season] < len(row) else ""
                if not re.search(r"\{\{\s*c?(main|recurring|guest)", cell, re.I):
                    continue

            # A Character header spanning two columns puts a qualifier beside
            # the name: "The Doctor" | "Thirteenth". It names the incarnation,
            # and so the page that describes who is actually on screen.
            qualifier = None
            if character_column + 1 < len(row):
                extra = link_text(row[character_column + 1])
                if extra and 2 <= len(extra) <= 24 and extra.lower() != character.lower():
                    qualifier = extra

            seen.add(character.lower())
            cast.append({"actor": actor, "character": character, "qualifier": qualifier})

    return cast


INFOBOX_FIELDS = {
    "species": "species", "race": "species",
    "gender": "gender", "sex": "gender",
    "hair": "hair", "hair_color": "hair", "haircolor": "hair", "hair_colour": "hair",
    "eye_color": "eyes", "eyecolor": "eyes", "eyes": "eyes", "eye_colour": "eyes",
}


def infobox_value(wikitext: str, key: str) -> str | None:
    match = re.search(r"^\s*\|\s*" + key + r"\s*=\s*(.+)$", wikitext, re.M | re.I)
    if not match:
        return None
    raw = match.group(1)

    # A wiki that fills a field with a reference to itself — "The Doctor's
    # species" — must be rejected here, while the apostrophe still exists.
    # Stripping it first turned that into "the doctor s species", which the
    # later guard could never match and which reached a Box question verbatim.
    if re.search(r"[\u2019']s\b", raw):
        return None

    value = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", raw)
    value = re.sub(r"\{\{[^}]*\}\}", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[*']", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ,;|")

    # Adaptations annotate their fields: "brown (novels) blue (film)". We are
    # looking at a screen, so the film or TV value wins.
    screen = re.search(r"([\w\s-]+?)\s*\((?:film|tv|series|show)[^)]*\)", value, re.I)
    if screen:
        value = screen.group(1).strip()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ,;|")

    # A field whose value repeats its own name is a template placeholder.
    if not value or len(value) > 40 or "=" in value:
        return None
    if re.search(r"\b(species|gender|hair|eye|colou?r|unknown|n/?a|none)\b$", value, re.I):
        return None
    return value


def pronoun_gender(wikitext: str) -> str | None:
    """Gender from the prose, which every character page has even when its
    infobox does not. The Doctor's actor changes between seasons, so this has
    to come from the page for that incarnation, not the character in general."""
    body = wikitext[:12000].lower()
    feminine = len(re.findall(r"\b(she|her|hers|herself)\b", body))
    masculine = len(re.findall(r"\b(he|him|his|himself)\b", body))
    if feminine >= 5 and feminine > masculine * 1.5:
        return "woman"
    if masculine >= 5 and masculine > feminine * 1.5:
        return "man"
    return None


def page_subject(character: str, qualifier: str | None) -> str:
    """'The Doctor' + 'Thirteenth' -> 'Thirteenth Doctor'."""
    if not qualifier or qualifier.lower() == character.lower():
        return character
    if character.lower().startswith("the "):
        return f"{qualifier} {character[4:]}"
    return f"{qualifier} {character}"


def wiki_profile(
    brave: Brave, client: httpx.Client, character: str, qualifier: str | None, title: str,
) -> str:
    """A visual line built from the character's own wiki page.

    Infoboxes carry hair, eyes and species on some wikis and nothing on others,
    so gender is read from the prose as a floor: on a frame of the Thirteenth
    Doctor, "Look for a woman" lifts the presence gate from 0.57 to 0.83, and
    the bare name alone never clears the 0.60 default.
    """
    subject = page_subject(character, qualifier)
    try:
        results = brave.search(f"{subject} {title} wiki character", count=8)
    except RuntimeError:
        return ""

    for result in results:
        url = result.get("url") or ""
        match = re.match(r"https?://([a-z0-9-]+\.fandom\.com)/(?:[a-z-]+/)?wiki/(.+)", url)
        if not match:
            continue
        host, page = match.group(1), match.group(2).split("?")[0]
        try:
            response = client.get(
                f"https://{host}/api.php",
                params={"action": "parse", "page": page, "prop": "wikitext",
                        "format": "json", "redirects": "1"},
            )
            data = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            continue
        if "parse" not in data:
            continue

        wikitext = data["parse"]["wikitext"]["*"]
        fields: dict[str, str] = {}
        for key, slot in INFOBOX_FIELDS.items():
            if slot in fields:
                continue
            value = infobox_value(wikitext, key)
            if value:
                fields[slot] = value

        gender = fields.get("gender", "").lower()
        if gender.startswith("f"):
            gender = "woman"
        elif gender.startswith("m"):
            gender = "man"
        else:
            gender = pronoun_gender(wikitext) or ""

        species = fields.get("species", "")
        if species.lower() in ("human", "humans", ""):
            species = ""

        head = " ".join(x for x in (species.lower(), gender) if x) or None
        if not head:
            continue

        parts = [f"a {head}"]
        hair = fields.get("hair", "").lower()
        if hair in ("bald", "none", "no hair"):
            parts.append("who is bald")
        elif hair:
            parts.append(f"with {hair} hair")
        if fields.get("eyes"):
            joiner = "and " if len(parts) > 1 and not parts[-1].startswith("who") else "with "
            parts.append(joiner + f"{fields['eyes'].lower()} eyes")
        return " ".join(parts)

    return ""


FILM_CAST_LINE = re.compile(
    r"^\*+\s*\[\[([^\]|]+)(?:\|[^\]]*)?\]\]\s+as\s+(.+)$", re.M,
)


def parse_film_cast(wikitext: str) -> list[dict]:
    """Read a film article's Cast section.

    Films list `* [[Actor]] as [[Character]]`, sometimes with a description
    after a colon or comma, and have no season columns to filter on.
    """
    match = re.search(r"^==+\s*Cast[^=]*==+\s*$", wikitext, re.M | re.I)
    if not match:
        return []
    rest = wikitext[match.end():]
    stop = re.search(r"^==[^=]", rest, re.M)
    section = rest[: stop.start()] if stop else rest

    cast: list[dict] = []
    seen: set[str] = set()
    for actor, tail in FILM_CAST_LINE.findall(section):
        # Only a link that opens the tail is the character. "as Lambert, the
        # Nostromo's [[navigator]]" links a job, not a name.
        stripped = tail.lstrip()
        links = re.findall(r"\[\[([^\]]+)\]\]", tail) if stripped.startswith("[[") else []
        if links:
            target = links[0]
            character = (target.split("|")[-1] if "|" in target else target).strip()
        else:
            # Not on a bare period: "Lt. Joshi" and "Dr. Ana Stelline" are names.
            character = re.split(r"[:;(]|,\s|\s[\u2013-]\s", tail)[0].rstrip(".")
            character = re.sub(r"\{\{[^}]*\}\}|<[^>]+>|''+", " ", character)
            character = re.sub(r"\s+", " ", character).strip()

        if not looks_like_name(character) or not looks_like_name(actor):
            continue
        if character.lower() in seen:
            continue
        seen.add(character.lower())
        cast.append({"actor": actor.strip(), "character": character, "qualifier": None})

    return cast


WHOSTHATCAST = "whosthatcast.com"

# Their pages ship a plain list behind the app: "<li>Character — played by Actor</li>".
WHOSTHATCAST_LINE = re.compile(
    r"<li>\s*([^<]+?)\s*[\u2014\u2013-]\s*played by\s*([^<]+?)\s*</li>", re.I,
)


def find_whosthatcast_page(
    brave: Brave, title: str, year: int | None, is_film: bool,
) -> str | None:
    """Build the Who's That? URL for a title.

    Their pages are keyed by TMDB id and ignore the slug, so
    /tv/287620 is enough. Searching the site directly does not find them —
    Brave returns the home page — but it does return TMDB, whose URLs carry
    the id we need.
    """
    kind = "movie" if is_film else "tv"
    bits = [title, str(year) if year else "", "movie" if is_film else "tv series", "themoviedb"]
    try:
        results = brave.search(" ".join(b for b in bits if b), count=8)
    except RuntimeError:
        return None

    for result in results:
        url = result.get("url") or ""
        # Their own page, if it happens to be indexed.
        direct = re.match(rf"https?://(?:www\.)?{re.escape(WHOSTHATCAST)}/(tv|movie)/(\d+)", url)
        if direct:
            return f"https://{WHOSTHATCAST}/{direct.group(1)}/{direct.group(2)}"
        found = re.search(r"themoviedb\.org/(tv|movie)/(\d+)", url)
        if found and found.group(1) == kind:
            return f"https://{WHOSTHATCAST}/{kind}/{found.group(2)}"

    return None


def parse_whosthatcast(page_html: str) -> list[dict]:
    """Read the character/actor pairs off a Who's That? page.

    Covers what Wikipedia does not: a show too new or too small for an article
    with a cast table still has a page here.
    """
    cast: list[dict] = []
    seen: set[str] = set()

    for character, actor in WHOSTHATCAST_LINE.findall(page_html):
        character = html_lib.unescape(character).strip()
        actor = html_lib.unescape(actor).strip()
        # "Stuart Bloom / Alternate Stuart Bloom" is one part the actor plays.
        character = re.split(r"\s+/\s+", character)[0].strip()
        if not character or not actor or len(character) > 60:
            continue
        if character.lower() in seen:
            continue
        seen.add(character.lower())
        cast.append({"actor": actor, "character": character, "qualifier": None})

    return cast


def find_film_page(brave: Brave, title: str, year: int | None) -> str | None:
    query = f"{title} {year} film wikipedia" if year else f"{title} film wikipedia"
    for result in brave.search(query):
        url = result.get("url") or ""
        if "en.wikipedia.org/wiki/" in url:
            page = url.rsplit("/wiki/", 1)[-1]
            if not page.lower().startswith("list_of"):
                return page
    return None


def describe(brave: Brave, character: str, title: str) -> str:
    """A line about how this character looks, best-effort."""
    known = KNOWN_LOOKS.get(character.lower())
    if known:
        return known

    try:
        results = brave.search(f"{character} {title} character appearance description", count=5)
    except RuntimeError as error:
        print(f"    ! {error}", file=sys.stderr)
        return ""

    best, best_score = "", 0
    for result in results:
        text = strip_html(result.get("description") or "")
        # Casting and production talk describes the actor, not the character.
        text = re.sub(r",?\s*(played|portrayed) by [^,.]+", "", text, flags=re.I)
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if len(sentence) < 40 or len(sentence) > 300:
                continue
            lowered = sentence.lower()
            if any(word in lowered for word in META_WORDS):
                continue
            score = sum(word in lowered for word in LOOK_WORDS)
            if score > best_score:
                best, best_score = sentence.strip(), score

    # One stray match is usually a coincidence ("dark" in "dark matter"), and a
    # wrong description is worse in a Box question than none at all.
    if best_score < 2:
        return ""
    return best


def build_cast(
    video: str,
    *,
    key: str,
    season: int | None = None,
    limit: int = 0,
    descriptions: bool = True,
    log=print,
) -> dict:
    """Find the video's cast and describe each character. Used by app.py too."""
    title, parsed_season, year = parse_filename(video)
    season = season or parsed_season
    log(f"Title:  {title}")
    log(f"Season: {season if season else 'all'}")

    with httpx.Client(
        timeout=45.0,
        follow_redirects=True,
        headers={"User-Agent": user_agent()},
    ) as client:
        brave = Brave(key, client)

        # No season in the name means a film, and films list their cast as
        # prose bullets with no season columns to filter on.
        is_film = season is None

        log("==> Finding the cast page")
        page = find_film_page(brave, title, year) if is_film else find_cast_page(brave, title)
        cast: list[dict] = []

        if page:
            log(f"    {page.replace('_', ' ')}")
            log("==> Reading the cast")
            wikitext = fetch_cast_table(client, page)
            cast = parse_film_cast(wikitext) if is_film else parse_cast(wikitext, season, year)
            if not cast and is_film:
                cast = parse_cast(wikitext, None, None)   # some films use a table
        else:
            log("    no Wikipedia page")

        if not cast:
            # Wikipedia has no cast for this one. Who's That? covers shows and
            # films too new or too small to have an article with a cast table.
            log(f"==> Trying {WHOSTHATCAST}")
            url = find_whosthatcast_page(brave, title, year, is_film)
            if url:
                log(f"    {url}")
                try:
                    response = client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/120.0 Safari/537.36",
                    })
                    if response.status_code == 200:
                        cast = parse_whosthatcast(response.text)
                        page = url
                except httpx.HTTPError as error:
                    log(f"    ! {error}")

        if not cast:
            where = page or title
            raise LookupError(f"No cast found for {title!r} (tried {where}).")
        if limit:
            cast = cast[:limit]
        log(f"    {len(cast)} characters")

        characters: dict[str, dict] = {}
        names = display_names([entry["character"] for entry in cast])
        log("==> Describing each character")
        for entry in cast:
            formal = entry["character"]
            look, source = "", "none"
            if descriptions:
                known = KNOWN_LOOKS.get(formal.lower())
                if known:
                    look, source = known, "known"
                else:
                    look = wiki_profile(brave, client, formal, entry.get("qualifier"), title)
                    if look:
                        source = "wiki"
                    else:
                        look = describe(brave, formal, title)
                        source = "search" if look else "none"
            slug = slugify(formal) or f"character_{len(characters) + 1}"
            while slug in characters:
                slug = f"{slug}_{len(characters) + 1}"
            characters[slug] = {
                "name": names[formal],
                "formal": formal,
                "actor": entry["actor"],
                "qualifier": entry.get("qualifier"),
                "description": look,
                "source": source,
            }
            log(f"    {formal:<28} [{source}] {look[:70]}")

    return {
        "title": title,
        "season": season,
        "source_page": page,
        "characters": characters,
    }


def brave_key() -> str | None:
    return os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", help="Video filename or a show title.")
    parser.add_argument("--season", type=int, help="Override the season parsed from the name.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Keep at most N characters (default 0, keep all).")
    parser.add_argument("--out", default=str(HERE / "cast.json"))
    parser.add_argument("--no-descriptions", action="store_true",
                        help="Skip the per-character search (names only).")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    load_dotenv(HERE.parent / ".env")
    load_dotenv()

    key = brave_key()
    if not key:
        parser.error("Set BRAVE_SEARCH_API_KEY in .env")

    try:
        payload = build_cast(
            args.video,
            key=key,
            season=args.season,
            limit=args.limit,
            descriptions=not args.no_descriptions,
        )
    except (LookupError, RuntimeError, httpx.HTTPError) as error:
        sys.exit(str(error))

    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nWrote {len(payload['characters'])} characters to {args.out}")


if __name__ == "__main__":
    main()
