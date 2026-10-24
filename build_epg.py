
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

EPG_SOURCES = {
    "Australia": "https://iptv-epg.org/files/epg-au.xml.gz",
    "UK": "https://iptv-epg.org/files/epg-gb.xml.gz",
    "USA": "https://iptv-epg.org/files/epg-us.xml.gz",
}

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

        country = group.rsplit(
            " | ", 1
        )[-1]

        channels[channel_id] = country

    return channels


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
    programme_counts = Counter()
    seen_programmes = set()
    sources_ok = []
    sources_failed = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        for country, url in EPG_SOURCES.items():
            wanted = {
                cid for cid, c in channels.items()
                if c == country
            }

            if not wanted:
                continue

            print(
                f"Downloading {country} EPG...",
                flush=True
            )

            source_file = tmp / (
                country + ".xml.gz"
            )

            try:
                download_guide(url, source_file)

                with read_guide(source_file) as stream:
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

                            if cid in wanted:
                                root.append(
                                    ET.fromstring(
                                        ET.tostring(element)
                                    )
                                )
                                matched_channels.add(cid)

                            element.clear()

                        elif element.tag == "programme":
                            cid = element.get("channel")

                            if cid in wanted:
                                key = (
                                    cid,
                                    element.get("start"),
                                    element.get("stop"),
                                )

                                if key not in seen_programmes:
                                    root.append(
                                        ET.fromstring(
                                            ET.tostring(element)
                                        )
                                    )
                                    seen_programmes.add(key)
                                    programme_counts[cid] += 1

                            element.clear()

                        # Free completed elements to avoid
                        # retaining the entire source guide.
                        if element is not source_root:
                            continue

                sources_ok.append(country)

            except Exception as exc:
                sources_failed[country] = str(exc)
                print(
                    f"EPG source failed ({country}): "
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
                if channels.get(cid) == country
            )
            for country in EPG_SOURCES
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
