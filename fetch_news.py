#!/usr/bin/env python3
"""
Wireless Router & WiFi News Monitor

Fetches news from RSS feeds about major router brands and WiFi standards,
generates daily briefings, and updates the dashboard.

Features:
- Parallel RSS fetching for faster execution
- Automatic retry with exponential backoff
- Type-safe article handling with dataclasses
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # Exponential backoff multiplier
MAX_WORKERS = 8  # Parallel fetch threads


@dataclass
class Article:
    """Represents a news article with metadata."""

    title: str
    link: str
    description: str
    date: datetime | None
    date_str: str
    source: str
    tags: list[str] = field(default_factory=list)
    article_id: str = ""

    def __post_init__(self) -> None:
        if not self.article_id:
            key = (self.title + self.link).lower()
            self.article_id = hashlib.md5(key.encode()).hexdigest()[:12]


def load_config() -> dict[str, Any]:
    """Load configuration from config.json."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state(state_file: str) -> dict[str, Any]:
    """Load state from state file."""
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)
    return {"seen_ids": [], "last_run": None}


def save_state(state_file: str, state: dict[str, Any]) -> None:
    """Save state to state file."""
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def fetch_rss_with_retry(
    url: str, timeout: int = 15, max_retries: int = MAX_RETRIES
) -> list[dict[str, Any]]:
    """Fetch and parse an RSS/Atom feed with retry logic.

    Args:
        url: The RSS feed URL to fetch
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts

    Returns:
        List of parsed feed entries
    """
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) WirelessNewsBot/1.0"}
    req = Request(url, headers=headers)

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            break
        except (URLError, OSError, TimeoutError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = RETRY_BACKOFF ** attempt
                print(f"  [RETRY] Attempt {attempt + 1}/{max_retries} failed for {url}, "
                      f"retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
            continue
    else:
        print(f"  [WARN] Failed to fetch {url} after {max_retries} attempts: {last_error}")
        return []

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        print(f"  [WARN] Failed to parse feed {url}: {e}")
        return []

    entries: list[dict[str, Any]] = []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc": "http://purl.org/dc/elements/1.1/",
        "media": "http://search.yahoo.com/mrss/",
    }

    # Try RSS 2.0 format
    for item in root.findall(".//item"):
        entry = parse_rss_item(item, ns)
        if entry:
            entries.append(entry)

    # Try Atom format
    if not entries:
        for item in root.findall(".//atom:entry", ns):
            entry = parse_atom_entry(item, ns)
            if entry:
                entries.append(entry)

    return entries


# Keep old function name for compatibility
def fetch_rss(url: str, timeout: int = 15) -> list[dict[str, Any]]:
    """Fetch and parse an RSS/Atom feed, return list of entries."""
    return fetch_rss_with_retry(url, timeout)


def parse_rss_item(item: ElementTree.Element, ns: dict[str, str]) -> dict[str, Any] | None:
    """Parse an RSS 2.0 <item> element.

    Args:
        item: XML element representing an RSS item
        ns: Namespace dictionary for XML parsing

    Returns:
        Dictionary with article data or None if invalid
    """
    title = item.findtext("title", "").strip()
    link = item.findtext("link", "").strip()
    desc = item.findtext("description", "").strip()
    pub_date = item.findtext("pubDate", "").strip()
    source = item.findtext("source", "").strip()

    if not title:
        return None

    # Clean HTML from description
    desc = strip_html(desc)
    if len(desc) > 300:
        desc = desc[:297] + "..."

    # Parse date
    parsed_date = parse_date(pub_date)

    return {
        "title": html.unescape(title),
        "link": link,
        "description": html.unescape(desc),
        "date": parsed_date,
        "date_str": parsed_date.strftime("%Y-%m-%d") if parsed_date else "",
        "source": source or extract_domain(link),
    }


def parse_atom_entry(item: ElementTree.Element, ns: dict[str, str]) -> dict[str, Any] | None:
    """Parse an Atom <entry> element.

    Args:
        item: XML element representing an Atom entry
        ns: Namespace dictionary for XML parsing

    Returns:
        Dictionary with article data or None if invalid
    """
    title = item.findtext("atom:title", "", ns).strip()
    link_el = item.find("atom:link[@rel='alternate']", ns)
    if link_el is None:
        link_el = item.find("atom:link", ns)
    link = link_el.get("href", "") if link_el is not None else ""
    summary = item.findtext("atom:summary", "", ns).strip()
    content = item.findtext("atom:content", "", ns).strip()
    updated = item.findtext("atom:updated", "", ns).strip()
    published = item.findtext("atom:published", "", ns).strip()

    if not title:
        return None

    desc = strip_html(summary or content)
    if len(desc) > 300:
        desc = desc[:297] + "..."

    parsed_date = parse_date(published or updated)

    return {
        "title": html.unescape(title),
        "link": link,
        "description": html.unescape(desc),
        "date": parsed_date,
        "date_str": parsed_date.strftime("%Y-%m-%d") if parsed_date else "",
        "source": extract_domain(link),
    }


def strip_html(text: str) -> str:
    """Remove HTML tags from text.

    Args:
        text: HTML string to clean

    Returns:
        Plain text with HTML tags removed
    """
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def parse_date(date_str: str) -> datetime | None:
    """Parse various date formats from RSS feeds.

    Args:
        date_str: Date string in various formats

    Returns:
        Parsed datetime object or None if parsing fails
    """
    if not date_str:
        return None

    # Common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",  # RFC 822
        "%a, %d %b %Y %H:%M:%S %Z",  # RFC 822 with timezone name
        "%Y-%m-%dT%H:%M:%S%z",  # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",  # ISO 8601 UTC
        "%Y-%m-%dT%H:%M:%S.%f%z",  # ISO 8601 with microseconds
        "%Y-%m-%d %H:%M:%S",  # Simple datetime
        "%Y-%m-%d",  # Simple date
    ]

    # Strip timezone offset like +0000 that some parsers can't handle
    cleaned = re.sub(r"\s*\+\d{4}$", "", date_str.strip())
    cleaned2 = date_str.strip()

    for fmt in formats:
        for d in [cleaned2, cleaned]:
            try:
                return datetime.strptime(d, fmt).replace(tzinfo=None)
            except ValueError:
                continue

    return None


