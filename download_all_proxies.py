#!/usr/bin/env python3
"""
download_all_proxies.py

Aggregates, validates, and deduplicates proxies from the top GitHub repositories
under the 'proxy-list' topic.
Outputs:
  - proxies.json (structured JSON with metadata, protocol breakdown, and deduplicated entries)
  - proxies.txt  (clean protocol://ip:port list for easy ingestion by proxy clients)
"""

import concurrent.futures
from datetime import datetime, timezone
import ipaddress
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_JSON = os.path.join(BASE_DIR, "proxies.json")
OUTPUT_TXT = os.path.join(BASE_DIR, "proxies.txt")

# Verified Raw GitHub URLs for Top 'proxy-list' Repositories
SOURCES = [
    # proxifly/free-proxy-list (7,000+ stars)
    ("proxifly/free-proxy-list", "all", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    ("proxifly/free-proxy-list", "http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("proxifly/free-proxy-list", "https", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt"),
    ("proxifly/free-proxy-list", "socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt"),
    ("proxifly/free-proxy-list", "socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"),

    # TheSpeedX/PROXY-List (5,800+ stars)
    ("TheSpeedX/PROXY-List", "http", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("TheSpeedX/PROXY-List", "socks4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt"),
    ("TheSpeedX/PROXY-List", "socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),

    # monosans/proxy-list (1,300+ stars)
    ("monosans/proxy-list", "all", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"),
    ("monosans/proxy-list", "http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("monosans/proxy-list", "socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
    ("monosans/proxy-list", "socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("monosans/proxy-list", "anonymous", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/all.txt"),

    # hookzof/socks5_list (1,000+ stars)
    ("hookzof/socks5_list", "socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),

    # clarketm/proxy-list (2,300+ stars)
    ("clarketm/proxy-list", "http", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),

    # roosterkid/openproxylist
    ("roosterkid/openproxylist", "https", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("roosterkid/openproxylist", "socks4", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt"),
    ("roosterkid/openproxylist", "socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),

    # jetkai/proxy-list
    ("jetkai/proxy-list", "https", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt"),
    ("jetkai/proxy-list", "http", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"),
    ("jetkai/proxy-list", "socks4", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt"),
    ("jetkai/proxy-list", "socks5", "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"),

    # ShiftyTR/Proxy-List
    ("ShiftyTR/Proxy-List", "http", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
    ("ShiftyTR/Proxy-List", "https", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt"),
    ("ShiftyTR/Proxy-List", "socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("ShiftyTR/Proxy-List", "socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),

    # MuRongPIG/Proxy-Master
    ("MuRongPIG/Proxy-Master", "http", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"),
    ("MuRongPIG/Proxy-Master", "socks4", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt"),
    ("MuRongPIG/Proxy-Master", "socks5", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),

    # ErcinDedeoglu/proxies
    ("ErcinDedeoglu/proxies", "http", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt"),
    ("ErcinDedeoglu/proxies", "https", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt"),
    ("ErcinDedeoglu/proxies", "socks4", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt"),
    ("ErcinDedeoglu/proxies", "socks5", "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt"),

    # Zaeem20/FREE_PROXIES_LIST
    ("Zaeem20/FREE_PROXIES_LIST", "http", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"),
    ("Zaeem20/FREE_PROXIES_LIST", "https", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt"),
    ("Zaeem20/FREE_PROXIES_LIST", "socks4", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks4.txt"),
    ("Zaeem20/FREE_PROXIES_LIST", "socks5", "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt"),

    # prxchk/proxy-list
    ("prxchk/proxy-list", "http", "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt"),
    ("prxchk/proxy-list", "socks4", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt"),
    ("prxchk/proxy-list", "socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt"),

    # hendrikbgr/Free-Proxy-Repo
    ("hendrikbgr/Free-Proxy-Repo", "http", "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt"),

    # mmpx12/proxy-list
    ("mmpx12/proxy-list", "http", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("mmpx12/proxy-list", "https", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt"),
    ("mmpx12/proxy-list", "socks4", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("mmpx12/proxy-list", "socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),

    # sunny9577/proxy-scraper
    ("sunny9577/proxy-scraper", "http", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt"),
    ("sunny9577/proxy-scraper", "socks4", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks4_proxies.txt"),
    ("sunny9577/proxy-scraper", "socks5", "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/socks5_proxies.txt"),

    # Anonym0usWork1221/Free-Proxies
    ("Anonym0usWork1221/Free-Proxies", "http", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/master/proxy_files/http_proxies.txt"),
    ("Anonym0usWork1221/Free-Proxies", "https", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/master/proxy_files/https_proxies.txt"),
    ("Anonym0usWork1221/Free-Proxies", "socks4", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/master/proxy_files/socks4_proxies.txt"),
    ("Anonym0usWork1221/Free-Proxies", "socks5", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/master/proxy_files/socks5_proxies.txt"),

    # vakhov/fresh-proxy-list
    ("vakhov/fresh-proxy-list", "http", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"),
    ("vakhov/fresh-proxy-list", "https", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/https.txt"),
    ("vakhov/fresh-proxy-list", "socks4", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks4.txt"),
    ("vakhov/fresh-proxy-list", "socks5", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt"),

    # officialputuid/KangProxy
    ("officialputuid/KangProxy", "http", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt"),
    ("officialputuid/KangProxy", "https", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/https/https.txt"),
    ("officialputuid/KangProxy", "socks4", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks4/socks4.txt"),
    ("officialputuid/KangProxy", "socks5", "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/socks5/socks5.txt"),

    # B4RC0D3-UA/proxy-list
    ("B4RC0D3-UA/proxy-list", "http", "https://raw.githubusercontent.com/B4RC0D3-UA/proxy-list/main/HTTP.txt"),
    ("B4RC0D3-UA/proxy-list", "socks4", "https://raw.githubusercontent.com/B4RC0D3-UA/proxy-list/main/SOCKS4.txt"),
    ("B4RC0D3-UA/proxy-list", "socks5", "https://raw.githubusercontent.com/B4RC0D3-UA/proxy-list/main/SOCKS5.txt"),

    # zevtyardt/proxy-list
    ("zevtyardt/proxy-list", "http", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt"),
    ("zevtyardt/proxy-list", "socks4", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt"),
    ("zevtyardt/proxy-list", "socks5", "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt"),

    # ALIILAPRO/Proxy
    ("ALIILAPRO/Proxy", "http", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt"),
    ("ALIILAPRO/Proxy", "socks4", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt"),
    ("ALIILAPRO/Proxy", "socks5", "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt"),

    # ObcbO/getproxy
    ("ObcbO/getproxy", "http", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt"),
    ("ObcbO/getproxy", "https", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/https.txt"),
    ("ObcbO/getproxy", "socks4", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks4.txt"),
    ("ObcbO/getproxy", "socks5", "https://raw.githubusercontent.com/ObcbO/getproxy/master/file/socks5.txt"),

    # r00tee/Proxy-List
    ("r00tee/Proxy-List", "https", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt"),
    ("r00tee/Proxy-List", "socks4", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt"),
    ("r00tee/Proxy-List", "socks5", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt"),

    # zloi-user/hideip.me
    ("zloi-user/hideip.me", "http", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt"),
    ("zloi-user/hideip.me", "https", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt"),
    ("zloi-user/hideip.me", "socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt"),
    ("zloi-user/hideip.me", "socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt"),

    # iplocate/free-proxy-list
    ("iplocate/free-proxy-list", "http", "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"),
    ("iplocate/free-proxy-list", "socks4", "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks4.txt"),
    ("iplocate/free-proxy-list", "socks5", "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt"),

    # a2u/free-proxy-list
    ("a2u/free-proxy-list", "http", "https://raw.githubusercontent.com/a2u/free-proxy-list/master/free-proxy-list.txt"),

    # dpangestuw/Free-Proxy
    ("dpangestuw/Free-Proxy", "http", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt"),
    ("dpangestuw/Free-Proxy", "socks4", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks4_proxies.txt"),
    ("dpangestuw/Free-Proxy", "socks5", "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/socks5_proxies.txt"),

    # SevenworksDev/proxy-list
    ("SevenworksDev/proxy-list", "http", "https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/http.txt"),
    ("SevenworksDev/proxy-list", "https", "https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/https.txt"),
    ("SevenworksDev/proxy-list", "socks4", "https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/socks4.txt"),
    ("SevenworksDev/proxy-list", "socks5", "https://raw.githubusercontent.com/SevenworksDev/proxy-list/main/proxies/socks5.txt"),

    # rdavydov/proxy-list
    ("rdavydov/proxy-list", "http", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"),
    ("rdavydov/proxy-list", "socks4", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt"),
    ("rdavydov/proxy-list", "socks5", "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt"),

    # MrMarble/proxy-list
    ("MrMarble/proxy-list", "http", "https://raw.githubusercontent.com/MrMarble/proxy-list/main/http.txt"),
    ("MrMarble/proxy-list", "https", "https://raw.githubusercontent.com/MrMarble/proxy-list/main/https.txt"),
    ("MrMarble/proxy-list", "socks4", "https://raw.githubusercontent.com/MrMarble/proxy-list/main/socks4.txt"),
    ("MrMarble/proxy-list", "socks5", "https://raw.githubusercontent.com/MrMarble/proxy-list/main/socks5.txt"),

    # VPSLabCloud/VPSLab-Free-Proxy-List
    ("VPSLabCloud/VPSLab-Free-Proxy-List", "all", "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/all_proxies.txt"),
    ("VPSLabCloud/VPSLab-Free-Proxy-List", "https", "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/all_ssl.txt"),

    # rix4uni/fresh-proxy-list
    ("rix4uni/fresh-proxy-list", "http", "https://raw.githubusercontent.com/rix4uni/fresh-proxy-list/main/proxylist.txt"),

    # mzyui/proxy-list
    ("mzyui/proxy-list", "http", "https://raw.githubusercontent.com/mzyui/proxy-list/main/all.txt")
]

PROXY_REGEX = re.compile(
    r'(?:(https?|socks4|socks5h?)://)?([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}):([0-9]{1,5})(?!\d)',
    re.IGNORECASE
)

def is_routable_public_ipv4(ip_str: str) -> bool:
    """Returns True only for valid public unicast IPv4 addresses (rejects bogons and multicast)."""
    try:
        ip_obj = ipaddress.IPv4Address(ip_str)
    except ValueError:
        return False
    # Python's is_global is True for 224.0.0.0/4 multicast, so exclude multicast explicitly.
    return bool(
        ip_obj.is_global
        and not ip_obj.is_multicast
        and not ip_obj.is_private
        and not ip_obj.is_loopback
        and not ip_obj.is_link_local
        and not ip_obj.is_reserved
        and not ip_obj.is_unspecified
    )

def get_ssl_context():
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()

def fetch_source(item):
    repo, default_proto, url = item
    ctx = get_ssl_context()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LunamixProxyCollector/1.0",
        "Accept": "*/*"
    }
    req = urllib.request.Request(url, headers=headers)
    found = []
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            content = r.read().decode("utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = PROXY_REGEX.search(line)
                if match:
                    prefix_proto = (match.group(1) or "").lower()
                    ip = match.group(2)
                    try:
                        port = int(match.group(3))
                    except ValueError:
                        continue

                    # Validate IPv4 octets & Ensure Public Unicast Routable IP
                    if not is_routable_public_ipv4(ip):
                        continue

                    if not (1 <= port <= 65535):
                        continue

                    # Determine protocol
                    if prefix_proto == "socks5h":
                        prefix_proto = "socks5"
                    proto = prefix_proto if prefix_proto else default_proto
                    if proto not in ("http", "https", "socks4", "socks5"):
                        proto = "http"

                    found.append((proto, ip, port, repo))

        elapsed = time.time() - t0
        return True, repo, default_proto, url, len(found), found, f"{elapsed:.2f}s"
    except Exception as exc:
        elapsed = time.time() - t0
        return False, repo, default_proto, url, 0, [], f"Error: {exc} ({elapsed:.2f}s)"

def main():
    print("=" * 72)
    print("     LUNAMIX PROXY COLLECTOR (github.com/topics/proxy-list)     ")
    print("=" * 72)
    print(f"[*] Dispatching concurrent scrapers to {len(SOURCES)} verified upstream feeds...")

    proxies_map = {}  # key: (ip, port), value: dict
    sources_success = 0
    sources_failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(fetch_source, s) for s in SOURCES]
        for future in concurrent.futures.as_completed(futures):
            ok, repo, default_proto, url, count, found, info = future.result()
            if ok:
                sources_success += 1
                print(f"  ✓ [{repo:<28}] ({default_proto:<6}): {count:>5} proxies ({info})")
                for proto, ip, port, source_repo in found:
                    key = (ip, port)
                    if key not in proxies_map:
                        proxies_map[key] = {
                            "ip": ip,
                            "port": port,
                            "protocols": set([proto]),
                            "sources": set([source_repo])
                        }
                    else:
                        proxies_map[key]["protocols"].add(proto)
                        proxies_map[key]["sources"].add(source_repo)
            else:
                sources_failed += 1
                print(f"  ✗ [{repo:<28}] ({default_proto:<6}): {info}")

    total_unique = len(proxies_map)
    print("\n" + "=" * 72)
    print(f"[*] Aggregation Complete: {sources_success} feeds succeeded, {sources_failed} failed.")
    print(f"[*] Total Unique Proxies Collected: {total_unique:,}")
    print("=" * 72)

    # Classify by protocol
    protocol_counts = {"http": 0, "https": 0, "socks4": 0, "socks5": 0}
    by_protocol = {"http": [], "https": [], "socks4": [], "socks5": []}
    all_proxies_list = []

    for (ip, port), data in sorted(proxies_map.items()):
        protos = sorted(list(data["protocols"]))
        primary_proto = protos[0]
        for p in protos:
            if p in protocol_counts:
                protocol_counts[p] += 1
            if p in by_protocol:
                by_protocol[p].append(f"{ip}:{port}")

        proxy_obj = {
            "ip": ip,
            "port": port,
            "primary_protocol": primary_proto,
            "protocols": protos,
            "url": f"{primary_proto}://{ip}:{port}",
            "sources": sorted(list(data["sources"]))
        }
        all_proxies_list.append(proxy_obj)

    print(f"  • SOCKS5 Proxies : {protocol_counts['socks5']:,}")
    print(f"  • SOCKS4 Proxies : {protocol_counts['socks4']:,}")
    print(f"  • HTTP Proxies   : {protocol_counts['http']:,}")
    print(f"  • HTTPS Proxies  : {protocol_counts['https']:,}")

    # Build Master JSON payload
    export_payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "source_topic": "https://github.com/topics/proxy-list",
            "feeds_queried": len(SOURCES),
            "feeds_succeeded": sources_success,
            "total_unique_proxies": total_unique,
            "protocol_breakdown": protocol_counts
        },
        "protocols": {
            "socks5": by_protocol["socks5"],
            "socks4": by_protocol["socks4"],
            "https": by_protocol["https"],
            "http": by_protocol["http"]
        },
        "proxies": all_proxies_list
    }

    def _atomic_write(path: str, writer_fn) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent)
        file_obj = None
        try:
            file_obj = os.fdopen(fd, "w", encoding="utf-8")
            with file_obj as f:
                writer_fn(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if file_obj is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    print(f"\n[*] Writing master JSON to: {OUTPUT_JSON}...")
    _atomic_write(OUTPUT_JSON, lambda f: json.dump(export_payload, f, indent=2))

    print(f"[*] Writing clean text list to: {OUTPUT_TXT}...")
    def _write_txt(f):
        for p in all_proxies_list:
            f.write(f"{p['url']}\n")
    _atomic_write(OUTPUT_TXT, _write_txt)

    json_mb = os.path.getsize(OUTPUT_JSON) / (1024 * 1024)
    txt_mb = os.path.getsize(OUTPUT_TXT) / (1024 * 1024)
    print(f"✓ Saved {OUTPUT_JSON} ({json_mb:.2f} MB)")
    print(f"✓ Saved {OUTPUT_TXT} ({txt_mb:.2f} MB)")
    print("=" * 72)

if __name__ == "__main__":
    main()
