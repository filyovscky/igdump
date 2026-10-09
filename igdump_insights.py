"""Passive publication reports from received payloads and local caches only."""
from __future__ import annotations

import calendar
import math
import json
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime, timedelta
from html import escape, unescape
from pathlib import Path
from string import Template
from urllib.parse import urlsplit, urlunsplit

from igdump_csv import write_csv
from igdump_storage import read_json, write_json

REPORTS = {"analytics": "Publication analytics", "mentions": "Mentioned and tagged accounts",
           "contacts": "Contacts in captions", "locations": "Publication locations"}
USERNAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,29}")
MENTION = re.compile(r"(?<![\w@.+-])@([A-Za-z0-9_][A-Za-z0-9_.]{0,29})(?![A-Za-z0-9_])")
EMAIL = re.compile(r"(?<![\w.+@-])([\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+)", re.I)
URL = re.compile(r"(?<![\w@])(?:https?://|www\.|(?:t\.me|wa\.me)/)[^\s<>\"\u201c\u201d]+", re.I)
BARE_URL = re.compile(r"(?<![\w@./-])(?:[A-Za-z0-9-]+\.)+(?:com|org|net|ru|рф|io|info|me|co|dev|app)(?:/[^\s<>\"]*)?(?![\w.-])", re.I)
PHONE = re.compile(r"(?<![\w])\+?[0-9](?:[0-9 ()\t.-]{5,30}[0-9])(?![\w])")
CONTACT_HINT = re.compile(r"(?:тел(?:ефон)?|звон|контакт|whatsapp|phone|call|tel\s*:)", re.I)


def publication_details(payload: dict) -> dict:
    """Normalize only fields actually present; a missing field is not an empty field."""
    result = {}
    timestamp = payload.get("taken_at", payload.get("taken_at_timestamp"))
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            datetime.fromtimestamp(timestamp, UTC)
            result["taken_at"] = timestamp
        except (ValueError, OverflowError, OSError):
            pass
    fallback = payload.get("_fallback_timestamp")
    if "taken_at" not in result and isinstance(fallback, str):
        try:
            value = datetime.fromisoformat(fallback.replace("Z", "+00:00"))
            if value.tzinfo:
                result["taken_at"] = value.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    if "caption" in payload:
        caption = payload["caption"]
        result["caption"] = (caption.get("text") or "") if isinstance(caption, dict) else (caption if isinstance(caption, str) else "")
    elif "edge_media_to_caption" in payload:
        result["caption"] = "\n".join((edge.get("node") or {}).get("text") or "" for edge in (payload["edge_media_to_caption"] or {}).get("edges", []))

    tags = []
    tags_known = False
    children = [(edge.get("node") or {}) for edge in (payload.get("edge_sidecar_to_children") or {}).get("edges", [])]
    for item in [payload, *(payload.get("carousel_media") or []), *children]:
        if "usertags" in item:
            tags_known = True
            tags.extend((tag.get("user") or {}) for tag in (item.get("usertags") or {}).get("in", []))
        if "edge_media_to_tagged_user" in item:
            tags_known = True
            tags.extend(((edge.get("node") or {}).get("user") or {}) for edge in (item.get("edge_media_to_tagged_user") or {}).get("edges", []))
    if tags_known:
        result["tagged_users"] = sorted({user["username"].lower(): {"username": user["username"].lower(),
            "user_id": str(user.get("pk") or user.get("id") or ""), "full_name": user.get("full_name") or ""}
            for user in tags if isinstance(user.get("username"), str) and USERNAME.fullmatch(user["username"])}.values(), key=lambda user: user["username"])
    if "tagged_users" in payload:  # Our own compact records.
        result["tagged_users"] = payload["tagged_users"]
    if "location" in payload:
        location = payload.get("location")
        result["location"] = None
        if isinstance(location, dict):
            result["location"] = {"id": str(location.get("pk") or location.get("id") or ""),
                "name": location.get("name") or "", "lat": coordinate(location.get("lat", location.get("latitude")), 90),
                "lng": coordinate(location.get("lng", location.get("longitude")), 180)}
    return result


