"""Official feeds, exact AWS tag inclusion, and paginated GitHub releases."""

import hashlib
import html
import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from defusedxml import ElementTree


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    parser = Text()
    parser.feed(value or "")
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def canonical(url):
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("Article URL must be HTTP(S)")
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")]
    return urlunsplit(
        (
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def stamp(value):
    if not value:
        raise ValueError("Missing publication date")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        raise ValueError("Publication date must include timezone")
    return int(dt.timestamp())


def tags_of(item):
    return sorted(
        {
            tag.strip()
            for c in item.findall("category")
            for tag in (c.text or c.get("term", "")).split(",")
            if tag.strip()
        }
    )


def normalize(source, uid, url, title, excerpt, published, tags=(), preview=False):
    title = plain(title)[:1000]
    return dict(
        id=hashlib.sha256((source["id"] + ":" + uid).encode()).hexdigest(),
        upstream_id=uid,
        source=source["id"],
        technology=source["technology"],
        url=canonical(url),
        title=title,
        excerpt=plain(excerpt)[:1200],
        published=stamp(published),
        matched_tags=list(tags),
        preview=preview
        or bool(re.search(r"\b(preview|alpha|beta|release candidate)\b", title, re.I)),
        regional=bool(re.search(r"\b(region|regions|govcloud)\b", title, re.I)),
    )


def parse_feed(body, source, allowed):
    root = ElementTree.fromstring(body)
    # Strip namespaces for RSS/Atom's common fields.
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    entries = root.findall(".//item") if root.tag != "feed" else root.findall("entry")
    if root.tag not in ("rss", "feed", "RDF"):
        raise ValueError("Unexpected feed document")
    result = []
    for item in entries:
        tags = sorted(set(tags_of(item)) & set(allowed)) if source["id"] == "aws" else []
        if source["id"] == "aws" and not tags:
            continue
        if (
            source.get("title_contains")
            and source["title_contains"] not in (item.findtext("title") or "").lower()
        ):
            continue
        links = item.findall("link")
        url = next(
            (e.get("href") or e.text for e in links if e.get("rel", "alternate") == "alternate"), ""
        )
        result.append(
            normalize(
                source,
                item.findtext("guid") or item.findtext("id") or url,
                url,
                item.findtext("title"),
                item.findtext("description")
                or item.findtext("summary")
                or item.findtext("content"),
                item.findtext("pubDate") or item.findtext("published") or item.findtext("updated"),
                tags,
            )
        )
    return result


def fetch(url):
    request = Request(
        url,
        headers={
            "User-Agent": "technology-digest/0.1",
            "Accept": "application/atom+xml,application/rss+xml,application/json",
        },
    )
    with urlopen(request, timeout=25) as response:
        body = response.read(12_000_001)
        if len(body) > 12_000_000:
            raise ValueError("Source exceeds 12 MB safety limit")
        return body, response.headers.get("Link", "")


def collect_source(source, allowed, snapshot, now, fetcher=fetch):
    url = source["url"]
    items = []
    seen = set()
    while url:
        if url in seen or len(seen) >= 100:
            raise ValueError("Release pagination did not complete")
        seen.add(url)
        body, link = fetcher(url)
        snapshot(len(seen), body)
        if source["kind"] == "rss":
            return parse_feed(body, source, allowed)
        releases = json.loads(body)
        if not isinstance(releases, list):
            raise ValueError("Invalid GitHub releases response")
        for release in releases:
            if release["draft"]:
                continue
            item = normalize(
                source,
                str(release["id"]),
                release["html_url"],
                release["name"] or release["tag_name"],
                release["body"],
                release["published_at"],
                preview=release["prerelease"],
            )
            if item["published"] >= now - 90 * 86400:
                items.append(item)
        # GitHub pagination follows Link, never assumes a short page means the end.
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None
        if url and (urlsplit(url).netloc != "api.github.com" or urlsplit(url).scheme != "https"):
            raise ValueError("Unexpected GitHub pagination host")
    return items
