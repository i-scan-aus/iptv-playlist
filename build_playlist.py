import concurrent.futures
import json
import os
import re
import subprocess
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

BASE = "https://iptv-org.github.io/api/"
OUTPUT = Path("playlist.m3u")
REPORT = Path("validation-report.json")

COUNTRIES = {"AU": "Australia", "UK": "UK", "US": "USA"}
WORKERS = 10
TIMEOUT = 12
MAX_TESTS_PER_CHANNEL = 5

BLOCKED_CATEGORIES = {"religious", "shopping"}
BLOCKED_NAMES = re.compile(
    r"\b(church|gospel|christian|islamic|quran|"
    r"bible|worship|shopping|qvc|hsn|"
    r"teleshopping|jewelry television)\b",
    re.I,
)

# Known examples of UK-restricted live delivery networks.
# This is a conservative filter, not a complete geo-block list.
BLOCKED_HOSTS = (
    "vs-cmaf-pushb-uk-live.akamaized.net",
    "vs-hls-pushb-uk-live.akamaized.net",
    "vs-cmaf-pushb-uk.live.fastly.md.bbc.co.uk",
    "vs-hls-pushb-uk.live.fastly.md.bbc.co.uk",
)

CATEGORY_NAMES = {
    "news": "News",
    "sports": "Sports",
    "movies": "Movies",
    "documentary": "Documentaries",
    "kids": "Kids",
    "animation": "Kids",
    "music": "Music",
    "comedy": "Comedy",
    "series": "TV Series",
    "entertainment": "Entertainment",
    "education": "Education",
    "science": "Science",
    "travel": "Travel",
    "cooking": "Food & Cooking",
    "business": "Business",
    "weather": "Weather",
    "lifestyle": "Lifestyle",
    "family": "Family",
    "general": "General",
}

PRIORITY = list(CATEGORY_NAMES)