def coordinate(value, bound):
    try:
        value = float(value)
        return value if math.isfinite(value) and -bound <= value <= bound else None
    except (ValueError, TypeError, OverflowError):
        return None


def remember_details(session, items):
    records = getattr(session, "_igdump_post_details", None)
    if not isinstance(records, dict):
        records = session._igdump_post_details = {}
    for item in items:
        code = item.get("code") or item.get("shortcode")
        if isinstance(code, str):
            records[code] = {**records.get(code, {}), **publication_details(item)}


def save_details(session, root: Path, username: str):
    data = read_json(root / ".post-details.json") or {}
    records = data.get("posts", {}) if data.get("username", "").lower() == username.lower() else {}
    if not isinstance(records, dict):
        records = {}
    for code, value in getattr(session, "_igdump_post_details", {}).items():
        records[code] = {**records.get(code, {}), **value}
    write_json(root / ".post-details.json", {"version": 1, "username": username, "posts": records})
    return records


def cached_details(root: Path, username: str, links: list[str]):
    cached = read_json(root / ".post-details.json") or {}
    records = cached.get("posts", {}) if cached.get("username", "").lower() == username.lower() else {}
    records = {code: dict(value) for code, value in records.items() if isinstance(value, dict)} if isinstance(records, dict) else {}
    state = read_json(root / ".export-state.json") or {}
    if (state.get("job") or {}).get("username", "").lower() == username.lower():
        for old in state.get("post_records", []):
            code = old.get("shortcode")
            if code:
                row = {**publication_details(old), **records.get(code, {})}
                if "taken_at" not in row:
                    try:
                        row["taken_at"] = datetime.strptime(old.get("date_label", ""), "%d.%m.%Y %H:%M").astimezone().timestamp()
                        row["legacy_time"] = True
                    except (ValueError, OverflowError, OSError):
                        pass
                records[code] = row
    selected = []
    seen = set()
    for link in links:
        match = re.search(r"/(?:p|reel|tv)/([^/?#]+)", link)
        if match and re.fullmatch(r"[A-Za-z0-9_-]+", match[1]) and match[1] not in seen:
            code = match[1]
            seen.add(code)
            selected.append({**records.get(code, {}), "shortcode": code, "source_url": f"https://www.instagram.com/p/{code}/"})
    return selected