def extract_domain(url: str) -> str:
    """Extract domain name from URL for source attribution.

    Args:
        url: Full URL string

    Returns:
        Friendly domain name or raw domain
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        # Friendly names
        friendly = {
            "theverge.com": "The Verge",
            "arstechnica.com": "Ars Technica",
            "techcrunch.com": "TechCrunch",
            "cnet.com": "CNET",
            "tomshardware.com": "Tom's Hardware",
            "pcmag.com": "PCMag",
            "engadget.com": "Engadget",
            "wired.com": "Wired",
            "zdnet.com": "ZDNet",
            "tomsguide.com": "Tom's Guide",
            "news.google.com": "Google News",
        }
        return friendly.get(domain, domain)
    except Exception:
        return ""


def article_id(article: dict[str, Any]) -> str:
    """Generate a unique ID for deduplication.

    Args:
        article: Article dictionary

    Returns:
        12-character hash ID
    """
    key = (article.get("title", "") + article.get("link", "")).lower()
    return hashlib.md5(key.encode()).hexdigest()[:12]


def matches_keywords(article: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Check if article matches any of our keywords.

    Args:
        article: Article dictionary
        config: Configuration dictionary

    Returns:
        List of matched keywords
    """
    text = (article.get("title", "") + " " + article.get("description", "")).lower()

    all_keywords = config["keywords"]["brands"] + config["keywords"]["technologies"]
    matched = []
    for kw in all_keywords:
        if kw.lower() in text:
            matched.append(kw)

    return matched


def categorize_article(matched_keywords: list[str], config: dict[str, Any]) -> list[str]:
    """Categorize an article based on matched keywords.

    Args:
        matched_keywords: List of matched keyword strings
        config: Configuration dictionary

    Returns:
        Sorted list of category tags
    """
    brands_lower = [b.lower() for b in config["keywords"]["brands"]]
    techs_lower = [t.lower() for t in config["keywords"]["technologies"]]

    categories = set()
    for kw in matched_keywords:
        kw_l = kw.lower()
        if kw_l in brands_lower:
            categories.add(kw)
        if kw_l in techs_lower:
            categories.add(kw)

    return sorted(categories)