def download(name):
    req = urllib.request.Request(
        BASE + name + ".json",
        headers={"User-Agent": "IPTVBuilder/3.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def clean(value):
    return (
        str(value or "")
        .replace('"', "'")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def category_for(channel):
    categories = channel.get("categories") or []
    for category in PRIORITY:
        if category in categories:
            return CATEGORY_NAMES[category]
    return "General"


def rank(stream):
    url = stream["url"].lower()
    score = 0

    if ".m3u8" in url:
        score += 100
    elif ".mpd" in url:
        score += 20

    if url.startswith("https://"):
        score += 10

    if not stream.get("user_agent"):
        score += 5

    if not stream.get("referrer"):
        score += 5

    height = stream.get("height")
    if isinstance(height, (int, float)):
        score += min(height, 2160) / 100

    return score


def probe(stream):
    url = stream["url"]
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json",
        "-analyzeduration", "3000000",
        "-probesize", "3000000",
    ]

    headers = []

    if stream.get("user_agent"):
        command += ["-user_agent", stream["user_agent"]]

    if stream.get("referrer"):
        headers.append("Referer: " + stream["referrer"])

    if headers:
        command += [
            "-headers",
            "\r\n".join(headers) + "\r\n",
        ]

    command += [url]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )

        if result.returncode != 0:
            return False

        data = json.loads(result.stdout)
        streams = data.get("streams") or []

        # Require a recognisable video stream.
        return any(
            s.get("codec_type") == "video"
            and s.get("codec_name") not in (None, "unknown")
            for s in streams
        )

    except (
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
    ):
        return False


def make_entry(channel, stream, logo):
    name = clean(channel.get("name"))
    cid = clean(channel["id"])
    country = COUNTRIES[channel["country"]]
    category = category_for(channel)
    group = f"{country} | {category}"

    entry = (
        f'#EXTINF:-1 tvg-id="{cid}" '
        f'tvg-logo="{clean(logo)}" '
        f'group-title="{group}",{name}\n'
    )

    if stream.get("user_agent"):
        entry += (
            "#EXTVLCOPT:http-user-agent="
            + clean(stream["user_agent"]) + "\n"
        )

    if stream.get("referrer"):
        entry += (
            "#EXTVLCOPT:http-referrer="
            + clean(stream["referrer"]) + "\n"
        )

    entry += stream["url"] + "\n"
    return group, name, entry


def main():
    print("Downloading IPTV metadata...", flush=True)
    channels_data = download("channels")
    feeds = download("feeds")
    streams = download("streams")
    logos_data = download("logos")

    channels = {}

    for channel in channels_data:
        if channel.get("country") not in COUNTRIES:
            continue
        if channel.get("closed") or channel.get("is_nsfw"):
            continue
        if set(channel.get("categories") or []) & BLOCKED_CATEGORIES:
            continue
        if BLOCKED_NAMES.search(channel.get("name") or ""):
            continue
        channels[channel["id"]] = channel

    english_feeds = set()
    english_channels = set()

    for feed in feeds:
        cid = feed.get("channel")
        if cid in channels and "eng" in (feed.get("languages") or []):
            english_feeds.add((cid, feed.get("id")))
            english_channels.add(cid)

    logos = {}
    for logo in logos_data:
        cid = logo.get("channel")
        if cid in channels and logo.get("url"):
            if cid not in logos or logo.get("in_use"):
                logos[cid] = logo["url"]

    candidates = defaultdict(list)
    seen = set()
    excluded_duplicates = 0
    excluded_hosts = 0

    for stream in streams:
        cid = stream.get("channel")
        url = (stream.get("url") or "").strip()

        if cid not in channels or not url:
            continue

        feed_id = stream.get("feed")
        if feed_id is not None:
            if (cid, feed_id) not in english_feeds:
                continue
        elif cid not in english_channels:
            continue

        if BLOCKED_NAMES.search(stream.get("title") or ""):
            continue

        hostname = (urlsplit(url).hostname or "").lower()
        if hostname in BLOCKED_HOSTS:
            excluded_hosts += 1
            continue

        key = (cid, url)
        if key in seen:
            excluded_duplicates += 1
            continue
        seen.add(key)

        item = dict(stream)
        item["url"] = url
        candidates[cid].append(item)

    # Test streams concurrently, but never test unlimited
    # alternatives for one channel.
    tasks = []
    for cid, options in candidates.items():
        options.sort(key=rank, reverse=True)
        for stream in options[:MAX_TESTS_PER_CHANNEL]:
            tasks.append((cid, stream))

    print(
        f"Checking {len(tasks)} candidate streams "
        f"for {len(candidates)} channels...",
        flush=True,
    )

    passed = defaultdict(list)
    tested = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:
        futures = {
            executor.submit(probe, stream): (cid, stream)
            for cid, stream in tasks
        }

        for future in concurrent.futures.as_completed(futures):
            cid, stream = futures[future]
            tested += 1

            try:
                valid = future.result()
            except Exception:
                valid = False

            if valid:
                passed[cid].append(stream)
            else:
                failed += 1

            if tested % 100 == 0:
                print(
                    f"Checked {tested}/{len(tasks)} | "
                    f"Passed {tested - failed} | Failed {failed}",
                    flush=True,
                )

    grouped = defaultdict(list)

    for cid, valid_streams in passed.items():
        # Keep only the highest-ranked stream that passed.
        best = max(valid_streams, key=rank)
        group, name, entry = make_entry(
            channels[cid],
            best,
            logos.get(cid, ""),
        )
        grouped[group].append((name.casefold(), entry))

    total = sum(len(entries) for entries in grouped.values())

    report = {
        "candidate_channels": len(candidates),
        "tested_streams": tested,
        "passed_streams": tested - failed,
        "failed_streams": failed,
        "published_channels": total,
        "duplicate_urls_removed": excluded_duplicates,
        "known_restricted_urls_excluded": excluded_hosts,
        "groups": {
            group: len(entries)
            for group, entries in sorted(grouped.items())
        },
    }

    REPORT.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report, indent=2), flush=True)

    if total == 0:
        raise RuntimeError(
            "Validation produced no working channels. "
            "Refusing to publish an empty playlist."
        )

    country_order = list(COUNTRIES.values())

    def group_key(group):
        country, category = group.split(" | ", 1)
        return (country_order.index(country), category)

    lines = ["#EXTM3U\n"]

    for group in sorted(grouped, key=group_key):
        for _, entry in sorted(grouped[group]):
            lines.append(entry)

    OUTPUT.write_text(
        "".join(lines),
        encoding="utf-8",
    )

    print(f"Published {total} validated channels.")


if __name__ == "__main__":
    main()
