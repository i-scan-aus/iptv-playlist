
import gzip
import json
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

PLAYLIST = Path("playlist.m3u")
OUTPUT_XML = Path("epg.xml")
OUTPUT_GZ = Path("epg.xml.gz")
REPORT = Path("epg-report.json")

EPG_SOURCES = [
    ("IPTV-EPG Australia", "Australia", "https://iptv-epg.org/files/epg-au.xml.gz"),
    # Change Melbourne.xml to another listed XMLTV.net regional file if needed.
    ("XMLTV.net Melbourne", "Australia", "http://xmltv.net/xml_files/Melbourne.xml"),
    ("IPTV-EPG UK", "UK", "https://iptv-epg.org/files/epg-gb.xml.gz"),
    ("IPTV-EPG USA", "USA", "https://iptv-epg.org/files/epg-us.xml.gz"),
]

EPG_URL = (
    "https://i-scan-aus.github.io/"
    "iptv-playlist/epg.xml.gz"
)

MAX_SOURCE_BYTES = 1500 * 1024 * 1024

def playlist_channels():
    if not PLAYLIST.exists():
        raise FileNotFoundError(
            "playlist.m3u must be generated first"
        )

    text = PLAYLIST.read_text(
        encoding="utf-8-sig"
    )

    channels = {}

    for line in text.splitlines():
        if not line.startswith("#EXTINF:"):
            continue

        id_match = re.search(
            r'tvg-id="([^"]+)"', line
        )
        country_match = re.search(
            r'tvg-country="([^"]+)"', line
        )
        group_match = re.search(
            r'group-title="([^"]+)"', line
        )

        if not id_match:
            continue

        channel_id = id_match.group(1)
        group = (
            group_match.group(1)
            if group_match else ""
        )

        # New playlists sort by category only, so country is carried as an
        # explicit M3U attribute. Retain the legacy group-title fallback.
        country_code = (
            country_match.group(1)
            if country_match else ""
        )
        country = {
            "AU": "Australia",
            "UK": "UK",
            "US": "USA",
        }.get(
            country_code,
            group.rsplit(" | ", 1)[-1],
        )

        channels[channel_id] = {
            "country": country,
            "name": line.rsplit(",", 1)[-1].strip(),
        }

    return channels


def normalise_name(value):
    """Match display names across EPG providers conservatively."""
    value = re.sub(r"\([^)]*\)", "", value or "").casefold()
    value = re.sub(r"\b(1080p|720p|hd|sd|uhd|fhd)\b", "", value)
    # Freeview uses numeric network names while IPTV metadata often spells
    # them out. This is deliberately limited to the three FTA brands.
    value = re.sub(r"\bseven\b", "7", value)
    value = re.sub(r"\bnine\b", "9", value)
    value = re.sub(r"\bten\b", "10", value)
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def source_channel_map(channels, country):
    """Return unambiguous display-name-to-playlist-ID mappings."""
    names = {}

    for channel_id, details in channels.items():
        if details["country"] != country:
            continue

        key = normalise_name(details["name"])

        if key:
            names.setdefault(key, []).append(channel_id)

    return {
        key: ids[0]
        for key, ids in names.items()
        if len(ids) == 1
    }


def download_guide(url, destination):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "IPTVPlaylistEPG/1.0"
        },
    )

    with urllib.request.urlopen(
        request, timeout=180
    ) as response, open(
        destination, "wb"
    ) as output:
        shutil.copyfileobj(
            response, output,
            length=1024 * 1024
        )

    if destination.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(
            "EPG source exceeds size limit"
        )


def read_guide(path):
    if path.read_bytes()[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")

    return open(path, "rb")


def build():
    channels = playlist_channels()

    if not channels:
        raise RuntimeError(
            "No tvg-id values found in playlist"
        )

    print(
        f"Playlist channels: {len(channels)}",
        flush=True
    )

    root = ET.Element("tv", {
        "generator-info-name":
            "i-scan-aus IPTV EPG Builder",
    })

    matched_channels = set()
    emitted_channels = set()
    programme_counts = Counter()
    seen_programmes = set()
    sources_ok = []
    sources_failed = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        for source_name, country, url in EPG_SOURCES:
            wanted = {
                cid for cid, details in channels.items()
                if details["country"] == country
            }
            names = source_channel_map(channels, country)

            if not wanted:
                continue

            print(
                f"Downloading {source_name}...",
                flush=True
            )

            source_file = tmp / (
                re.sub(r"[^a-z0-9]+", "-", source_name.casefold())
                + ".xml.gz"
            )

            try:
                download_guide(url, source_file)

                with read_guide(source_file) as stream:
                    source_ids = {}
                    context = ET.iterparse(
                        stream,
                        events=("start", "end")
                    )

                    _, source_root = next(context)

                    for event, element in context:
                        if event != "end":
                            continue

                        if element.tag == "channel":
                            cid = element.get("id")
                            display_names = [
                                normalise_name(name.text)
                                for name in element.findall("display-name")
                                if name.text
                            ]
                            target_id = (
                                cid if cid in wanted else next(
                                    (names[name] for name in display_names if name in names),
                                    None,
                                )
                            )

                            if target_id:
                                source_ids[cid] = target_id
                                matched_channels.add(target_id)

                                if target_id not in emitted_channels:
                                    copy = ET.fromstring(ET.tostring(element))
                                    copy.set("id", target_id)
                                    root.append(copy)
                                    emitted_channels.add(target_id)

                            element.clear()

                        elif element.tag == "programme":
                            cid = element.get("channel")
                            target_id = source_ids.get(cid)

                            if target_id:
                                key = (
                                    target_id,
                                    element.get("start"),
                                    element.get("stop"),
                                )

                                if key not in seen_programmes:
                                    copy = ET.fromstring(ET.tostring(element))
                                    copy.set("channel", target_id)
                                    root.append(copy)
                                    seen_programmes.add(key)
                                    programme_counts[target_id] += 1

                            element.clear()

                        # Release parsed channel/programme
                        # elements from the source tree.
                        if element.tag in ("channel", "programme"):
                            source_root.clear()

                sources_ok.append(source_name)

            except Exception as exc:
                sources_failed[source_name] = str(exc)
                print(
                    f"EPG source failed ({source_name}): "
                    f"{exc}",
                    flush=True
                )

    if not matched_channels or not programme_counts:
        raise RuntimeError(
            "No matching programme data found. "
            "Refusing to publish an empty EPG."
        )

    # Remove channel definitions without programmes.
    for element in list(root):
        if (
            element.tag == "channel"
            and element.get("id") not in programme_counts
        ):
            root.remove(element)

    tree = ET.ElementTree(root)
    tree.write(
        OUTPUT_XML,
        encoding="utf-8",
        xml_declaration=True,
    )

    with open(OUTPUT_XML, "rb") as source:
        with gzip.open(
            OUTPUT_GZ, "wb", compresslevel=6
        ) as output:
            shutil.copyfileobj(source, output)

    report = {
        "playlist_channels": len(channels),
        "epg_channels": len(programme_counts),
        "epg_programmes": sum(
            programme_counts.values()
        ),
        "channels_without_epg":
            len(set(channels) - set(programme_counts)),
        "sources_succeeded": sources_ok,
        "sources_failed": sources_failed,
        "epg_url": EPG_URL,
        "epg_channels_by_country": {
            country: sum(
                1 for cid in programme_counts
                if channels[cid]["country"] == country
            )
            for country in ("Australia", "UK", "USA")
        },
    }

    REPORT.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(report, indent=2),
        flush=True
    )


if __name__ == "__main__":
    build()
