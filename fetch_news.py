#!/usr/bin/env python3
"""
Wireless Router & WiFi News Monitor
Fetches news from RSS feeds about major router brands and WiFi standards,
generates daily briefings, and updates the dashboard.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from xml.etree import ElementTree
import html

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state(state_file):
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)
    return {"seen_ids": [], "last_run": None}


def save_state(state_file, state):
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def fetch_rss(url, timeout=15):
    """Fetch and parse an RSS/Atom feed, return list of entries."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) WirelessNewsBot/1.0"
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (URLError, OSError, TimeoutError) as e:
        print(f"  [WARN] Failed to fetch {url}: {e}")
        return []

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        print(f"  [WARN] Failed to parse feed {url}: {e}")
        return []

    entries = []
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


def parse_rss_item(item, ns):
    """Parse an RSS 2.0 <item> element."""
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


def parse_atom_entry(item, ns):
    """Parse an Atom <entry> element."""
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


def strip_html(text):
    """Remove HTML tags from text."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def parse_date(date_str):
    """Parse various date formats from RSS feeds."""
    if not date_str:
        return None

    # Common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",      # RFC 822
        "%a, %d %b %Y %H:%M:%S %Z",      # RFC 822 with timezone name
        "%Y-%m-%dT%H:%M:%S%z",            # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",             # ISO 8601 UTC
        "%Y-%m-%dT%H:%M:%S.%f%z",         # ISO 8601 with microseconds
        "%Y-%m-%d %H:%M:%S",              # Simple datetime
        "%Y-%m-%d",                        # Simple date
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


def extract_domain(url):
    """Extract domain name from URL for source attribution."""
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


def article_id(article):
    """Generate a unique ID for deduplication."""
    key = (article.get("title", "") + article.get("link", "")).lower()
    return hashlib.md5(key.encode()).hexdigest()[:12]


def matches_keywords(article, config):
    """Check if article matches any of our keywords."""
    text = (
        article.get("title", "") + " " + article.get("description", "")
    ).lower()

    all_keywords = config["keywords"]["brands"] + config["keywords"]["technologies"]
    matched = []
    for kw in all_keywords:
        if kw.lower() in text:
            matched.append(kw)

    return matched


def categorize_article(matched_keywords, config):
    """Categorize an article based on matched keywords."""
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


def is_google_news_feed(feed_url):
    """Check if this is a Google News RSS feed (pre-filtered by query)."""
    return "news.google.com" in feed_url


def fetch_all_news(config):
    """Fetch news from all configured RSS feeds."""
    all_articles = []
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


def deduplicate(articles, seen_ids):
    """Remove duplicate articles."""
    unique = []
    new_ids = set(seen_ids)

    for a in articles:
        aid = article_id(a)
        if aid not in new_ids:
            new_ids.add(aid)
            a["_id"] = aid
            unique.append(a)

    return unique, list(new_ids)


def generate_briefing(articles, date_str):
    """Generate a daily briefing markdown file."""
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


def update_dashboard(config, briefings_dir):
    """Update the dashboard.md homepage.

    Layout:
    - Briefings sorted by date, newest first.
    - The LATEST briefing: bold + red text, top 5 headlines shown.
    - Older briefings: normal weight, top 3 headlines shown.
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


def main():
    config = load_config()
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

    # Update dashboard
    update_dashboard(config, config["briefings_dir"])
    print(f"Dashboard updated: {config['dashboard_path']}")

    # Print top headlines
    if articles:
        print(f"\nTop headlines:")
        for i, a in enumerate(articles[:10], 1):
            print(f"  {i}. [{a.get('source', '')}] {a['title']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
