import concurrent.futures
import hashlib
import json
import re
import subprocess
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# IPTV-org supplies metadata for AU/UK/US. The two extra lists add free AU TV;
# they do not provide, or attempt to provide, subscription Foxtel/Kayo streams.
BASE_URL = "https://iptv-org.github.io/api/"
AUSTRALIAN_PLAYLISTS = {
    "Free-TV Australia": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_australia.m3u8",
    # The complete Free-TV list may also contain eligible UK and US entries.
    # Entries without a recognised country tag/ID are discarded.
    "Free-TV AU/UK/US": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8",
    "Streamline IPTV Australia": "https://cdn.jsdelivr.net/gh/streamline-iptv/iptv-playlists@dist/countries/au.m3u",
    # Filtered to Australia by parse_m3u(), so this widens the AU pool without
    # changing the playlist's AU/UK/US scope.
    "IPTV-org Oceania": "https://iptv-org.github.io/iptv/regions/oce.m3u",
}
OUTPUT_FILE, REPORT_FILE = Path("playlist.m3u"), Path("validation-report.json")
COUNTRIES = {"AU": "Australia", "UK": "UK", "US": "USA"}
WORKERS, TIMEOUT, MAX_TESTS_PER_CHANNEL = 10, 12, 5
EXCLUDED_CATEGORIES = {"religious", "shopping"}
EXCLUDED_NAMES = re.compile(r"\b(church|gospel|christian|islamic|quran|bible|worship|shopping|qvc|hsn|teleshopping|jewelry television|adult|xxx|porn)\b", re.I)
BLOCKED_HOSTS = {"vs-cmaf-pushb-uk-live.akamaized.net", "vs-hls-pushb-uk-live.akamaized.net", "vs-cmaf-pushb-uk.live.fastly.md.bbc.co.uk", "vs-hls-pushb-uk.live.fastly.md.bbc.co.uk"}
CATEGORY_NAMES = {"animation":"Kids", "auto":"Automotive", "business":"Business", "classic":"Classic TV", "comedy":"Comedy", "cooking":"Food & Cooking", "culture":"Culture", "documentary":"Documentaries", "education":"Education", "entertainment":"Entertainment", "family":"Family", "general":"General", "kids":"Kids", "legislative":"Government", "lifestyle":"Lifestyle", "movies":"Movies", "music":"Music", "news":"News", "outdoor":"Outdoors", "relax":"Relaxation", "science":"Science", "series":"TV Series", "sports":"Sports", "travel":"Travel", "weather":"Weather"}
CATEGORY_PRIORITY = ["sports", "news", "movies", "documentary", "kids", "animation", "series", "comedy", "music", "science", "education", "cooking", "travel", "business", "weather", "entertainment", "family", "lifestyle", "general"]

def download(url, as_json=False):
    print(f"Downloading {url}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent":"IPTVPlaylistBuilder/5.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response) if as_json else response.read().decode("utf-8-sig", "replace")

def clean(value):
    return str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ").strip()

def normalise_name(name):
    return re.sub(r"[^a-z0-9]+", "", clean(name).casefold())

def canonical_url(url):
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), parts.path.rstrip("/") or "/", parts.query, ""))

def attrs(line):
    return {key.lower(): value for key, value in re.findall(r'([\w-]+)="([^"]*)"', line)}

def external_category(name, group):
    text = f"{name} {group}".casefold()
    if any(word in text for word in ("sport", "racing", "cricket", "afl", "nrl")): return "Sports"
    if "news" in text: return "News"
    if any(word in text for word in ("kids", "cartoon", "nick", "abc me")): return "Kids"
    if any(word in text for word in ("movie", "film", "cinema")): return "Movies"
    return "General"

def playlist_country(meta, name, default_country="AU"):
    """Return an allowed country from M3U metadata or a standard tvg-id."""
    code = meta.get("tvg-country", "").upper()
    code = {"GB": "UK"}.get(code, code)

    if code in COUNTRIES:
        return code

    match = re.search(r"\.(au|uk|gb|us)(?:@|$)", meta.get("tvg-id", ""), re.I)

    if match:
        return {"GB": "UK"}.get(match.group(1).upper(), match.group(1).upper())

    return default_country if default_country in COUNTRIES else None