def is_google_news_feed(feed_url: str) -> bool:
    """Check if this is a Google News RSS feed (pre-filtered by query).

    Args:
        feed_url: RSS feed URL

    Returns:
        True if this is a Google News feed
    """
    return "news.google.com" in feed_url


def _fetch_single_feed(
    feed: dict[str, str], config: dict[str, Any], cutoff: datetime
) -> list[dict[str, Any]]:
    """Fetch and filter a single RSS feed.

    Args:
        feed: Feed configuration with 'name' and 'url'
        config: Global configuration
        cutoff: Date cutoff for filtering old articles

    Returns:
        List of filtered articles from this feed
    """
    feed_articles: list[dict[str, Any]] = []
    entries = fetch_rss_with_retry(feed["url"])
    google_feed = is_google_news_feed(feed["url"])

    for entry in entries:
        # Filter by date
        if entry["date"] and entry["date"] < cutoff:
            continue

        # For Google News feeds, articles are already filtered by search query
        if google_feed:
            entry["_tags"] = categorize_article(
                matches_keywords(entry, config), config
            ) or ["Wireless/WiFi"]
            feed_articles.append(entry)
        else:
            # For general tech feeds, must match our keywords
            matched = matches_keywords(entry, config)
            if matched:
                entry["_tags"] = categorize_article(matched, config)
                feed_articles.append(entry)

    return feed_articles


