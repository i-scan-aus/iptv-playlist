
import concurrent.futures
import json
import re
import subprocess
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://iptv-org.github.io/api/"

OUTPUT_FILE = Path("playlist.m3u")
REPORT_FILE = Path("validation-report.json")

COUNTRIES = {
    "AU": "Australia",
    "UK": "UK",
    "US": "USA",
}

# UK and USA only. Australia skips validation.
WORKERS = 10
TIMEOUT = 12
MAX_TESTS_PER_CHANNEL = 5

EXCLUDED_CATEGORIES = {
    "religious",
    "shopping",
}

EXCLUDED_NAMES = re.compile(
    r"\b("
    r"church|gospel|christian|islamic|quran|"
    r"bible|worship|shopping|qvc|hsn|"
    r"teleshopping|jewelry television"
    r")\b",
    re.IGNORECASE,
)

# Known UK-restricted BBC delivery hosts.
# Applied to UK/USA only, not Australian channels.
BLOCKED_HOSTS = {
    "vs-cmaf-pushb-uk-live.akamaized.net",
    "vs-hls-pushb-uk-live.akamaized.net",
    "vs-cmaf-pushb-uk.live.fastly.md.bbc.co.uk",
    "vs-hls-pushb-uk.live.fastly.md.bbc.co.uk",
}

CATEGORY_NAMES = {
    "animation": "Kids",
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
    "sports",
    "news",
    "movies",
    "documentary",
    "kids",
    "animation",
    "series",
    "comedy",
    "music",
    "science",
    "education",
    "cooking",
    "travel",
    "business",
    "weather",
    "entertainment",
    "family",
    "lifestyle",
    "general",
]

# ============================================================
# HELPERS
# ============================================================

def download_json(name):
    url = BASE_URL + name + ".json"
    print(f"Downloading {url}", flush=True)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "IPTVPlaylistBuilder/4.0"
        },
    )

    with urllib.request.urlopen(
        request, timeout=120
    ) as response:
        return json.load(response)


def clean(value):
    return (
        str(value or "")
        .replace('"', "'")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def get_category(channel):
    categories = channel.get("categories") or []

    for category in CATEGORY_PRIORITY:
        if category in categories:
            return CATEGORY_NAMES.get(
                category, category.title()
            )

    for category in categories:
        if category in CATEGORY_NAMES:
            return CATEGORY_NAMES[category]

    return "General"


def stream_rank(stream):
    url = stream["url"].lower()
    score = 0

    # Prefer HLS for IPTV player compatibility.
    if ".m3u8" in url:
        score += 100
    elif ".mpd" in url:
        score += 20

    if url.startswith("https://"):
        score += 10

    # Prefer streams that don't need custom headers.
    if not stream.get("user_agent"):
        score += 5

    if not stream.get("referrer"):
        score += 5

    height = stream.get("height")

    if isinstance(height, (int, float)):
        score += min(height, 2160) / 100

    return score


# ============================================================
# STREAM VALIDATION
# ============================================================

def validate_stream(stream):
    """Validate video playback using FFprobe."""

    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name",
        "-of", "json",
        "-analyzeduration", "3000000",
        "-probesize", "3000000",
    ]

    if stream.get("user_agent"):
        command += [
            "-user_agent",
            str(stream["user_agent"]),
        ]

    if stream.get("referrer"):
        command += [
            "-headers",
            "Referer: "
            + str(stream["referrer"])
            + "\r\n",
        ]

    command.append(stream["url"])

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

        return any(
            item.get("codec_type") == "video"
            and item.get("codec_name")
            not in (None, "unknown")
            for item in data.get("streams", [])
        )

    except (
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
    ):
        return False


# ============================================================
# PLAYLIST ENTRY
# ============================================================

