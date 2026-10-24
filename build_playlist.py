
import json
import re
import urllib.request
from pathlib import Path
from collections import defaultdict

# Configuration
BASE_URL = "https://iptv-org.github.io/api/"
OUTPUT_FILE = "playlist.m3u"

COUNTRIES = {
    "AU": "Australia",
    "UK": "UK",
    "US": "USA",
}

EXCLUDED_CATEGORIES = {
    "religious",
    "shopping",
    "shop",
    "teleshopping",
}

EXCLUDED_NAMES = re.compile(
    r"\b("
    r"church|ministr(?:y|ies)|worship|gospel|"
    r"christian|islamic|quran|bible|"
    r"god tv|jesus|shopping|shop tv|"
    r"shop hq|qvc|hsn|jewelry television"
    r")\b",
    re.IGNORECASE,
)


def load_json(name):
    """Download current IPTV-org API data."""
    url = BASE_URL + name + ".json"
    print(f"Downloading {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 IPTVPlaylistBuilder/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def clean_text(value):
    """Remove characters that break M3U metadata."""
    return str(value or "").replace('"', "'").replace("\n", " ").strip()


def main():
    print("Starting IPTV playlist generation...")

    channels_data = load_json("channels")
    feeds_data = load_json("feeds")
    streams_data = load_json("streams")
    logos_data = load_json("logos")

    # Index active channels in selected countries
    channels = {}

    for channel in channels_data:
        if channel.get("country") not in COUNTRIES:
            continue

        if channel.get("closed"):
            continue

        if channel.get("is_nsfw"):
            continue

        categories = set(channel.get("categories") or [])

        if categories.intersection(EXCLUDED_CATEGORIES):
            continue

        name = channel.get("name") or ""

        if EXCLUDED_NAMES.search(name):
            continue

        channels[channel["id"]] = channel

    print(f"Eligible country/category channels: {len(channels)}")

    # Index English-language feeds
    english_feeds = set()
    channel_has_english = set()

    for feed in feeds_data:
        channel_id = feed.get("channel")

        if channel_id not in channels:
            continue

        languages = feed.get("languages") or []

        if "eng" in languages:
            english_feeds.add(
                (channel_id, feed.get("id"))
            )
            channel_has_english.add(channel_id)

    print(f"Channels with English feeds: {len(channel_has_english)}")

    # Find logos
    logos = {}

    for logo in logos_data:
        channel_id = logo.get("channel")
        url = logo.get("url")

        if channel_id not in channels or not url:
            continue

        if channel_id not in logos or logo.get("in_use"):
            logos[channel_id] = url

    # Build country groups
    grouped = defaultdict(list)
    seen = set()

    for stream in streams_data:
        channel_id = stream.get("channel")
        url = stream.get("url")

        if channel_id not in channels or not url:
            continue

        feed_id = stream.get("feed")

        # Require explicitly English-language metadata.
        # For streams without a feed ID, accept only if
        # the channel has at least one English feed.
        if feed_id is not None:
            if (channel_id, feed_id) not in english_feeds:
                continue
        elif channel_id not in channel_has_english:
            continue

        # Remove duplicate channel/URL combinations
        key = (channel_id, url)

        if key in seen:
            continue

        seen.add(key)

        channel = channels[channel_id]
        country = channel["country"]

        name = clean_text(channel.get("name"))
        logo = clean_text(logos.get(channel_id, ""))
        tvg_id = clean_text(channel_id)

        # Extra filtering of stream titles
        stream_title = stream.get("title") or ""

        if EXCLUDED_NAMES.search(stream_title):
            continue

        # Keep compatibility headers where supplied
        extra = ""

        if stream.get("user_agent"):
            extra += (
                "#EXTVLCOPT:http-user-agent="
                + str(stream["user_agent"])
                + "\n"
            )

        if stream.get("referrer"):
            extra += (
                "#EXTVLCOPT:http-referrer="
                + str(stream["referrer"])
                + "\n"
            )

        entry = (
            f'#EXTINF:-1 tvg-id="{tvg_id}" '
            f'tvg-logo="{logo}" '
            f'group-title="{COUNTRIES[country]}",'
            f'{name}\n'
            f'{extra}'
            f'{url}\n'
        )

        grouped[country].append((name.lower(), entry))

    # Generate final playlist
    lines = ["#EXTM3U\n"]

    total = 0

    for country in COUNTRIES:
        entries = sorted(grouped[country], key=lambda x: x[0])

        print(
            f"{COUNTRIES[country]}: "
            f"{len(entries)} streams"
        )

        for _, entry in entries:
            lines.append(entry)
            total += 1

    if total == 0:
        raise RuntimeError(
            "No IPTV streams matched the filters. "
            "Playlist will not be published."
        )

    Path(OUTPUT_FILE).write_text(
        "".join(lines),
        encoding="utf-8",
    )

    print("--------------------------------")
    print("Playlist generated successfully!")
    print(f"Total streams: {total}")
    print(f"Output file: {OUTPUT_FILE}")
    print("--------------------------------")


if __name__ == "__main__":
    main()