def fetch_all_news(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch news from all configured RSS feeds in parallel.

    Uses ThreadPoolExecutor for concurrent fetching, with automatic
    retry on failure.

    Args:
        config: Configuration dictionary with RSS feeds

    Returns:
        List of all filtered articles from all feeds
    """
    all_articles: list[dict[str, Any]] = []
    cutoff = datetime.now() - timedelta(days=config.get("lookback_days", 3))
    feeds = config["rss_feeds"]

    print(f"  Fetching {len(feeds)} feeds in parallel (max {MAX_WORKERS} workers)...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all feed fetches
        future_to_feed = {
            executor.submit(_fetch_single_feed, feed, config, cutoff): feed
            for feed in feeds
        }

        # Collect results as they complete
        for future in as_completed(future_to_feed):
            feed = future_to_feed[future]
            try:
                articles = future.result()
                print(f"    {feed['name']}: {len(articles)} articles")
                all_articles.extend(articles)
            except Exception as e:
                print(f"    [ERROR] {feed['name']}: {e}")

    elapsed = time.time() - start_time
    print(f"  Completed in {elapsed:.1f}s")

    return all_articles


def fetch_all_news_sequential(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch news from all configured RSS feeds sequentially (fallback).

    Args:
        config: Configuration dictionary with RSS feeds

    Returns:
        List of all filtered articles from all feeds
    """
    all_articles: list[dict[str, Any]] = []
    cutoff = datetime.now() - timedelta(days=config.get("lookback_days", 3))

    for feed in config["rss_feeds"]:
        print(f"  Fetching: {feed['name']}...")
        entries = fetch_rss(feed["url"])
        print(f"    Got {len(entries)} entries")

        google_feed = is_google_news_feed(feed["url"])

        for entry in entries:
            # Filter by date
            if entry["date"] and entry["date"] < cutoff:
                continue

            # For Google News feeds, articles are already filtered by search query
            if google_feed:
                entry["_tags"] = categorize_article(
                    matches_keywords(entry, config), config
                ) or ["Wireless/WiFi"]
                all_articles.append(entry)
            else:
                # For general tech feeds, must match our keywords
                matched = matches_keywords(entry, config)
                if matched:
                    entry["_tags"] = categorize_article(matched, config)
                    all_articles.append(entry)

    return all_articles


def deduplicate(
    articles: list[dict[str, Any]], seen_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Remove duplicate articles.

    Args:
        articles: List of article dictionaries
        seen_ids: List of previously seen article IDs

    Returns:
        Tuple of (unique articles, updated seen IDs list)
    """
    unique: list[dict[str, Any]] = []
    new_ids = set(seen_ids)

    for a in articles:
        aid = article_id(a)
        if aid not in new_ids:
            new_ids.add(aid)
            a["_id"] = aid
            unique.append(a)

    return unique, list(new_ids)


def generate_briefing(articles: list[dict[str, Any]], date_str: str) -> str:
    """Generate a daily briefing markdown file.

    Args:
        articles: List of article dictionaries
        date_str: Date string for the briefing title

    Returns:
        Markdown content as string
    """
    lines = []
    lines.append(f"# Wireless & WiFi News Briefing - {date_str}")
    lines.append("")
    lines.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append(f"*Articles: {len(articles)}*")
    lines.append("")

    if not articles:
        lines.append("> No relevant news found today.")
        return "\n".join(lines)

    # Group by tags
    tagged = {}
    for a in articles:
        for tag in a.get("_tags", ["General"]):
            tagged.setdefault(tag, []).append(a)

    # Print summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| # | Title | Source | Date | Tags |")
    lines.append("|---|-------|--------|------|------|")

    for i, a in enumerate(articles, 1):
        title = a["title"]
        if len(title) > 80:
            title = title[:77] + "..."
        tags = ", ".join(a.get("_tags", []))
        source = a.get("source", "")
        date = a.get("date_str", "")
        link = a.get("link", "")
        lines.append(f"| {i} | [{title}]({link}) | {source} | {date} | {tags} |")

    lines.append("")

    # Detailed articles
    lines.append("## Articles")
    lines.append("")

    for i, a in enumerate(articles, 1):
        lines.append(f"### {i}. {a['title']}")
        lines.append("")
        lines.append(f"- **Source**: {a.get('source', 'Unknown')}")
        lines.append(f"- **Date**: {a.get('date_str', 'Unknown')}")
        lines.append(f"- **Tags**: {', '.join(a.get('_tags', []))}")
        if a.get("link"):
            lines.append(f"- **Link**: {a['link']}")
        lines.append("")
        if a.get("description"):
            lines.append(f"> {a['description']}")
            lines.append("")

    lines.append("---")
    lines.append("*Generated by wireless-news monitor*")
    return "\n".join(lines)


def update_dashboard(config: dict[str, Any], briefings_dir: str) -> None:
    """Update the dashboard.md homepage.

    Layout:
    - Briefings sorted by date, newest first.
    - The LATEST briefing: bold + red text, top 5 headlines shown.
    - Older briefings: normal weight, top 3 headlines shown.

    Args:
        config: Configuration dictionary
        briefings_dir: Path to briefings directory
    """
    briefings_path = Path(briefings_dir)
    briefing_files = sorted(briefings_path.glob("*.md"), reverse=True)

    now = datetime.now()

    lines = []
    lines.append("# Wireless Router & WiFi News Monitor")
    lines.append("")
    lines.append(f"Last updated: **{now.strftime('%Y-%m-%d %H:%M')}**")
    lines.append("")
    lines.append("Tracking: **Linksys** | **Netgear** | **TP-Link** | **Asus** | "
                 "**D-Link** | **Ubiquiti** | **WiFi 7** | **WiFi 8** | **Mesh WiFi**")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## News Briefings")
    lines.append("")

    if not briefing_files:
        lines.append("No briefings yet. Run `python3 fetch_news.py` to generate.")
    else:
        for idx, bf in enumerate(briefing_files):
            date_str = bf.stem  # e.g. "2026-04-11"
            rel_path = f"briefings/{bf.name}"
            is_latest = (idx == 0)  # first file = newest

            # Read briefing to extract article count and top headlines
            headlines = []
            article_count = 0
            with open(bf) as f:
                content = f.read()

            m = re.search(r"\*Articles:\s*(\d+)\*", content)
            if m:
                article_count = int(m.group(1))

            for tbl_line in content.split("\n"):
                if tbl_line.startswith("| ") and "[" in tbl_line and "](" in tbl_line:
                    tm = re.search(r"\[([^\]]+)\]\(", tbl_line)
                    if tm:
                        headlines.append(tm.group(1))
                if len(headlines) >= 5:
                    break

            if is_latest:
                # Latest briefing: bold + red
                lines.append(f'<p style="color:red;font-weight:bold;font-size:1.1em;">')
                lines.append(f'  <a href="{rel_path}" style="color:red;font-weight:bold;">')
                lines.append(f'    {date_str} - Daily Briefing ({article_count} articles)')
                lines.append(f'  </a>')
                lines.append(f'</p>')
                lines.append("")
                if headlines:
                    for h in headlines[:5]:
                        lines.append(f'<span style="color:red;">**- {h}**</span>  ')
                    lines.append("")
            else:
                # Older briefings: normal style, no bold
                lines.append(f"[{date_str} - Daily Briefing ({article_count} articles)]({rel_path})")
                lines.append("")
                if headlines:
                    for h in headlines[:3]:
                        lines.append(f"- {h}")
                    lines.append("")

    lines.append("---")
    lines.append("*Generated by wireless-news monitor*")
    lines.append("")

    with open(config["dashboard_path"], "w") as f:
        f.write("\n".join(lines))


def update_readme(articles: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Update the README.md (GitHub homepage) with briefing summaries.

    - Latest briefing: bold + red, top 10 headlines
    - Older briefings: normal style in collapsible section

    Args:
        articles: List of article dictionaries (used for count)
        config: Configuration dictionary
    """
    briefings_path = Path(config["briefings_dir"])
    briefing_files = sorted(briefings_path.glob("*.md"), reverse=True)
    now = datetime.now()

    lines = []
    lines.append("# Wireless Router & WiFi News Monitor")
    lines.append("")
    lines.append("Daily news briefings covering major wireless router brands and WiFi standards.")
    lines.append("")
    lines.append("**Tracking**: Linksys | Netgear | TP-Link | Asus | D-Link | "
                 "Ubiquiti | WiFi 7 | WiFi 8 | Mesh WiFi")
    lines.append("")
    lines.append(f"*Last updated: {now.strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, bf in enumerate(briefing_files):
        date_str = bf.stem
        is_latest = (idx == 0)

        with open(bf) as f:
            content = f.read()

        # Extract article count
        article_count = 0
        m = re.search(r"\*Articles:\s*(\d+)\*", content)
        if m:
            article_count = int(m.group(1))

        # Extract headlines from the detailed "### N. Title" sections
        headlines = []
        article_re = re.compile(r"^### \d+\.\s+(.+)$")
        src_re = re.compile(r"^\- \*\*Source\*\*:\s+(.+)$")
        date_re = re.compile(r"^\- \*\*Date\*\*:\s+(.+)$")
        tags_re = re.compile(r"^\- \*\*Tags\*\*:\s+(.+)$")
        link_re = re.compile(r"^\- \*\*Link\*\*:\s+(.+)$")
        current = None
        for line in content.split("\n"):
            am = article_re.match(line)
            if am:
                if current:
                    headlines.append(current)
                current = {"title": am.group(1), "source": "", "date": "", "tags": "", "link": ""}
                continue
            if current:
                sm = src_re.match(line)
                if sm:
                    current["source"] = sm.group(1)
                dm = date_re.match(line)
                if dm:
                    current["date"] = dm.group(1)
                tgm = tags_re.match(line)
                if tgm:
                    current["tags"] = tgm.group(1)
                lm = link_re.match(line)
                if lm:
                    current["link"] = lm.group(1)
        if current:
            headlines.append(current)

        if is_latest:
            lines.append(f'<h3 style="color:red;">{date_str} - Daily Briefing ({article_count} articles)</h3>')
            lines.append("")
            for i, h in enumerate(headlines[:10], 1):
                title = h["title"]
                # Clean trailing " - Source" from title if present
                clean_title = re.sub(r"\s*-\s*[A-Za-z][\w\s'.&]*$", "", title)
                if clean_title == title:
                    clean_title = re.sub(r"\s*\|\s*[A-Za-z][\w\s'.&]*$", "", title)
                link = h.get("link", "")
                if link:
                    lines.append(
                        f'<span style="color:red;"><b>{i}. <a href="{link}" style="color:red;">{clean_title}</a></b>'
                        f' — {h["source"]}</span><br>'
                    )
                else:
                    lines.append(
                        f'<span style="color:red;"><b>{i}. {clean_title}</b>'
                        f' — {h["source"]}</span><br>'
                    )
            lines.append("")

            # Full table in collapsible
            if len(headlines) > 10:
                lines.append(f'<details><summary><b>View all {len(headlines)} articles...</b></summary>')
                lines.append("")
                lines.append("| # | Date | Title | Source | Tags |")
                lines.append("|---|------|-------|--------|------|")
                for i, h in enumerate(headlines, 1):
                    link = h.get("link", "")
                    title_cell = f"[{h['title']}]({link})" if link else h['title']
                    lines.append(f"| {i} | {h['date']} | {title_cell} | {h['source']} | {h['tags']} |")
                lines.append("")
                lines.append("</details>")
                lines.append("")
        else:
            # Older briefings: normal style
            lines.append(f"### {date_str} - Daily Briefing ({article_count} articles)")
            lines.append("")
            for h in headlines[:5]:
                link = h.get("link", "")
                if link:
                    lines.append(f"- [{h['title']}]({link}) — {h['source']}")
                else:
                    lines.append(f"- {h['title']} — {h['source']}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## How It Works")
    lines.append("")
    lines.append("```")
    lines.append("cron (daily) -> fetch_news.py -> RSS feeds (Google News, The Verge, Ars Technica, ...)")
    lines.append("                              -> filter by keywords (brands + WiFi standards)")
    lines.append("                              -> deduplicate")
    lines.append("                              -> generate briefing (briefings/YYYY-MM-DD.md)")
    lines.append("                              -> update README.md")
    lines.append("                              -> send email notification")
    lines.append("```")
    lines.append("")
    lines.append("**Manual run**: `python3 fetch_news.py`")
    lines.append("")
    lines.append("**Config**: [`config.json`](config.json)")
    lines.append("")
    lines.append("---")
    lines.append("*Generated by wireless-news monitor*")
    lines.append("")

    readme_path = SCRIPT_DIR / "README.md"
    with open(readme_path, "w") as f:
        f.write("\n".join(lines))


def send_email(
    articles: list[dict[str, Any]],
    date_str: str,
    smtp_conf_path: str,
    recipient: str | None = None,
    subject_tag: str = "WiFiNewsCreated",
) -> None:
    """Send briefing email.

    Args:
        articles: List of article dictionaries
        date_str: Date string for subject line
        smtp_conf_path: Path to SMTP configuration file
        recipient: Override recipient email address
        subject_tag: Tag for email subject line
    """
    if not os.path.exists(smtp_conf_path):
        print(f"  [EMAIL] {smtp_conf_path} not found, skipping email")
        return

    with open(smtp_conf_path) as f:
        smtp = json.load(f)

    to_addr = recipient or smtp.get("recipient")

    # Build email body
    body_lines = []
    body_lines.append(f"Wireless Router & WiFi News Briefing - {date_str}")
    body_lines.append("=" * 55)
    body_lines.append(f"Articles: {len(articles)}")
    body_lines.append("")

    for i, a in enumerate(articles, 1):
        body_lines.append(f"{i}. [{a.get('date_str', '')}] {a['title']}")
        body_lines.append(f"   Source: {a.get('source', 'Unknown')}  |  Tags: {', '.join(a.get('_tags', []))}")
        if a.get("description"):
            desc = a["description"]
            if len(desc) > 200:
                desc = desc[:197] + "..."
            body_lines.append(f"   {desc}")
        if a.get("link"):
            body_lines.append(f"   {a['link']}")
        body_lines.append("")

    body_lines.append("---")
    body_lines.append("Dashboard: https://github.com/JianrongXiao-Linksys/wireless-news")

    body = "\n".join(body_lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[{subject_tag}] WiFi & Router News Briefing - {date_str} ({len(articles)} articles)"
    msg["From"] = smtp["sender"]
    msg["To"] = to_addr

    try:
        server = smtplib.SMTP(smtp["smtp_server"], smtp["smtp_port"])
        server.starttls()
        server.login(smtp["sender"], smtp["password"])
        server.sendmail(smtp["sender"], to_addr, msg.as_string())
        server.quit()
        print(f"  [EMAIL] Sent to {to_addr}")
    except Exception as e:
        print(f"  [EMAIL] Failed: {e}")


def send_push(articles: list[dict[str, Any]], date_str: str, ntfy_topic: str) -> None:
    """Send push notification via ntfy.sh with clickable article links.

    Args:
        articles: List of article dictionaries
        date_str: Date string for notification title
        ntfy_topic: ntfy.sh topic name
    """
    import json as _json

    # Plain text format — URLs on their own line are auto-linked by the app
    body_lines = []
    for i, a in enumerate(articles[:10], 1):
        title = a["title"]
        clean = re.sub(r"\s*-\s*[A-Za-z][\w\s'.&]*$", "", title)
        source = a.get("source", "")
        link = a.get("link", "")
        body_lines.append(f"{i}. {clean} — {source}")
        if link:
            body_lines.append(f"   {link}")
        body_lines.append("")

    payload = {
        "topic": ntfy_topic,
        "title": f"WiFi News {date_str} - {len(articles)} articles",
        "message": "\n".join(body_lines).strip(),
        "priority": 3,
        "tags": ["newspaper", "wifi"],
        "click": "https://github.com/JianrongXiao-Linksys/wireless-news",
    }

    url = "https://ntfy.sh/"
    data = _json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"  [PUSH] Sent to ntfy.sh/{ntfy_topic}")
            else:
                print(f"  [PUSH] Unexpected status: {resp.status}")
    except Exception as e:
        print(f"  [PUSH] Failed: {e}")


def resolve_paths(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve relative paths in config against SCRIPT_DIR.

    Args:
        config: Configuration dictionary with relative paths

    Returns:
        Configuration dictionary with absolute paths
    """
    for key in ("dashboard_path", "briefings_dir", "state_file"):
        if key in config and not os.path.isabs(config[key]):
            config[key] = str(SCRIPT_DIR / config[key])
    return config


def main() -> int:
    """Main entry point for the news fetcher.

    Returns:
        Exit code (0 for success)
    """
    config = resolve_paths(load_config())
    os.makedirs(config["briefings_dir"], exist_ok=True)
    state = load_state(config["state_file"])
    today_str = datetime.now().strftime("%Y-%m-%d")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Wireless News Monitor")
    print(f"  Lookback: {config.get('lookback_days', 3)} days")
    print()

    # Fetch news from all sources
    print("Fetching news from RSS feeds...")
    all_articles = fetch_all_news(config)
    print(f"\nTotal articles found: {len(all_articles)}")

    # Deduplicate
    seen_ids = state.get("seen_ids", [])
    # Keep seen_ids to a reasonable size (last 2000)
    if len(seen_ids) > 2000:
        seen_ids = seen_ids[-1000:]

    articles, new_seen_ids = deduplicate(all_articles, seen_ids)
    print(f"New unique articles: {len(articles)}")

    # Sort by date (newest first)
    articles.sort(key=lambda a: a.get("date") or datetime.min, reverse=True)

    # Limit
    max_articles = config.get("max_articles_per_briefing", 30)
    articles = articles[:max_articles]

    # Generate briefing
    briefing_content = generate_briefing(articles, today_str)
    briefing_file = Path(config["briefings_dir"]) / f"{today_str}.md"
    with open(briefing_file, "w") as f:
        f.write(briefing_content)
    print(f"Briefing saved: {briefing_file}")

    # Update state
    state["seen_ids"] = new_seen_ids
    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(config["state_file"], state)

    # Update dashboard and README
    update_dashboard(config, config["briefings_dir"])
    print(f"Dashboard updated: {config['dashboard_path']}")

    update_readme(articles, config)
    print(f"README updated: {SCRIPT_DIR / 'README.md'}")

    # Send email and push notification
    smtp_conf = config.get("smtp_conf", str(SCRIPT_DIR / ".smtp.conf"))
    recipient = config.get("email_recipient", "jianrong.xiao@linksys.com")
    ntfy_topic = config.get("ntfy_topic", "wifi-news-jianrong")
    if articles:
        send_email(articles, today_str, smtp_conf, recipient)
        send_push(articles, today_str, ntfy_topic)

    # Print top headlines
    if articles:
        print(f"\nTop headlines:")
        for i, a in enumerate(articles[:10], 1):
            print(f"  {i}. [{a.get('source', '')}] {a['title']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