def parse_m3u(text, source, default_country="AU"):
    entries, pending, options = [], None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            pending, options = {"name":line.rsplit(",", 1)[-1].strip(), "attrs":attrs(line)}, {}
        elif pending and line.startswith("#EXTVLCOPT:http-user-agent="):
            options["user_agent"] = line.split("=", 1)[1].strip()
        elif pending and line.startswith("#EXTVLCOPT:http-referrer="):
            options["referrer"] = line.split("=", 1)[1].strip()
        elif pending and line and not line.startswith("#"):
            name, meta = pending["name"], pending["attrs"]
            language = meta.get("tvg-language", "").casefold()
            country = playlist_country(meta, name, default_country)
            if country and (not language or language in {"eng", "en", "english"}) and not EXCLUDED_NAMES.search(name):
                channel_id = meta.get("tvg-id") or "ext-" + hashlib.sha1(f"{source}:{country}:{normalise_name(name)}".encode()).hexdigest()[:12]
                entries.append({"id":channel_id, "name":name, "country":country, "categories":[], "category":external_category(name, meta.get("group-title", "")), "logo":meta.get("tvg-logo", ""), "stream":{"url":line, **options, "source":source}})
            pending = None
    return entries

def get_category(channel):
    if channel.get("category"): return channel["category"]
    for category in CATEGORY_PRIORITY:
        if category in (channel.get("categories") or []): return CATEGORY_NAMES.get(category, category.title())
    return "General"

def stream_rank(stream):
    url, score = stream["url"].lower(), 0
    score += 100 if ".m3u8" in url else 20 if ".mpd" in url else 0
    score += 10 if url.startswith("https://") else 0
    score += 5 if not stream.get("user_agent") else 0
    score += 5 if not stream.get("referrer") else 0
    score += min(stream.get("height") or 0, 2160) / 100
    return score + (2 if stream.get("source") == "IPTV-org" else 0)

