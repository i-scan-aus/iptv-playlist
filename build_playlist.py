
import json
import re
import urllib.request
from pathlib import Path
from collections import defaultdict

BASE_URL = "https://iptv-org.github.io/api/"
OUTPUT_FILE = "playlist.m3u"

COUNTRIES = {
    "AU": "Australia",
    "UK": "UK",
    "US": "USA",
}

EXCLUDED_CATEGORIES = {
    "religious", "shopping", "shop", "teleshopping"
}

EXCLUDED_NAMES = re.compile(
    r"\b(church|ministr(?:y|ies)|worship|gospel|"
    r"christian|islamic|quran|bible|god tv|jesus|"
    r"shopping|shop tv|shop hq|qvc|hsn)\b",
    re.IGNORECASE,
)

CATEGORY_NAMES = {
    "animation": "Animation",
    "auto": "Automotive",
    "business": "Business",
    "classic": "Classic TV",
    "comedy": "Comedy",
    "cooking": "Food & Cooking",
    "culture": "Culture",
    "documentary": "Documentaries",
    "education": "Education",
    "entertainment": "Entertainment",
    "family": "Family",
    "general": "General",
    "kids": "Kids",
    "legislative": "Government",
    "lifestyle": "Lifestyle",
    "movies": "Movies",
    "music": "Music",
    "news": "News",
    "outdoor": "Outdoors",
    "relax": "Relaxation",
    "science": "Science",
    "series": "TV Series",
    "sports": "Sports",
    "travel": "Travel",
    "weather": "Weather",
}

CATEGORY_PRIORITY = [
    "sports", "news", "movies", "documentary",
    "kids", "animation", "series", "comedy",
    "music", "science", "education", "cooking",
    "travel", "business", "entertainment",
    "family", "lifestyle", "general",
]


def load_json(name):
    url = BASE_URL + name + ".json"
    print("Downloading:", url)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "IPTVPlaylistBuilder/2.0"},
    )

    with urllib.request.urlopen(request, timeout=120) as r:
        return json.load(r)


def clean(value):
    return (
        str(value or "")
        .replace('"', "'")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def get_category(channel):
    categories = channel.get("categories") or []

    for category in CATEGORY_PRIORITY:
        if category in categories:
            return CATEGORY_NAMES.get(category, category.title())

    if categories:
        return CATEGORY_NAMES.get(
            categories[0], categories[0].title()
        )

    return "General"


def main():
    print("Building IPTV playlist...")

    channels_data = load_json("channels")
    feeds_data = load_json("feeds")
    streams_data = load_json("streams")
    logos_data = load_json("logos")

    channels = {}

    for channel in channels_data:
        if channel.get("country") not in COUNTRIES:
            continue

        if channel.get("closed") or channel.get("is_nsfw"):
            continue

        categories = set(channel.get("categories") or [])

        if categories & EXCLUDED_CATEGORIES:
            continue

        if EXCLUDED_NAMES.search(channel.get("name") or ""):
            continue

        channels[channel["id"]] = channel

    english_feeds = set()
    english_channels = set()

    for feed in feeds_data:
        cid = feed.get("channel")

        if cid not in channels:
            continue

        if "eng" in (feed.get("languages") or []):
            english_feeds.add((cid, feed.get("id")))
            english_channels.add(cid)

    logos = {}

    for logo in logos_data:
        cid = logo.get("channel")

        if cid in channels and logo.get("url"):
            if cid not in logos or logo.get("in_use"):
                logos[cid] = logo["url"]

    grouped = defaultdict(list)
    seen = set()
    duplicates = 0

    for stream in streams_data:
        cid = stream.get("channel")
        url = stream.get("url")

        if cid not in channels or not url:
            continue

        feed_id = stream.get("feed")

        if feed_id is not None:
            if (cid, feed_id) not in english_feeds:
                continue
        elif cid not in english_channels:
            continue

        if EXCLUDED_NAMES.search(stream.get("title") or ""):
            continue

        # Remove exact channel/stream duplicates.
        key = (cid, url.strip())

        if key in seen:
            duplicates += 1
            continue

        seen.add(key)

        channel = channels[cid]
        country = COUNTRIES[channel["country"]]
        category = get_category(channel)
        group = f"{country} | {category}"

        name = clean(channel.get("name"))
        logo = clean(logos.get(cid, ""))
        tvg_id = clean(cid)

        extra = ""

        if stream.get("user_agent"):
            extra += (
                "#EXTVLCOPT:http-user-agent="
                + str(stream["user_agent"]) + "\n"
            )

        if stream.get("referrer"):
            extra += (
                "#EXTVLCOPT:http-referrer="
                + str(stream["referrer"]) + "\n"
            )

        entry = (
            f'#EXTINF:-1 tvg-id="{tvg_id}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group}",{name}\n'
            f'{extra}{url}\n'
        )

        grouped[group].append((name.casefold(), entry))

    if not grouped:
        raise RuntimeError("No streams matched the filters.")

    lines = ["#EXTM3U\n"]
    total = 0

    country_order = list(COUNTRIES.values())

    def group_sort(group):
        country, category = group.split(" | ", 1)
        return (country_order.index(country), category.casefold())

    for group in sorted(grouped, key=group_sort):
        entries = sorted(grouped[group])

        print(f"{group}: {len(entries)} streams")

        for _, entry in entries:
            lines.append(entry)
            total += 1

    Path(OUTPUT_FILE).write_text(
        "".join(lines),
        encoding="utf-8",
    )

    print(f"Total streams: {total}")
    print(f"Exact duplicates removed: {duplicates}")
    print(f"Groups created: {len(grouped)}")
    print(f"Saved: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