def contact_matches(text: str):
    """Independent text extractor: normalized values plus original text evidence."""
    text = unicodedata.normalize("NFKC", unescape(text))
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    # Bracketed common email obfuscation; avoid interpreting ordinary prose as email.
    prepared = re.sub(r"\s*[\[({]\s*(?:at|собака|@)\s*[\])}]\s*", "@", text, flags=re.I)
    prepared = re.sub(r"\s*[\[({]\s*(?:dot|точка|\.)\s*[\])}]\s*", ".", prepared, flags=re.I)
    # Keep whitespace around literal @: captions use it for account mentions.
    # Only explicit bracketed obfuscation above may join an address.
    found = []
    for match in EMAIL.finditer(prepared):
        value = match[1].rstrip(".,;:!?")
        local, domain = value.rsplit("@", 1)
        try:
            domain = domain.encode("idna").decode("ascii").lower()
        except UnicodeError:
            continue
        suffix = domain.rsplit(".", 1)[-1]
        if (local.startswith(".") or local.endswith(".") or ".." in local
                or not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]+", suffix)
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split('.'))
                or suffix in {"png", "jpg", "svg", "mp4"}):
            continue
        found.append(("email", f"{local.lower()}@{domain}", match[0]))
    occupied = []
    for pattern in (URL, BARE_URL):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            value = match[0].rstrip(".,;:!?)]}»")
            candidate = value if value.lower().startswith(("http://", "https://")) else "https://" + value
            try:
                parts = urlsplit(candidate)
                if not parts.hostname or "." not in parts.hostname or parts.username or parts.password:
                    continue
                _ = parts.port
                normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))
            except ValueError:
                continue
            occupied.append(match.span())
            found.append(("url", normalized, value))
    for line in text.splitlines():  # Never join separate lines into a phone number.
        for match in PHONE.finditer(line):
            value = match[0].strip()
            digits = re.sub(r"\D", "", value)
            if not 7 <= len(digits) <= 15:
                continue
            if re.fullmatch(r"\d{1,4}([./-])\d{1,2}\1\d{1,4}", value) or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
                continue
            # A bare identifier is a phone only when the surrounding text says so.
            hint = CONTACT_HINT.search(line[max(0, match.start()-30):match.start()])
            formatted = len(digits) >= 9 and len(re.findall(r"[0-9]+", value)) >= 3 and bool(re.search(r"[ ()-]", value))
            if not value.startswith("+") and not hint and not formatted:
                continue
            normalized = "+" + digits if value.startswith("+") else ("+7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits)
            found.append(("phone", normalized, value))
    seen = set()
    for kind, value, evidence in found:
        if (kind, value) not in seen:
            seen.add((kind, value))
            yield kind, value, evidence


def mention_rows(posts):
    rows = []
    for post in posts:
        seen = set()
        for match in MENTION.finditer(post.get("caption", "")):
            username = match[1].rstrip(".").lower()
            if username and (username, "caption") not in seen:
                seen.add((username, "caption"))
                rows.append({"username": username, "user_id": "", "full_name": "", "kind": "caption",
                    "evidence": match[0], "post_shortcode": post["shortcode"], "source_url": post["source_url"]})
        for user in post.get("tagged_users", []):
            username = user.get("username", "").lower()
            if USERNAME.fullmatch(username) and (username, "tag") not in seen:
                seen.add((username, "tag"))
                rows.append({**user, "username": username, "kind": "tag", "evidence": "Instagram media tag",
                    "post_shortcode": post["shortcode"], "source_url": post["source_url"]})
    return rows


def contact_rows(posts):
    return [{"type": kind, "value": value, "evidence": evidence, "context": post.get("caption", ""),
             "post_shortcode": post["shortcode"], "source_url": post["source_url"]}
            for post in posts for kind, value, evidence in contact_matches(post.get("caption", ""))]


def location_rows(posts):
    return [{"name": location.get("name", ""), "location_id": location.get("id", ""),
        "latitude": location.get("lat"), "longitude": location.get("lng"),
        "published_at": location_publication_date(post),
        "post_shortcode": post["shortcode"], "source_url": post["source_url"]}
        for post in posts if isinstance(location := post.get("location"), dict)]


def location_publication_date(post):
    stamp = post.get('taken_at')
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return None
    try:
        return datetime.fromtimestamp(stamp, UTC).astimezone().isoformat(timespec='minutes')
    except (ValueError, OverflowError, OSError):
        return None


def publication_stats(posts):
    times = []
    timestamps = []
    for post in posts:
        try:
            stamp = post.get("taken_at")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                times.append(datetime.fromtimestamp(stamp, UTC).astimezone())
                timestamps.append(stamp)
        except (ValueError, OverflowError, OSError):
            pass
    daily = Counter(value.date().isoformat() for value in times)
    monthly = Counter(value.strftime("%Y-%m") for value in times)
    heatmap = [[0] * 24 for _ in range(7)]
    for value in times:
        heatmap[value.weekday()][value.hour] += 1
    first = min(times).date() if times else None
    last = max(times).date() if times else None
    span = (last-first).days + 1 if first else 0
    # Explicit zero months between observations, rather than hiding inactive periods.
    if first:
        year, month = first.year, first.month
        while (year, month) <= (last.year, last.month):
            monthly.setdefault(f"{year:04d}-{month:02d}", 0)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return {"selected_posts": len(posts), "dated_posts": len(times), "unknown_dates": len(posts)-len(times),
        "timezone": f"Export computer local time ({datetime.now().astimezone().tzname()}, UTC{datetime.now().astimezone().strftime('%z')})",
        "timestamps": timestamps,
        "first_day": first.isoformat() if first else None, "last_day": last.isoformat() if last else None,
        "observed_days": span, "active_days": len(daily),
        "posts_per_day": round(len(times)/span, 3) if span else None,
        "posts_per_week": round(len(times)*7/span, 3) if span else None,
        "daily": dict(sorted(daily.items())), "monthly": dict(sorted(monthly.items())),
        "weekdays": [sum(row) for row in heatmap], "hours": [sum(row[h] for row in heatmap) for h in range(24)],
        "heatmap": heatmap, "legacy_times": sum(bool(post.get("legacy_time")) for post in posts)}


def bars(labels, counts, title):
    maximum = max(counts, default=0) or 1
    lines = []
    for label, count in zip(labels, counts):
        lines.append(f'<div class="bar-row"><span>{escape(str(label))}</span><div class="bar-track"><div class="bar" style="width:{count/maximum*100:.2f}%"></div></div><span>{count}</span></div>')
    return f'<h2>{escape(title)}</h2><div class="bars">' + "".join(lines) + '</div>'


def analytics_body(stats):
    if not stats["dated_posts"]:
        return '<p>No publication timestamps were received. No dates or hours have been inferred.</p>'
    maximum = max((max(row) for row in stats["heatmap"]), default=0) or 1
    header = '<tr><th>Day / hour</th>' + ''.join(f'<th>{h:02d}</th>' for h in range(24)) + '</tr>'
    rows = []
    for day, values in zip(calendar.day_abbr, stats["heatmap"]):
        cells = ''.join(f'<td style="background:rgba(192,46,101,{0.08 + count/maximum*0.72:.3f})" title="{day} {h:02d}:00: {count} posts">{count}</td>' for h, count in enumerate(values))
        rows.append(f'<tr><th>{day}</th>{cells}</tr>')
    frequency = (f'{stats["dated_posts"]} dated posts · {stats["active_days"]} active days / {stats["observed_days"]} calendar days · '
        f'{stats["posts_per_day"]} posts/day · {stats["posts_per_week"]} posts/week. '
        f'Observed span: {stats["first_day"]} through {stats["last_day"]}. Zero-post days are included in the rate.')
    return ('<div class="report-controls"><label for="timezone">Chart time zone</label><select id="timezone"><option value="">Export computer local time</option></select>'
        '<button id="analytics-csv" type="button">Download CSV in selected zone</button>'
        '<button id="analytics-json" type="button">Download JSON in selected zone</button></div>'
        '<p class="meta" id="timezone-note">Hours and calendar dates use the time zone shown below. These are publication times, not audience activity. Daylight saving is applied at each publication date.</p>'
        '<div id="analytics-charts">'
        + f'<p>{escape(frequency)}</p><p>{escape(stats["timezone"])}.</p>'
        + bars(list(stats["monthly"]), list(stats["monthly"].values()), 'Posts by month')
        + bars(list(calendar.day_name), stats["weekdays"], 'Posts by weekday')
        + bars([f'{h:02d}:00' for h in range(24)], stats["hours"], 'Posts by hour')
        + '<h2>Weekday × hour</h2><div class="table-scroll"><table class="heatmap"><thead>' + header + '</thead><tbody>' + ''.join(rows) + '</tbody></table></div></div>'
        + '<script id="analytics-data" type="application/json">' + json.dumps(stats).replace('<', '\\u003c') + '</script><script src="insights.js" defer></script>')


def source_link(row):
    return f'<a href="{escape(row["source_url"], quote=True)}">{escape(row["post_shortcode"])}</a>'


def evidence_table(rows, columns):
    header = ''.join(f'<th>{escape(label)}</th>' for _, label in columns) + '<th>Source post</th>'
    body = []
    for row in rows:
        cells = ''.join('<td>' + ('<div class="caption-text" tabindex="0" aria-label="Original caption">' if key == 'context' else '')
            + escape(str(row.get(key) if row.get(key) is not None else '')) + ('</div>' if key == 'context' else '') + '</td>' for key, _ in columns)
        body.append('<tr>' + cells + '<td>' + source_link(row) + '</td></tr>')
    layout = ' class="contacts-table"' if any(key == 'context' for key, _ in columns) else ''
    widths = '<colgroup><col style="width:90px"><col style="width:205px"><col style="width:205px"><col><col style="width:120px"></colgroup>' if layout else ''
    return '<div class="table-scroll"><table' + layout + '>' + widths + '<thead><tr>' + header + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>'


def locations_map(rows):
    points = [row for row in rows if row['latitude'] is not None and row['longitude'] is not None]
    grid = ''.join(f'<path d="M{x} 0V360"/>' for x in range(0, 721, 120)) + ''.join(f'<path d="M0 {y}H720"/>' for y in range(0, 361, 60))
    dots = []
    groups = {}
    for row in points:
        groups.setdefault((row['latitude'], row['longitude']), []).append(row)
    for index, group in enumerate(groups.values()):
        row = group[0]
        x, y = (row['longitude']+180)*2, (90-row['latitude'])*2
        dates = sorted({r['published_at'][:10] for r in group if r.get('published_at')})
        date_label = (' → '.join((dates[0], dates[-1])) if len(dates) > 1 else dates[0]) if dates else 'Date unknown'
        if len(group) > 1:
            date_label += f' ({len(group)} posts)'
        label = '\n'.join(f"{r['name']} · {r.get('published_at') or 'Date unknown'} · {r['post_shortcode']}" for r in group)
        dots.append(f'<g class="location-pin" data-location-group="{index}" tabindex="0" role="button" aria-label="{escape(label, quote=True)}"><title>{escape(label)}</title><circle cx="{x:.2f}" cy="{y:.2f}" r="4"/><text class="pin-date" x="{x:.2f}" y="{y:.2f}">{escape(date_label)}</text></g>')
    world = json.loads((Path(__file__).parent / 'igdump_assets' / 'world-land.json').read_text(encoding='utf-8'))
    land = ''.join(f'<path d="{escape(country["path"], quote=True)}"><title>{escape(country["name"])}</title></path>' for country in world)
    coordinates = [[(row['longitude']+180)*2, (90-row['latitude'])*2] for row in points]
    return ('<h2>Coordinate map</h2><p>Offline geographic map. Drag to pan; use + / − to zoom. Click a pin for publication dates, places and source posts. Identical coordinates are grouped.</p>'
        + '<p class="meta">Pin dates use the export computer’s local time zone. Exact publication times below include their UTC offset. These are publication dates, not confirmed visit dates.</p>'
        + ('<p>No coordinates received. Named places remain in the source table.</p>' if not points else '')
        + '<div class="report-controls"><button id="street-map-toggle" type="button">Load street map (Internet)</button><span class="meta">Street tiles are requested from OpenStreetMap only after this click.</span></div>'
        + '<div id="offline-map-controls" class="report-controls"><button type="button" data-map="in" aria-label="Zoom in">+</button><button type="button" data-map="out" aria-label="Zoom out">−</button><button type="button" data-map="fit">Fit locations</button><button type="button" data-map="world">World</button></div>'
        + '<div id="street-map" hidden aria-label="Street map of publication locations"></div>'
        + '<div class="report-controls"><label><input id="pin-dates" type="checkbox" checked> Show pin dates</label></div><div id="offline-pin-details" aria-live="polite"></div>'
        + '<svg id="location-map" class="coordinate-map" viewBox="-8 -8 736 376" role="img" aria-label="Geographic map of publication locations" tabindex="0"><g class="map-land">' + land + '</g><g class="map-grid">' + grid + '</g>' + ''.join(dots) + '</svg>'
        + '<p class="map-note">Country outlines: <a href="https://www.naturalearthdata.com/">Natural Earth</a>. Approximate geography at small scale; no streets or external map requests.</p>'
        + '<script id="map-data" type="application/json">' + json.dumps(coordinates) + '</script>'
        + '<script id="map-records" type="application/json">' + json.dumps(points).replace('<', '\\u003c') + '</script>'
        + '<link rel="stylesheet" href="leaflet.css"><script src="leaflet.js" defer></script><script src="insights.js" defer></script>')


def generate_reports(root: Path, username: str, links: list[str], enabled: list[str], selection: str, full=False):
    posts = cached_details(root, username, links)
    if 'analytics' in enabled or 'locations' in enabled:
        (root / 'insights.js').write_text((Path(__file__).parent / 'igdump_assets' / 'insights.js').read_text(encoding='utf-8'), encoding='utf-8')
    if 'locations' in enabled:
        for filename in ('leaflet.js', 'leaflet.css', 'leaflet-license.txt', 'map-data-license.txt'):
            (root / filename).write_bytes((Path(__file__).parent / 'igdump_assets' / filename).read_bytes())
    template = Template((Path(__file__).parent / 'igdump_assets' / 'insights.html').read_text(encoding='utf-8'))
    reports = []
    for kind in enabled:
        title = REPORTS[kind]
        if kind == 'analytics':
            stats = publication_stats(posts)
            write_json(root / 'analytics.json', stats)
            rows = [{"date": day, "posts": stats['daily'].get(day, 0)} for day in (
                (datetime.fromisoformat(stats['first_day']).date() + timedelta(days=i)).isoformat()
                for i in range(stats['observed_days']))] if stats['first_day'] else []
            write_csv(root / 'analytics.csv', ['date', 'posts'], rows)
            coverage = f"{stats['dated_posts']} of {len(posts)} selected posts have a timestamp; {stats['unknown_dates']} unknown. {stats['legacy_times']} times recovered from old local date labels."
            body = analytics_body(stats)
        elif kind == 'mentions':
            rows = mention_rows(posts)
            columns = [('username', 'Account'), ('full_name', 'Name'), ('kind', 'Evidence type'), ('evidence', 'Evidence')]
            fields = ['username', 'user_id', 'full_name', 'kind', 'evidence', 'post_shortcode', 'source_url']
            captions = sum('caption' in post for post in posts)
            tags = sum('tagged_users' in post for post in posts)
            coverage = f"Captions available: {captions}/{len(posts)} posts. Media tag data available: {tags}/{len(posts)}. An account reference is not a verified real-world identity."
            ranking = Counter(row['username'] for row in rows)
            body = bars(list(dict(ranking.most_common(20))), list(dict(ranking.most_common(20)).values()), 'Most referenced accounts (post / evidence type)') + evidence_table(rows, columns)
        elif kind == 'contacts':
            rows = contact_rows(posts)
            columns = [('type', 'Type'), ('value', 'Normalized value'), ('evidence', 'Matched normalized text'), ('context', 'Original caption')]
            fields = ['type', 'value', 'evidence', 'context', 'post_shortcode', 'source_url']
            coverage = f"Captions available: {sum('caption' in post for post in posts)}/{len(posts)} posts. Contacts are text matches, not verified ownership. Links are extracted without visiting them."
            body = evidence_table(rows, columns)
        else:
            rows = location_rows(posts)
            columns = [('name', 'Place'), ('location_id', 'Location ID'), ('latitude', 'Latitude'), ('longitude', 'Longitude'), ('published_at', 'Published at (UTC offset)')]
            fields = ['name', 'location_id', 'latitude', 'longitude', 'published_at', 'post_shortcode', 'source_url']
            coverage = f"Location field received: {sum('location' in post for post in posts)}/{len(posts)} posts. {len(rows)} geotagged posts. Missing locations are not inferred from caption text."
            body = locations_map(rows) + evidence_table(rows, columns)
        if kind != 'analytics':
            write_csv(root / f'{kind}.csv', fields, rows)
            write_json(root / f'{kind}.json', {'username': username, 'selected_posts': len(posts), 'coverage': coverage, 'records': rows})
        nav = ''.join(f'<a href="{name}.html">{escape(REPORTS[name])}</a>' for name in enabled)
        for filename, label in (('full.html', 'Full export'), ('index.html', 'Post archive')):
            if (root / filename).exists() or (full and filename == 'full.html'):
                nav += f'<a href="{filename}">{label}</a>'
        html = template.substitute(title=escape(f'{title} — @{username}'), selection=escape(selection),
            coverage=escape(coverage), navigation=nav, kind=kind, body=body or '<p>No matches in the available data.</p>')
        (root / f'{kind}.html').write_text(html, encoding='utf-8')
        reports.append((title, f'{kind}.html', 0))
    return reports