def validate_stream(stream):
    command = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", "-analyzeduration", "3000000", "-probesize", "3000000"]
    if stream.get("user_agent"): command += ["-user_agent", str(stream["user_agent"])]
    if stream.get("referrer"): command += ["-headers", "Referer: " + str(stream["referrer"]) + "\r\n"]
    command.append(stream["url"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        return any(item.get("codec_type") == "video" and item.get("codec_name") not in (None, "unknown") for item in data.get("streams", []))
    except (subprocess.TimeoutExpired, OSError, ValueError): return False

def make_entry(channel, stream, logo):
    # Groups are category-only. Keep country as metadata for the EPG builder.
    group = get_category(channel)
    entry = f'#EXTINF:-1 tvg-id="{clean(channel["id"])}" tvg-country="{channel["country"]}" tvg-logo="{clean(logo)}" group-title="{group}",{clean(channel["name"])}\n'
    if stream.get("user_agent"): entry += "#EXTVLCOPT:http-user-agent=" + clean(stream["user_agent"]) + "\n"
    if stream.get("referrer"): entry += "#EXTVLCOPT:http-referrer=" + clean(stream["referrer"]) + "\n"
    return group, clean(channel["name"]), entry + stream["url"] + "\n"

def main():
    print("Starting IPTV playlist generation...", flush=True)
    channels_data = download(BASE_URL + "channels.json", True)
    feeds_data, streams_data, logos_data = (download(BASE_URL + name + ".json", True) for name in ("feeds", "streams", "logos"))
    channels = {}
    for channel in channels_data:
        categories = set(channel.get("categories") or [])
        if channel.get("country") in COUNTRIES and not channel.get("closed") and not channel.get("is_nsfw") and not (categories & EXCLUDED_CATEGORIES) and not EXCLUDED_NAMES.search(channel.get("name") or ""):
            channels[channel["id"]] = channel
    english_feeds, english_channels = set(), set()
    for feed in feeds_data:
        cid = feed.get("channel")
        if cid in channels and "eng" in (feed.get("languages") or []): english_feeds.add((cid, feed.get("id"))); english_channels.add(cid)
    logos = {}
    for logo in logos_data:
        cid = logo.get("channel")
        if cid in channels and logo.get("url") and (cid not in logos or logo.get("in_use")): logos[cid] = logo["url"]
    candidates, seen_urls = defaultdict(list), set()
    source_counts, duplicate_urls, blocked_urls = {"IPTV-org":0}, 0, 0
    for stream in streams_data:
        cid, url = stream.get("channel"), (stream.get("url") or "").strip()
        if cid not in channels or not url: continue
        feed_id = stream.get("feed")
        if (feed_id is not None and (cid, feed_id) not in english_feeds) or (feed_id is None and cid not in english_channels) or EXCLUDED_NAMES.search(stream.get("title") or ""): continue
        if channels[cid]["country"] != "AU" and (urlsplit(url).hostname or "").lower() in BLOCKED_HOSTS: blocked_urls += 1; continue
        # A URL is only useful once, even when upstream sources assign it
        # different channel IDs.
        key = canonical_url(url)
        if key in seen_urls: duplicate_urls += 1; continue
        seen_urls.add(key); item = dict(stream); item.update(url=url, source="IPTV-org"); candidates[cid].append(item); source_counts["IPTV-org"] += 1
    for source, url in AUSTRALIAN_PLAYLISTS.items():
        # The complete Free-TV list is multinational, unlike the dedicated AU
        # lists. It must identify country per entry rather than defaulting AU.
        default_country = None if source == "Free-TV AU/UK/US" else "AU"
        try: entries = parse_m3u(download(url), source, default_country)
        except Exception as exc: print(f"Source failed ({source}): {exc}", flush=True); source_counts[source] = 0; continue
        accepted = 0
        for entry in entries:
            cid, stream = entry["id"], entry["stream"]
            channels.setdefault(cid, {key:entry[key] for key in ("id", "name", "country", "categories", "category")})
            if entry["logo"] and cid not in logos: logos[cid] = entry["logo"]
            key = canonical_url(stream["url"])
            if key in seen_urls: duplicate_urls += 1; continue
            seen_urls.add(key); candidates[cid].append(stream); accepted += 1
        source_counts[source] = accepted
    australian, seen_au_names, published_urls, validation_tasks = {}, set(), set(), []
    for cid, options in candidates.items():
        options.sort(key=stream_rank, reverse=True)
        if channels[cid]["country"] == "AU":
            stream = options[0]; name_key, url_key = normalise_name(channels[cid]["name"]), canonical_url(stream["url"])
            if name_key in seen_au_names or url_key in published_urls: duplicate_urls += 1; continue
            seen_au_names.add(name_key); published_urls.add(url_key); australian[cid] = stream
        else: validation_tasks.extend((cid, stream) for stream in options[:MAX_TESTS_PER_CHANNEL])
    print(f"Australian channels (no validation): {len(australian)}", flush=True); print(f"UK/USA streams to validate: {len(validation_tasks)}", flush=True)
    passed, tested, failed = defaultdict(list), 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(validate_stream, stream):(cid, stream) for cid, stream in validation_tasks}
        for future in concurrent.futures.as_completed(futures):
            cid, stream = futures[future]; tested += 1
            try: valid = future.result()
            except Exception: valid = False
            if valid: passed[cid].append(stream)
            else: failed += 1
    grouped = defaultdict(list)
    for cid, stream in australian.items():
        group, name, entry = make_entry(channels[cid], stream, logos.get(cid, "")); grouped[group].append((name.casefold(), entry))
    for cid, valid_streams in passed.items():
        group, name, entry = make_entry(channels[cid], max(valid_streams, key=stream_rank), logos.get(cid, "")); grouped[group].append((name.casefold(), entry))
    total = sum(len(entries) for entries in grouped.values())
    report = {"candidate_channels":len(candidates), "australian_channels_unvalidated":len(australian), "tested_streams":tested, "passed_streams":tested-failed, "failed_streams":failed, "published_channels":total, "duplicate_urls_removed":duplicate_urls, "known_restricted_urls_excluded":blocked_urls, "source_streams_accepted":source_counts, "groups":{group:len(entries) for group, entries in sorted(grouped.items())}}
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8"); print(json.dumps(report, indent=2), flush=True)
    if not total: raise RuntimeError("No channels available. Refusing to publish an empty playlist.")
    def group_sort(group): return group.casefold()
    lines = ["#EXTM3U\n"]
    for group in sorted(grouped, key=group_sort): lines.extend(entry for _, entry in sorted(grouped[group]))
    OUTPUT_FILE.write_text("".join(lines), encoding="utf-8")
    print(f"Playlist generation complete: {total} channels", flush=True)

if __name__ == "__main__": main()