def make_entry(channel, stream, logo):
    name = clean(channel.get("name"))
    channel_id = clean(channel["id"])

    country = COUNTRIES[channel["country"]]
    category = get_category(channel)

    # CATEGORY FIRST, COUNTRY SECOND
    group = f"{category} | {country}"

    entry = (
        f'#EXTINF:-1 tvg-id="{channel_id}" '
        f'tvg-logo="{clean(logo)}" '
        f'group-title="{group}",{name}\n'
    )

    if stream.get("user_agent"):
        entry += (
            "#EXTVLCOPT:http-user-agent="
            + clean(stream["user_agent"])
            + "\n"
        )

    if stream.get("referrer"):
        entry += (
            "#EXTVLCOPT:http-referrer="
            + clean(stream["referrer"])
            + "\n"
        )

    entry += stream["url"] + "\n"

    return group, name, entry


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "Starting IPTV playlist generation...",
        flush=True,
    )

    channels_data = download_json("channels")
    feeds_data = download_json("feeds")
    streams_data = download_json("streams")
    logos_data = download_json("logos")

    # --------------------------------------------------------
    # FILTER CHANNELS
    # --------------------------------------------------------

    channels = {}

    for channel in channels_data:

        if channel.get("country") not in COUNTRIES:
            continue

        if channel.get("closed"):
            continue

        if channel.get("is_nsfw"):
            continue

        categories = set(
            channel.get("categories") or []
        )

        if categories & EXCLUDED_CATEGORIES:
            continue

        if EXCLUDED_NAMES.search(
            channel.get("name") or ""
        ):
            continue

        channels[channel["id"]] = channel

    print(
        f"Eligible channels: {len(channels)}",
        flush=True,
    )

    # --------------------------------------------------------
    # ENGLISH LANGUAGE FILTER
    # --------------------------------------------------------

    english_feeds = set()
    english_channels = set()

    for feed in feeds_data:

        channel_id = feed.get("channel")

        if channel_id not in channels:
            continue

        languages = feed.get("languages") or []

        if "eng" in languages:
            english_feeds.add(
                (channel_id, feed.get("id"))
            )
            english_channels.add(channel_id)

    # --------------------------------------------------------
    # CHANNEL LOGOS
    # --------------------------------------------------------

    logos = {}

    for logo in logos_data:

        channel_id = logo.get("channel")

        if channel_id not in channels:
            continue

        if not logo.get("url"):
            continue

        if (
            channel_id not in logos
            or logo.get("in_use")
        ):
            logos[channel_id] = logo["url"]

    # --------------------------------------------------------
    # COLLECT CANDIDATE STREAMS
    # --------------------------------------------------------

    candidates = defaultdict(list)
    seen = set()

    duplicate_urls = 0
    blocked_urls = 0

    for stream in streams_data:

        channel_id = stream.get("channel")
        url = (stream.get("url") or "").strip()

        if channel_id not in channels:
            continue

        if not url:
            continue

        feed_id = stream.get("feed")

        if feed_id is not None:

            if (
                channel_id,
                feed_id,
            ) not in english_feeds:
                continue

        elif channel_id not in english_channels:
            continue

        if EXCLUDED_NAMES.search(
            stream.get("title") or ""
        ):
            continue

        country = channels[channel_id]["country"]

        # Do not block Australian streams.
        if country != "AU":
            hostname = (
                urlsplit(url).hostname or ""
            ).lower()

            if hostname in BLOCKED_HOSTS:
                blocked_urls += 1
                continue

        # Remove identical channel + URL combinations.
        key = (channel_id, url)

        if key in seen:
            duplicate_urls += 1
            continue

        seen.add(key)

        item = dict(stream)
        item["url"] = url

        candidates[channel_id].append(item)

    # --------------------------------------------------------
    # AUSTRALIA: SKIP VALIDATION
    # --------------------------------------------------------

    australian_channels = {}

    # UK/USA: VALIDATE
    validation_tasks = []

    for channel_id, options in candidates.items():

        options.sort(
            key=stream_rank,
            reverse=True,
        )

        country = channels[channel_id]["country"]

        if country == "AU":

            # Keep one preferred URL per Australian channel.
            australian_channels[channel_id] = options[0]

        else:

            for stream in options[
                :MAX_TESTS_PER_CHANNEL
            ]:
                validation_tasks.append(
                    (channel_id, stream)
                )

    print(
        f"Australian channels (no validation): "
        f"{len(australian_channels)}",
        flush=True,
    )

    print(
        f"UK/USA streams to validate: "
        f"{len(validation_tasks)}",
        flush=True,
    )

    # --------------------------------------------------------
    # RUN FFPROBE VALIDATION
    # --------------------------------------------------------

    passed = defaultdict(list)

    tested = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                validate_stream, stream
            ): (channel_id, stream)
            for channel_id, stream in validation_tasks
        }

        for future in concurrent.futures.as_completed(
            futures
        ):

            channel_id, stream = futures[future]

            tested += 1

            try:
                valid = future.result()
            except Exception:
                valid = False

            if valid:
                passed[channel_id].append(stream)
            else:
                failed += 1

            if tested % 100 == 0:

                print(
                    f"Checked {tested}/"
                    f"{len(validation_tasks)} | "
                    f"Passed {tested - failed} | "
                    f"Failed {failed}",
                    flush=True,
                )

    # --------------------------------------------------------
    # GENERATE PLAYLIST
    # --------------------------------------------------------

    grouped = defaultdict(list)

    # Add Australian channels without validation.
    for channel_id, stream in australian_channels.items():

        group, name, entry = make_entry(
            channels[channel_id],
            stream,
            logos.get(channel_id, ""),
        )

        grouped[group].append(
            (name.casefold(), entry)
        )

    # Add validated UK/USA channels.
    for channel_id, valid_streams in passed.items():

        best_stream = max(
            valid_streams,
            key=stream_rank,
        )

        group, name, entry = make_entry(
            channels[channel_id],
            best_stream,
            logos.get(channel_id, ""),
        )

        grouped[group].append(
            (name.casefold(), entry)
        )

    total_channels = sum(
        len(entries)
        for entries in grouped.values()
    )

    # --------------------------------------------------------
    # VALIDATION REPORT
    # --------------------------------------------------------

    report = {
        "candidate_channels": len(candidates),
        "australian_channels_unvalidated":
            len(australian_channels),
        "tested_streams": tested,
        "passed_streams": tested - failed,
        "failed_streams": failed,
        "published_channels": total_channels,
        "duplicate_urls_removed": duplicate_urls,
        "known_restricted_urls_excluded": blocked_urls,
        "groups": {
            group: len(entries)
            for group, entries in sorted(
                grouped.items()
            )
        },
    }

    REPORT_FILE.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(report, indent=2),
        flush=True,
    )

    if total_channels == 0:
        raise RuntimeError(
            "No channels available. "
            "Refusing to publish an empty playlist."
        )

    # --------------------------------------------------------
    # SORT BY CATEGORY FIRST
    # --------------------------------------------------------

    country_order = list(
        COUNTRIES.values()
    )

    def group_sort(group):
        category, country = group.split(
            " | ", 1
        )

        return (
            category.casefold(),
            country_order.index(country),
        )

    lines = ["#EXTM3U\n"]

    for group in sorted(
        grouped,
        key=group_sort,
    ):

        for _, entry in sorted(
            grouped[group]
        ):
            lines.append(entry)

    OUTPUT_FILE.write_text(
        "".join(lines),
        encoding="utf-8",
    )

    print("--------------------------------")
    print("Playlist generation complete!")
    print(f"Australian channels: {len(australian_channels)}")
    print(f"UK/USA validated: {len(passed)}")
    print(f"Total published: {total_channels}")
    print(f"Categories: {len(grouped)}")
    print("--------------------------------")


if __name__ == "__main__":
    main()
