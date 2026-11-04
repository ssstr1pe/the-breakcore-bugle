# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests",
#     "beautifulsoup4",
#     "pyyaml",
# ]
# ///
"""Scaffold a monthly Bugle edition from input.yaml and TEMPLATE.md.

Usage: uv run scaffold.py [--input input.yaml] [--force]

Writes src/content/posts/<date>.md and artwork to src/content/posts/images/<date>/.
"""

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "TEMPLATE.md"
POSTS = ROOT / "src" / "content" / "posts"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
}
TIMEOUT = 30


@dataclass
class Entry:
    url: str
    artist: str = "ARTIST"
    release: str = "RELEASE"
    art_url: str | None = None
    image: str = "./something.jpg"
    ok: bool = False

    @property
    def platform(self) -> str:
        return "SoundCloud" if is_soundcloud(self.url) else "Bandcamp"


def is_soundcloud(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def is_bandcamp(url: str) -> bool:
    return (urlparse(url).hostname or "").endswith("bandcamp.com")


def fetch_bandcamp(entry: Entry) -> None:
    resp = requests.get(entry.url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    tralbum = soup.find("script", attrs={"data-tralbum": True})
    if tralbum:
        data = json.loads(tralbum["data-tralbum"])
        entry.artist = data["artist"]
        entry.release = data["current"]["title"]
    else:
        og_title = soup.find("meta", property="og:title")
        release, _, artist = html.unescape(og_title["content"]).rpartition(", by ")
        entry.artist, entry.release = artist, release

    og_image = soup.find("meta", property="og:image")
    if og_image:
        entry.art_url = re.sub(r"_\d+\.jpg$", "_10.jpg", og_image["content"])


def fetch_soundcloud(entry: Entry) -> None:
    resp = requests.get(
        "https://soundcloud.com/oembed",
        params={"format": "json", "url": entry.url},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    entry.artist = data["author_name"]
    entry.release = data["title"].removesuffix(f" by {data['author_name']}")
    if data.get("thumbnail_url"):
        entry.art_url = re.sub(r"-t\d+x\d+\.", "-t500x500.", data["thumbnail_url"])


def fetch(url: str) -> Entry:
    entry = Entry(url=url)
    try:
        if is_soundcloud(url):
            fetch_soundcloud(entry)
        elif is_bandcamp(url):
            fetch_bandcamp(entry)
        else:
            raise ValueError("not a Bandcamp or SoundCloud URL")
        entry.ok = True
    except Exception as e:
        print(f"warning: {url}: {e}", file=sys.stderr)
    return entry


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def download_art(entry: Entry, dirname: str) -> None:
    if not entry.art_url:
        return
    slug = slugify(f"{entry.artist}-{entry.release}")
    out_dir = POSTS / "images" / dirname
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = [p for p in out_dir.glob(f"{slug}.*") if p.suffix in (".jpg", ".png")]
    if existing:
        entry.image = f"./images/{dirname}/{existing[0].name}"
        return

    try:
        resp = requests.get(entry.art_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"warning: artwork for {entry.url}: {e}", file=sys.stderr)
        entry.ok = False
        return
    ext = "png" if "png" in resp.headers.get("Content-Type", "") else "jpg"
    (out_dir / f"{slug}.{ext}").write_bytes(resp.content)
    entry.image = f"./images/{dirname}/{slug}.{ext}"


def extract_block(text: str, link_prefix: str) -> tuple[str, str]:
    """Return (text with block replaced by a marker, block)."""
    pattern = re.compile(
        r"### ARTIST - RELEASE\n.*?^" + re.escape(link_prefix) + r".*?$",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        sys.exit(f"error: couldn't find the '{link_prefix}' block in {TEMPLATE}")
    marker = f"@@{link_prefix}@@"
    return text[: match.start()] + marker + text[match.end() :], match.group(0)


def fill_block(block: str, entry: Entry) -> str:
    block = block.replace("ARTIST", entry.artist).replace("RELEASE", entry.release)
    block = block.replace("./something.jpg", entry.image)
    block = re.sub(r"\[(Bandcamp|SoundCloud)\]\(url\)", f"[{entry.platform}]({entry.url})", block)
    return block


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=ROOT / "input.yaml")
    parser.add_argument("--force", action="store_true", help="overwrite output if it exists")
    args = parser.parse_args()

    data = yaml.safe_load(args.input.read_text()) or {}
    # YAML parses an unquoted 2026-11-01 as a date; accept strings too.
    published = data.get("date") or date.today()
    if isinstance(published, str):
        published = date.fromisoformat(published)
    month = str(data.get("month") or published.strftime("%B"))
    output = POSTS / f"{published.isoformat()}.md"
    if output.exists() and not args.force:
        sys.exit(f"error: {output} already exists (use --force to overwrite)")

    sections = {}
    for key in ("releases", "singles"):
        entries = []
        for url in data.get(key) or []:
            print(f"{key}: {url}", file=sys.stderr)
            entry = fetch(url)
            download_art(entry, published.isoformat())
            entries.append(entry)
        sections[key] = entries

    text = TEMPLATE.read_text()
    text, release_block = extract_block(text, "Buy it on")
    text, single_block = extract_block(text, "Listen on")
    text = text.replace(
        "@@Buy it on@@",
        "\n\n".join(fill_block(release_block, e) for e in sections["releases"]),
    )
    text = text.replace(
        "@@Listen on@@",
        "\n\n".join(fill_block(single_block, e) for e in sections["singles"]),
    )
    text = text.replace("$DATE", published.isoformat()).replace("$MONTH", month)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)

    failed = [e.url for es in sections.values() for e in es if not e.ok]
    print(
        f"wrote {output.relative_to(ROOT)}: {len(sections['releases'])} releases, "
        f"{len(sections['singles'])} singles",
        file=sys.stderr,
    )
    if failed:
        print("needs fixing by hand:", *failed, sep="\n  ", file=sys.stderr)


if __name__ == "__main__":
    main()
