#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import random
import re
import time
import sys
import webbrowser
import traceback
import os
import sqlite3
from datetime import UTC, datetime
from html import escape
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from pathlib import Path
from typing import Any
from collections import deque
from urllib.parse import urlparse, parse_qs, urlencode, urljoin
from html.parser import HTMLParser
from string import Template

# ── Debug log (structured, AI-readable) ──────────────────────────────────────

_DEBUG_EVENTS = deque(maxlen=2000)
_DEBUG_LOG_PATH: Path | None = None

def _debug_event(event_type: str, message: str, **context) -> None:
    if event_type == "http.send":
        record_request()
    def redact(value):
        if isinstance(value, str):
            return re.sub(r"(sessionid|csrftoken)=([^&\s]+)", r"\1=***", value, flags=re.IGNORECASE)
        return value
    entry: dict[str, Any] = {
        "t": datetime.now(UTC).isoformat(),
        "event": event_type,
        "msg": redact(message),
    }
    if context:
        entry["ctx"] = {k: redact(v) for k, v in context.items() if v is not None}
    _DEBUG_EVENTS.append(entry)
    logger.debug("[dbg] %s: %s", event_type, entry["msg"])


def _save_debug_log(output_dir: Path) -> None:
    global _DEBUG_LOG_PATH
    path = output_dir / ".debug_log.json"
    try:
        path.write_text(
            json.dumps(list(_DEBUG_EVENTS), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _DEBUG_LOG_PATH = path
    except OSError as exc:
        logger.warning("Could not save debug log: %s", exc)


def _patch_session_for_logging(session: requests.Session) -> None:
    """Monkey-patch session.send so every HTTP call is logged."""
    if getattr(session, "_igdump_logged", False):
        return
    session._igdump_logged = True
    original_send = session.send

    def logged_send(request, **kwargs):
        _debug_event("http.send", f"{request.method} {request.url}",
                     method=request.method,
                     url=re.sub(r"(sessionid|csrftoken)=[^&]+", r"\1=***", str(request.url)))
        t0 = time.monotonic()
        try:
            response = original_send(request, **kwargs)
            elapsed = int((time.monotonic() - t0) * 1000)
            _debug_event("http.done", f"{response.status_code} {request.method} {request.url}",
                         status=response.status_code,
                         elapsed_ms=elapsed,
                         method=request.method,
                         url=re.sub(r"(sessionid|csrftoken)=[^&]+", r"\1=***", str(request.url)))
            request_url = urlparse(str(request.url))
            # Profile API limits are endpoint-specific: its caller can use
            # the existing public HTML fallback before giving up.
            if (request_url.hostname in ("www.instagram.com", "instagram.com")
                    and request_url.path.rstrip("/") != "/api/v1/users/web_profile_info"):
                raise_if_rate_limited(response, request_url.path)
            return response
        except requests.RequestException as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            _debug_event("http.error", f"{exc}",
                         error=str(exc),
                         elapsed_ms=elapsed,
                         method=request.method,
                         url=re.sub(r"(sessionid|csrftoken)=[^&]+", r"\1=***", str(request.url)))
            raise

    session.send = logged_send  # type: ignore[method-assign]

import requests
from igdump_storage import read_json, write_json, load_comment_cache, save_comment_cache, LikerStore, CommentStore, PostStore, atomic_write, export_lock
from igdump_dates import parse_date, post_date, date_matches, date_label
from igdump_progress import update_progress, TerminalUI, record_request, prompt_input
from igdump_csv import write_csv
from igdump_insights import REPORTS, publication_details, remember_details, save_details, cached_details, generate_reports
from igdump_summary import generate_summary
from igdump_highlights import export_highlights, rebuild_highlights
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, TimeoutError, sync_playwright

logger = logging.getLogger("igdump")


IG_BASE_URL = "https://www.instagram.com"
IG_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
)
IG_API_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "129477",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": f"{IG_BASE_URL}/",
}
LOGIN_URL = f"{IG_BASE_URL}/accounts/login/"
DEFAULT_PROFILE_DIR = Path.home() / ".insta-export" / "chrome-profile"
POST_WAIT_MS = 2500
NAVIGATION_TIMEOUT_MS = 30_000
AUTH_PROFILE_DOC_ID = "7898261790222653"
TIMELINE_MEDIA_CACHE_LIMIT = 256
TEMPLATE_DIR = Path(__file__).parent / "igdump_assets"
HTML_TEMPLATE = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")

try:
    VERSION = _pkg_version("igdump")
except PackageNotFoundError:
    VERSION = "0.7.0"


class ExportError(RuntimeError):
    pass


class InstagramRateLimitError(ExportError):
    """A run-wide stop condition, never a reason to try another endpoint."""


class InstagramAccessError(ExportError):
    """Login/verification failure that makes further item requests pointless."""


class ProfileHTMLUnavailableError(ExportError):
    """Public HTML cannot identify the profile; authenticated Chrome can."""


def raise_if_rate_limited(response, context):
    if response.status_code == 429:
        response.close()
        raise InstagramRateLimitError(f"Instagram rate limited (429) during {context}. Collection stopped; wait before retrying. Cached progress is preserved.")


def is_rate_limited(error):
    response = getattr(error, "response", None)
    return (isinstance(error, InstagramRateLimitError)
            or (response is not None and response.status_code == 429)
            or bool(re.search(r"\b429\b|rate[- ]limit", str(error), re.I)))


def retryable_request_error(error):
    if is_rate_limited(error):
        return False
    if isinstance(error, requests.HTTPError):
        response = error.response
        return response is not None and 500 <= response.status_code < 600
    return isinstance(error, (requests.ConnectionError, requests.Timeout))


def api_payload(response, context, required=None):
    """A successful HTTP status is insufficient to confirm a successful API call."""
    raise_if_rate_limited(response, context)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise ExportError(f"{context}: Instagram returned invalid JSON; progress is preserved.") from exc
    if not isinstance(data, dict):
        raise ExportError(f"{context}: Instagram returned an unexpected response type.")
    if data.get('status') not in (None, 'ok') or data.get('error') or data.get('errors'):
        if re.search(r'login_required|challenge_required|checkpoint_required|feedback_required', str(data.get('message', '')), re.I):
            raise InstagramAccessError(f"{context}: Instagram denied access: {data.get('message')}. Check login or verification before retrying.")
        raise ExportError(f"{context}: Instagram rejected the request: {data.get('message') or data.get('error') or data.get('errors')}")
    if required is not None and not isinstance(data.get(required), list):
        raise ExportError(f"{context}: Instagram did not return a valid {required} list. This is not an empty successful result.")
    if required is not None and any(not isinstance(item, dict) for item in data[required]):
        raise ExportError(f'{context}: malformed entries in {required}; progress is preserved.')
    return data


class InstagramBrowser:
    def __init__(self, profile_dir: Path, headful: bool) -> None:
        self.profile_dir = profile_dir
        self.headful = headful
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> "InstagramBrowser":
        self.playwright = sync_playwright().start()
        self.context = self._launch_context(self.headful)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.context is not None:
            self.context.close()
        if self.playwright is not None:
            self.playwright.stop()

    def _launch_context(self, headful: bool) -> BrowserContext:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        assert self.playwright is not None
        return self.playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            channel="chrome",
            args=["--disable-quic"],
            headless=not headful,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=IG_MOBILE_USER_AGENT,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

    def relaunch_headful(self) -> None:
        if self.context is not None:
            self.context.close()
        self.context = self._launch_context(True)
        self.headful = True

    def require_context(self) -> BrowserContext:
        if self.context is None:
            raise ExportError("Browser context is not initialized")
        return self.context

    def is_logged_in(self) -> bool:
        cookies = self.require_context().cookies(IG_BASE_URL)
        return any(cookie.get("name") == "sessionid" and cookie.get("value") for cookie in cookies)

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            return

        if not self.headful:
            self.relaunch_headful()

        page = self.require_context().new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except (TimeoutError, PlaywrightError):
            logger.debug("Login page failed to load via automation; waiting for manual login")

        logger.warning("No saved Instagram session in %s", self.profile_dir)
        logger.warning("A Chrome window was opened. Log into Instagram there, then return here.")
        try:
            prompt_input("Press Enter after the Instagram home page is fully loaded, or Ctrl+C to abort: ")
        finally:
            page.close()

        if not self.is_logged_in():
            raise ExportError(
                "Instagram login was not detected in the browser profile. Complete login in the opened Chrome window "
                "and rerun the command."
            )

        logger.info("Instagram session saved in %s", self.profile_dir)

    def open_page(self, url: str) -> Page:
        page = self.require_context().new_page()
        try:
            page.goto(url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
        except TimeoutError:
            logger.debug("Timeout loading %s, continuing with partial page", url)
        return page

    def authenticated_session(self) -> requests.Session:
        session = _make_session()
        for cookie in self.require_context().cookies():
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
                secure=cookie.get("secure", True),
                expires=int(cookie['expires']) if cookie.get('expires', -1) > 0 else None,
            )
        csrf = session.cookies.get("csrftoken")
        if csrf:
            session.headers["X-CSRFToken"] = csrf
        return session


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            **IG_API_HEADERS,
            "User-Agent": IG_MOBILE_USER_AGENT,
        }
    )
    return session


def public_api_session() -> requests.Session:
    """Unauthenticated session — no cookies. Works for public profiles."""
    return _make_session()


def instagram_username(value: str) -> str:
    error = "Enter @username, username, or an Instagram profile URL such as https://www.instagram.com/username/."
    username = value.removeprefix("@")
    if "://" in value or value.lower().startswith(("instagram.com/", "www.instagram.com/", "m.instagram.com/")):
        try:
            profile = urlparse(value if "://" in value else "https://" + value)
            match = re.fullmatch(r"/([A-Za-z0-9._]{1,30})/?", profile.path)
            if (profile.scheme.lower() not in ("http", "https")
                or profile.hostname not in ("instagram.com", "www.instagram.com", "m.instagram.com")
                or profile.username is not None or profile.password is not None or profile.port is not None
                or match is None):
                raise ValueError
            username = match[1]
            if username.lower() in {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about", "legal"}:
                raise ValueError
        except ValueError:
            raise argparse.ArgumentTypeError(error) from None
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        raise argparse.ArgumentTypeError(error)
    return username


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected an integer greater than 0.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected an integer greater than 0.")
    return number


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a nonnegative integer.") from None
    if number < 0:
        raise argparse.ArgumentTypeError("Expected a nonnegative integer.")
    return number


def nonnegative_delay(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a finite, nonnegative number of seconds.") from None
    if not 0 <= number < float("inf"):
        raise argparse.ArgumentTypeError("Expected a finite, nonnegative number of seconds.")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Instagram posts, comments, likes, Highlights or everything together.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  igdump posts @kharlamova_alena
      All posts, newest first.
  igdump posts @kharlamova_alena --limit 100
      The most recent 100 posts.
  igdump posts @kharlamova_alena --oldest --limit 100
      The first 100 posts from the beginning of the profile, oldest first.
  igdump likes @kharlamova_alena --oldest --limit 100
      Users who liked the first 100 posts, with participation statistics.
  igdump full @kharlamova_alena --after 01.01.2024 --before 31.12.2024
      Posts, comments, likes and all passive reports for the date range.
  igdump posts "https://instagram.com/kharlamova_alena/" --analytics --mentions --contacts --locations
      Posts with publication charts, mentioned/tagged accounts, contacts and locations.
  igdump full "@kharlamova_alena" --offline --output-dir "exports/kharlamova_alena-full-0"
      Rebuild all reports from the original export folder without network requests.
  igdump highlights "@kharlamova_alena" --download-videos
      Save available Highlights albums, including video files.

Commands choose what to collect. Options choose which posts to use.
No --limit (or --limit 0) means all posts. --oldest selects earliest posts.
Dates include both boundary days and apply before --limit.
full enables --analytics, --mentions, --contacts and --locations by default.
full also enables --text-report and --highlights; --no-highlights disables the
additional authenticated Highlight requests. --highlight-limit limits albums.
Other commands enable these reports with flags; --no-analytics, --no-mentions,
--no-contacts and --no-locations disable individual reports in full.
Passive reports use received or cached data and add no Instagram requests.
--oldest requires the complete timeline; incomplete timelines stop selection.
Quote @usernames in PowerShell. The @ prefix is optional; profile URLs also work.
Example: igdump full "https://www.instagram.com/kharlamova_alena/"
Without the installed command, use: python insta_html_export.py posts USERNAME
Command help: igdump posts --help
Legacy post commands: all = posts; oldest = posts --oldest --limit N.
""",
    )
    parser.add_argument("--version", action="version", version=f"igdump {VERSION}")
    parser.set_defaults(batch_size=9, headful=False, download_videos=False,
                        sessionid=None, comment_delay=2.0, like_delay=2.0,
                        offline=False, retry_missing=False, after=None, before=None, oldest=False, limit=0,
                        dry_run=False, refresh=False, text_report=None, highlights=None, highlight_limit=0,
                        **{flag:False for flag in REPORTS})
    subparsers = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("username", type=instagram_username, metavar="USERNAME", help="Instagram username, with or without @, or a profile URL (https://www.instagram.com/username/).")
    common.add_argument("--limit", type=nonnegative_int, default=0, metavar="N",
                        help="Maximum number of posts after date filters (0 or omitted = all). Latest posts by default; earliest with --oldest. This limits posts, not comments or users.")
    common.add_argument("--oldest", action="store_true", help="Select earliest posts and show them oldest first. Without a limit, show all selected posts oldest first.")
    common.add_argument("--after", type=parse_date, metavar="DD.MM.YYYY", help="Posts published on or after this day, in your computer's local time zone.")
    common.add_argument("--before", type=parse_date, metavar="DD.MM.YYYY", help="Posts published on or before this day. Combine with --after for an inclusive date range.")
    common.add_argument("--output-dir", default=None, help="Output folder (default: exports/<username>-<command>-<limit> with selection suffixes). Previous matching cache folders are reused.")
    common.add_argument("--refresh", action="store_true", help="Recheck the timeline and replace selected cached data. Normal runs resume unfinished work.")
    common.add_argument("--dry-run", action="store_true", help="Cache the timeline and report the selected post count without collecting media, comments or likes. Requires Instagram access.")
    common.add_argument("--no-open", action="store_true", help="Do not open the finished HTML automatically.")
    common.add_argument("--browser-profile-dir", default=str(DEFAULT_PROFILE_DIR), help="Saved Chrome profile used for Instagram login (default: ~/.insta-export/chrome-profile).")
    common.add_argument("--offline", action="store_true", help="Rebuild reports from the original collection folder without login or HTTP requests. Keep the same selection or specify --output-dir.")
    for flag, title in REPORTS.items():
        common.add_argument(f"--{flag}", action=argparse.BooleanOptionalAction, default=None,
                            help=f"{title} from received/cached posts only, with no extra requests (default: enabled in full).")
    common.add_argument('--text-report', action=argparse.BooleanOptionalAction, default=None, help='Copyable Russian summary from saved profile/posts/comments/likes (default: enabled in full).')
    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("--verbose", action="store_true", help="Show detailed debug logs.")
    verbosity.add_argument("--quiet", action="store_true", help="Show only warnings and errors.")

    posts = subparsers.add_parser("posts", parents=[common],
        help="Export selected posts with local photos and video covers.",
        description="Selected posts in index.html with local media. Choose posts using --limit, --oldest and date filters.",
        epilog="Example: igdump posts @kharlamova_alena --oldest --limit 100")
    comments = subparsers.add_parser("comments", parents=[common],
        help="Collect comments and participation statistics.",
        description="Comments on selected posts: comments.html, comments.csv and comments-stats.csv. No post media downloads.")
    likes = subparsers.add_parser("likes", parents=[common],
        help="Collect users who liked posts and participation statistics.",
        description="Users who liked selected posts: likes.html, likes.csv and likes-stats.csv. No post media downloads. Incomplete lists are automatically rechecked once.")
    full = subparsers.add_parser("full", parents=[common],
        help="Export posts, comments, likes and passive publication reports.",
        description="Posts, comments, likes and passive publication reports for the same selected posts. Online phases share the Chrome session. full.html links to all enabled reports and opens after collection.")
    for command in (posts, full):
        command.add_argument('--highlights', action=argparse.BooleanOptionalAction, default=None, help='Export available Highlights albums with additional authenticated page requests (default: enabled in full).')
        command.add_argument('--highlight-limit', type=nonnegative_int, default=0, metavar='N', help='Maximum Highlights albums, independent of post/date selection (0 = all).')
        command.add_argument("--download-videos", action="store_true", help="Download videos; by default, save only photos and video covers.")
        command.add_argument("--batch-size", type=positive_int, default=9, metavar="N", help="Cards loaded per scroll batch in the post archive (default: 9). Does not change request batching.")
        command.add_argument("--headful", action="store_true", help="Keep Chrome visible throughout post collection.")
    for command in (comments, likes):
        command.add_argument("--sessionid", default=None, help="Instagram sessionid cookie for collection without Chrome. A saved session is used automatically.")
    for command in (comments, full):
        command.add_argument("--comment-delay", type=nonnegative_delay, default=2.0, metavar="SECONDS", help="Delay between comment requests (default: 2 seconds).")
    for command in (likes, full):
        command.add_argument("--like-delay", type=nonnegative_delay, default=2.0, metavar="SECONDS", help="Delay between like-list requests, including automatic rechecks (default: 2 seconds).")
        command.add_argument('--recheck-likes', action=argparse.BooleanOptionalAction, default=True, help='Immediately recheck a newly incomplete like list once (default: enabled). --no-recheck-likes avoids the extra request; coverage remains partial.')
        command.add_argument("--retry-missing", action="store_true", help=argparse.SUPPRESS)
    highlights_command = subparsers.add_parser('highlights', help='Export Highlights albums only.', description='Save available Highlights albums as local media and highlights.html. Video covers by default; --download-videos saves videos. Cached complete albums are reused unless --refresh is set.')
    highlights_command.add_argument('username', type=instagram_username)
    highlights_command.add_argument('--output-dir', default=None)
    highlights_command.add_argument('--browser-profile-dir', default=str(DEFAULT_PROFILE_DIR))
    highlights_command.add_argument('--highlight-limit', type=nonnegative_int, default=0, metavar='N', help='Maximum albums (0 = all).')
    for option, help_text in [('offline','Rebuild from saved Highlights without HTTP requests.'),('refresh','Recheck available albums and replace selected cached metadata.'),('no-open','Do not open the finished report.'),('headful','Keep Chrome visible.'),('download-videos','Save videos instead of only their covers.'),('quiet','Show only warnings and errors.'),('verbose','Show debug logs.')]:
        highlights_command.add_argument('--'+option, action='store_true', help=help_text)

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:2] == ["all", "oldest"]:
        parser.error("Choose one command. For the first 100 posts: igdump posts @kharlamova_alena --oldest --limit 100")
    legacy = arguments[0] if arguments else None
    if legacy in ("all", "oldest"):
        arguments[0] = "posts"
        if legacy == "oldest":
            arguments.append("--oldest")
    elif legacy == "likers":
        arguments[0] = "likes"
    parsed = parser.parse_args(arguments)
    for flag in ('text_report','highlights'):
        if getattr(parsed,flag) is None:
            setattr(parsed,flag,parsed.mode in ('full','highlights') if flag=='highlights' else parsed.mode=='full')
    for flag in REPORTS:
        if getattr(parsed, flag) is None:
            setattr(parsed, flag, parsed.mode == "full")
    if legacy == "oldest" and parsed.limit <= 0:
        parser.error("The legacy oldest command requires --limit N greater than 0. Use posts --oldest for an unlimited selection.")
    if parsed.after and parsed.before and parsed.after > parsed.before:
        parser.error("--after must not be later than --before.")
    if parsed.offline and (parsed.refresh or parsed.dry_run or parsed.retry_missing):
        parser.error("--offline cannot be combined with --refresh, --dry-run or --retry-missing.")
    if parsed.offline and (parsed.headful or parsed.download_videos):
        parser.error("--offline cannot be combined with --headful or --download-videos.")
    return parsed


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "export"


def build_job_slug(args: argparse.Namespace) -> str:
    if args.mode=='highlights':
        return f'{safe_slug(args.username)}-highlights-{args.highlight_limit}'
    if args.mode == "all":
        slug = f"{safe_slug(args.username)}-all"
    else:
        limit = getattr(args, "limit", 0)
        slug = f"{safe_slug(args.username)}-{args.mode}-{limit}"
    if getattr(args, "oldest", False) and args.mode != "oldest":
        slug += "-oldest"
    for name in ("after", "before"):
        value = getattr(args, name, None)
        if value:
            slug += f"-{name}-{value.isoformat()}"
    return slug


def format_count(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ")


def ensure_output_dir(base: str | None, args: argparse.Namespace) -> Path:
    if base:
        root = Path(base).expanduser().resolve()
    else:
        root = (Path.cwd() / "exports" / build_job_slug(args)).resolve()
        legacy_mode = None
        if args.mode == "posts":
            if args.oldest and args.limit:
                legacy_mode = "oldest"
            elif not args.oldest and not args.limit:
                legacy_mode = "all"
        elif args.mode == "likes" and not args.oldest:
            legacy_mode = "likers"
        if legacy_mode and not root.exists():
            legacy = argparse.Namespace(**vars(args))
            legacy.mode = legacy_mode
            previous = (Path.cwd() / "exports" / build_job_slug(legacy)).resolve()
            if previous.exists():
                root = previous
    root.mkdir(parents=True, exist_ok=True)
    for filename in ('.post-links.json', '.export-state.json'):
        saved = read_json(root / filename) or {}
        owner = (saved.get('job') or {}).get('username')
        if owner and owner.lower() != args.username.lower():
            raise ExportError(f'The output folder belongs to @{owner}; choose a different --output-dir.')
    if args.mode in ("posts", "full"):
        (root / "media").mkdir(exist_ok=True)
    return root


def export_state_path(output_dir: Path) -> Path:
    return output_dir / ".export-state.json"


def post_links_path(output_dir: Path) -> Path:
    return output_dir / ".post-links.json"


def save_export_state(
    output_dir: Path,
    args: argparse.Namespace,
    profile: dict[str, Any],
    avatar_src: str | None,
    post_records: list[dict[str, Any]],
    completed: bool,
) -> None:
    previous = read_json(export_state_path(output_dir)) or {}
    archive = {}
    if (previous.get('job') or {}).get('username') == args.username:
        archive.update({record['shortcode']: record for record in previous.get('archive_records', previous.get('post_records', []))})
    archive.update({record['shortcode']: record for record in post_records})
    write_json(
        export_state_path(output_dir),
        {
            "version": 4,
            "job": {
                "username": args.username,
                "mode": args.mode,
                "limit": getattr(args, "limit", None),
                "oldest": getattr(args, "oldest", False),
                "after": args.after.isoformat() if args.after else None,
                "before": args.before.isoformat() if args.before else None,
            },
            "profile": profile,
            "avatar_src": avatar_src,
            "post_records": post_records,
            "archive_records": list(archive.values()),
            "completed": completed,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def load_export_state(output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = read_json(export_state_path(output_dir))
    if not payload:
        return {}
    job = payload.get("job", {})
    mode = job.get("mode")
    oldest = job.get("oldest", mode == "oldest")
    limit = job.get("limit")
    if mode in ("all", "oldest"):
        if mode == "all":
            limit = 0
        mode = "posts"
    if (
        job.get("username") != args.username
        or mode != args.mode
        or limit != getattr(args, "limit", None)
        or oldest != getattr(args, "oldest", False)
        or job.get("after") != (args.after.isoformat() if args.after else None)
        or job.get("before") != (args.before.isoformat() if args.before else None)
    ):
        return {}
    payload['post_records'] = payload.get('archive_records', payload.get('post_records', []))
    return payload


def save_post_links(output_dir: Path, args: argparse.Namespace, links: list[str], total: int | None, completed: bool, cursor: str | None = None) -> None:
    write_json(
        post_links_path(output_dir),
        {
            "version": 2,
            "job": {
                "username": args.username,
                "mode": "profile-links",
            },
            "links": links,
            "total": total,
            "completed": completed,
            "cursor": cursor,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def load_post_links(output_dir: Path, username: str) -> dict[str, Any]:
    payload = read_json(post_links_path(output_dir))
    if not payload:
        return {"links": [], "total": None, "completed": False, "cursor": None}
    job = payload.get("job", {})
    if job.get("username") != username:
        return {"links": [], "total": None, "completed": False, "cursor": None}
    links = payload.get("links", [])
    return {
        "links": links if isinstance(links, list) else [],
        "total": payload.get("total"),
        "completed": payload.get("version") == 2 and bool(payload.get("completed")) and payload.get("cursor") is None,
        "cursor": payload.get("cursor"),
        "selection": payload.get('selection'),
    }


def selection_key(args):
    return {key: str(getattr(args, key, None)) for key in ('limit', 'oldest', 'after', 'before')}


def timeline_satisfies_selection(output_dir, args):
    state = read_json(post_links_path(output_dir)) or {}
    return bool(state.get('completed') or (state.get('selection') == selection_key(args)))


def limited_selection_ready(session, args, links):
    """Only stop early with explicit pin flags and ordered, dated ordinary posts."""
    if not args.limit or args.oldest:
        return False
    nodes = getattr(session, '_igdump_timeline_nodes', {})
    if not isinstance(nodes, dict):
        return False
    observed = [nodes.get(parse_shortcode_from_url(url)) for url in links]
    if any(node is None or node.get('pinned') is None for node in observed):
        return False
    # Unknown pin status/order is a reason to keep traversing, not a guess.
    ordinary = [node for node in observed if node and node.get('pinned') is False]
    stamps = [node['stamp'] for node in ordinary]
    if not stamps or stamps != sorted(stamps, reverse=True):
        return False
    eligible = [node for node in ordinary if date_matches(datetime.fromtimestamp(node['stamp'], UTC).astimezone().date(), args.after, args.before)]
    return len(eligible) >= args.limit


def existing_binary_path(destination_base: Path) -> Path | None:
    matches = sorted(destination_base.parent.glob(f"{destination_base.name}.*"))
    return next((path for path in matches if path.suffix != ".part" and path.is_file() and path.stat().st_size > 0), None)


def extension_for_response(url: str, response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if content_type:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix else ".jpg"


def download_binary(session: requests.Session, url: str, destination_base: Path) -> Path:
    existing = existing_binary_path(destination_base)
    if existing is not None:
        return existing
    if urlparse(url).scheme != 'https':
        raise ExportError('Media downloads require an HTTPS URL.')
    if isinstance(session, requests.Session):
        media_session = getattr(session, '_igdump_media_session', None)
        if media_session is None:
            media_session = requests.Session()
            # A truthy no-op auth object disables implicit .netrc auth while
            # preserving the user's proxy environment.
            class NoMediaAuth(requests.auth.AuthBase):
                def __call__(self, request):
                    return request
            media_session.auth = NoMediaAuth()
            media_session.headers['User-Agent'] = IG_MOBILE_USER_AGENT
            def secure_redirect(response, *args, **kwargs):
                if response.is_redirect and urlparse(urljoin(response.url, response.headers.get('Location', ''))).scheme != 'https':
                    raise ExportError('Media redirect to HTTP was blocked.')
            media_session.hooks['response'].append(secure_redirect)
            _patch_session_for_logging(media_session)
            session._igdump_media_session = media_session
        session = media_session
    destination_base.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        response = None
        partial: Path | None = None
        try:
            response = session.get(url, timeout=30, stream=True)
            if isinstance(response.url, str) and urlparse(response.url or url).scheme != 'https':
                raise ExportError('The media server redirected to an insecure URL.')
            response.raise_for_status()
            if isinstance(response.headers.get('Content-Type'), str) and response.headers.get('Content-Type', '').split(';')[0].lower() in ('text/html', 'application/json'):
                raise ExportError('The media URL returned a web page or API response instead of media.')
            suffix = extension_for_response(url, response)
            destination = destination_base.with_suffix(suffix)
            partial = destination.with_suffix(destination.suffix + ".part")
            started = time.monotonic()
            written = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
                        written += len(chunk)
            partial.replace(destination)
            _debug_event('media.saved', destination.name, bytes=written, body_elapsed_ms=int((time.monotonic()-started)*1000))
            return destination
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 3:
                logger.warning("Media download failed on attempt %s/3: %s. Retrying ...", attempt, exc)
            else:
                break
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
            if response is not None:
                response.close()

    raise ExportError(f"Could not download media after 3 attempts: {last_error}")


def parse_shortcode_from_url(url: str) -> str:
    match = re.search(r"/(?:p|reel|tv)/([^/?#]+)/?", url)
    if not match:
        raise ExportError(f"Could not parse shortcode from {url}")
    return match.group(1)


def normalized_post_url(url: str) -> str:
    shortcode = parse_shortcode_from_url(url)
    path_match = re.search(r"/(p|reel|tv)/", url)
    kind = path_match.group(1) if path_match else "p"
    return f"{IG_BASE_URL}/{kind}/{shortcode}/"


def assert_page_is_usable(page: Page, expected_url: str) -> None:
    final_url = page.url
    if "/accounts/login" in final_url:
        raise ExportError(
            f"Instagram redirected to login while opening {expected_url}. The browser profile needs a fresh login."
        )
    if "/auth_platform/" in final_url:
        raise ExportError(
            f"Instagram requires checkpoint verification before accessing {expected_url}. Open {final_url} in Chrome, "
            "complete the challenge, then rerun."
        )


def preflight_instagram_access(browser: InstagramBrowser) -> None:
    page = browser.open_page(IG_BASE_URL)
    try:
        assert_page_is_usable(page, IG_BASE_URL)
        body_text = page.text_content("body") or ""
        if "Please wait a few minutes before you try again" in body_text:
            raise InstagramRateLimitError(
                "Instagram is temporarily rate-limiting this browser session before export start. Wait and retry later."
            )
    finally:
        page.close()


def _scrape_profile_html(username: str, session=None) -> tuple[dict[str, Any], list[str], str | None, str]:
    """Scrape profile info from Instagram's public HTML page (no API call).

    Falls back to parsing the server-rendered <script> data and <meta> tags.
    """
    _debug_event("profile.source", "HTML scrape", username=username)
    sess = session if session is not None else _make_session()
    if session is None:
        sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    resp = sess.get(f"{IG_BASE_URL}/{username}/", timeout=30)
    raise_if_rate_limited(resp, "profile HTML")
    resp.raise_for_status()
    if any(path in urlparse(resp.url).path for path in ("/accounts/login", "/auth_platform/")):
        raise ProfileHTMLUnavailableError("Public profile HTML redirected to login or verification.")
    text = resp.text
    _debug_event("profile.html_size", f"HTML {len(text)} bytes")

    # 1. Try window.__INITIAL_STATE__ (React embedded data)
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});", text, re.DOTALL)
    if m:
        _debug_event("profile.init_state", "__INITIAL_STATE__ found")
        try:
            data = json.loads(m.group(1))
            feed = data.get("xdt_api__v1__feed__user_timeline_graphql_connection") or {}
            user_obj = feed.get("user") or {}
            edges = feed.get("edges") or []
            links = []
            link_ids: set[str] = set()
            for e in edges:
                node = e.get("node") or {}
                sc = node.get("code") or node.get("shortcode")
                if sc and sc not in link_ids:
                    link_ids.add(sc)
                    links.append(f"{IG_BASE_URL}/p/{sc}/")
            pi = feed.get("page_info") or {}

            # Try multiple JSON paths for user_id
            uid = (
                user_obj.get("id")
                or (data.get("user") or {}).get("id")
                or (data.get("users") or {}).get(username, {}).get("id")
                or user_obj.get("pk")
                or ""
            )

            if not str(uid).isdigit() or (user_obj.get("username") and user_obj["username"].casefold() != username.casefold()):
                uid = ""
            uid = str(uid)
            profile = {
                "username": user_obj.get("username") or username,
                "full_name": user_obj.get("full_name") or username,
                "biography": user_obj.get("biography"),
                "mediacount": (user_obj.get("edge_owner_to_timeline_media") or {}).get("count"),
                "followers": (user_obj.get("edge_followed_by") or {}).get("count"),
                "followees": (user_obj.get("edge_follow") or {}).get("count"),
                "avatar_url": user_obj.get("profile_pic_url_hd") or user_obj.get("profile_pic_url") or "",
            }
            profile.update(profile_metadata_from_html(text, username))
            _debug_event("profile.init_state_ok", f"links={len(links)}, uid={uid!r}", links_count=len(links), uid=uid or None)
            if not uid:
                uid = _extract_user_id_from_html(text, username) or _resolve_user_id_via_search(username, session=sess)
            return profile, links, pi.get("end_cursor"), uid
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            _debug_event("profile.json_error", f"__INITIAL_STATE__ parse failed: {exc}")

    _debug_event("profile.init_state", "__INITIAL_STATE__ not found, using regex fallback")

    # 2. Fallback: extract shortcodes and user_id via regex
    links = list(dict.fromkeys(
        f"{IG_BASE_URL}/p/{sc}/"
        for sc in re.findall(r'(?:"code"|"shortcode")\s*:\s*"([A-Za-z0-9_-]{11})"', text)
        if sc
    ))
    if not links:
        links = list(dict.fromkeys(
            f"{IG_BASE_URL}{m}" for m in re.findall(r'href="(/p/[^/]+/)', text)
        ))
    title_m = re.search(r'<title>([^<]+)', text)
    raw = title_m.group(1) if title_m else username
    display_name = raw.split("(")[0].strip() if "(" in raw else raw
    scraped_username = username

    uid = _extract_user_id_from_html(text, username)

    profile = {
        "username": scraped_username,
        "full_name": display_name,
        "biography": None,
        "mediacount": None,
        "followers": None,
        "followees": None,
        "avatar_url": "",
    }
    profile.update(profile_metadata_from_html(text, username))
    _debug_event("profile.regex_fallback", f"links={len(links)}, uid={uid!r}", links_count=len(links), uid=uid or None)

    # 3. If still no user_id, try search API
    if not uid:
        uid = _resolve_user_id_via_search(username, session=sess)

    return profile, links, None, uid


def profile_metadata_from_user(user, username):
    if not isinstance(user, dict) or str(user.get("username", "")).casefold() != username.casefold():
        return {}
    result = {}
    for key in ("full_name", "biography"):
        if isinstance(user.get(key), str):
            result[key] = user[key]
    avatar = user.get("hd_profile_pic_url_info")
    avatar = avatar.get("url") if isinstance(avatar, dict) else None
    avatar = avatar or user.get("profile_pic_url_hd") or user.get("profile_pic_url")
    if isinstance(avatar, str) and urlparse(avatar).scheme in ("https", "http"):
        result["avatar_url"] = avatar
    for dest, direct, nested in (("mediacount", "media_count", "edge_owner_to_timeline_media"),
                                 ("followers", "follower_count", "edge_followed_by"),
                                 ("followees", "following_count", "edge_follow")):
        value = user.get(direct)
        if value is None and isinstance(user.get(nested), dict):
            value = user[nested].get("count")
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool):
            result[dest] = value
    return result


def capture_browser_profile(session, username, response):
    endpoint = urlparse(response.url)
    if endpoint.hostname not in ('www.instagram.com','instagram.com') or endpoint.path.rstrip('/') not in ('/api/graphql','/graphql/query') or response.status != 200:
        return
    try:
        payload = response.json()
        data = payload.get('data') or {}
        user = data.get('user')
        metadata = profile_metadata_from_user(user, username)
        if metadata:
            saved = getattr(session, '_igdump_browser_profile_metadata', {})
            if not isinstance(saved, dict): saved = {}
            saved.update(metadata)
            if all(key in metadata for key in ('followers','followees','mediacount')):
                saved['observed_at'] = datetime.now(UTC).isoformat()
            saved['user_id'] = str(user.get('pk') or user.get('id') or '')
            session._igdump_browser_profile_metadata = saved
        tray = data.get('highlights')
        if not payload.get('errors') and isinstance(tray, dict) and isinstance(tray.get('edges'), list):
            nodes = [edge['node'] for edge in tray['edges'] if isinstance(edge.get('node'), dict)]
            headers = response.request.all_headers()
            referer = urlparse(headers.get('referer','')).path.strip('/').casefold()
            form={key:values[-1] for key,values in parse_qs(response.request.post_data or '').items()}
            variables=json.loads(form.get('variables','{}'))
            owner=str(variables.get('user_id') or '')
            metadata=getattr(session,'_igdump_browser_profile_metadata',{})
            target_uid=metadata.get('user_id') if isinstance(metadata,dict) else None
            if not nodes and owner.isdecimal():
                pending=getattr(session,'_igdump_empty_highlight_trays',{})
                if not isinstance(pending,dict):pending={}
                pending[owner]=tray.get('page_info') or {}
                session._igdump_empty_highlight_trays=pending
            if (nodes and all(str((node.get('user') or {}).get('username','')).casefold() == username.casefold() for node in nodes)) or (not nodes and (referer == username.casefold() or owner and owner==target_uid)):
                session._igdump_highlight_nodes = nodes
                session._igdump_highlight_page_info = tray.get('page_info') or {}
    except (ValueError,TypeError,AttributeError,PlaywrightError):
        return


def profile_metadata_from_html(text, username):
    """Read target-account fields already embedded in public or browser HTML."""
    result = {}
    def visit(value):
        if isinstance(value, dict):
            result.update(profile_metadata_from_user(value, username))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                visit(json.loads(value))
            except (ValueError, RecursionError):
                pass
    for script in re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL):
        try:
            visit(json.loads(script))
        except (ValueError, RecursionError):
            pass
    for variant in (text, text.replace('\\"', '"')):
        for candidate in re.findall(r'\{[^{}]*"username"\s*:\s*"' + re.escape(username) + r'"[^{}]*\}', variant, re.I):
            try:
                result.update(profile_metadata_from_user(json.loads(candidate), username))
            except ValueError:
                pass
    class ProfileMeta(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and attrs.get("property", "").lower() == "og:image" and not result.get("avatar_url"):
                value = attrs.get("content") or ""
                if urlparse(value).scheme in ("http", "https"):
                    result["avatar_url"] = value
    ProfileMeta().feed(text)
    return result


def _extract_user_id_from_html(text: str, username: str | None = None) -> str:
    """Try multiple methods to find the numeric user id in HTML."""
    # Method 1: ld+json structured data (most reliable)
    for ld_m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', text, re.DOTALL):
        try:
            ld = json.loads(ld_m.group(1))
            if isinstance(ld, dict):
                val = ld.get("identifier") or ""
                if isinstance(val, dict):
                    val = val.get("value", "")
                if str(val).isdigit():
                    return str(val)
        except (json.JSONDecodeError, TypeError):
            continue

    # Embedded data can include the logged-in viewer and media IDs. Match the
    # requested profile before accepting a generic id/pk field.
    def find_user(value):
        if isinstance(value, dict):
            if username and str(value.get("username", "")).casefold() == username.casefold():
                uid = value.get("id") or value.get("pk") or ""
                if str(uid).isdigit():
                    return str(uid)
            for child in value.values():
                found = find_user(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_user(child)
                if found:
                    return found
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                return find_user(json.loads(value))
            except (ValueError, RecursionError):
                pass
        return ""
    for script in re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL):
        try:
            found = find_user(json.loads(script))
            if found:
                return found
        except (ValueError, RecursionError):
            pass
    if username:
        # Some scripts contain a JavaScript wrapper instead of a JSON document;
        # extract only an object explicitly identifying the requested account.
        for variant in (text, text.replace('\\"', '"')):
            for candidate in re.findall(r'\{[^{}]*"username"\s*:\s*"' + re.escape(username) + r'"[^{}]*\}', variant, re.I):
                try:
                    found = find_user(json.loads(candidate))
                    if found:
                        return found
                except ValueError:
                    pass
            ids = set(re.findall(r'(?:"profile_id"\s*:\s*"?(\d+)|profilePage_(\d+))', variant))
            ids = {a or b for a, b in ids}
            if len(ids) == 1 and username.casefold() in variant.casefold():
                return ids.pop()
        return ""

    # Legacy callers without a target profile.
    patterns = [
        r'"pk":\s*(\d{5,})',
        r'"id":\s*"(\d{5,})"',
        r'"user_id":\s*"(\d{5,})"',
        r'"userId":\s*"(\d{5,})"',
        r'"owner":\s*\{\s*"id":\s*"(\d{5,})"',
        r'profilePage_(\d{5,})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def _resolve_user_id_via_search(username: str, session=None) -> str:
    """Resolve numeric user_id via Instagram's search/topsearch API.

    This endpoint has different rate-limit characteristics from web_profile_info.
    """
    sess = session if session is not None else _make_session()
    if session is None:
        sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    for attempt in range(2):
        try:
            resp = sess.get(
                "https://www.instagram.com/web/search/topsearch/",
                params={"query": username},
                timeout=15,
            )
            raise_if_rate_limited(resp, "profile ID search")
            resp.raise_for_status()
            data = resp.json()
            users = data.get("users") or []
            for entry in users:
                user_data = entry.get("user") or {}
                if user_data.get("username", "").lower() == username.lower():
                    pk = user_data.get("pk") or ""
                    _debug_event("search.ok", f"user_id={pk}", uid=str(pk))
                    return str(pk)
            _debug_event("search.not_found", f"username @{username} not in search results")
            return ""
        except requests.RequestException as exc:
            _debug_event("search.error", f"search API failed: {exc}")
            if attempt == 0 and retryable_request_error(exc):
                _safe_sleep(3)
                continue
            return ""
    return ""


def fetch_profile_info(session: requests.Session, username: str) -> tuple[dict[str, Any], list[str], str | None, str]:
    url = f"{IG_BASE_URL}/api/v1/users/web_profile_info/?username={username}"
    try:
        response = session.get(url, timeout=(10, 20))
    except requests.exceptions.SSLError as exc:
        transport = getattr(session, "_igdump_profile_transport", None)
        if transport is None or not re.search(r"UNEXPECTED_EOF|SSLEOFError", str(exc), re.I):
            raise
        logger.warning("Profile TLS connection closed unexpectedly. Trying Playwright transport once ...")
        _debug_event("http.send", "Profile lookup via Playwright", method="GET", url=url)
        try:
            native = transport.get(url, headers=dict(session.headers), timeout=20000,
                                   fail_on_status_code=False, max_redirects=0)
            try:
                response = requests.Response()
                response.status_code = native.status
                response.url = native.url
                response.headers.update(native.headers)
                response._content = native.body()
                response._content_consumed = True
                _debug_event("http.done", "Profile lookup via Playwright", status=native.status, url=url)
            finally:
                native.dispose()
        except PlaywrightError as fallback:
            raise ExportError("Profile connection failed through both Requests (SSL EOF) and Playwright. "
                              "Check the connection to Instagram and retry later.") from fallback
    if response.status_code == 429:
        response.close()
        logger.warning("API rate limited (429). Trying HTML scrape ...")
        _debug_event("profile.fallback", "API 429 → HTML scrape", username=username)
        # Preserve the original fallback's public session and desktop headers.
        return _scrape_profile_html(username)
    if response.status_code == 400:
        logger.warning("Profile API returned 400. Trying authenticated profile HTML once ...")
        return _scrape_profile_html(username, session=session)
    response.raise_for_status()
    payload = response.json()
    user = payload.get("data", {}).get("user")
    if not user:
        raise ExportError(f"Could not resolve profile @{username}. It may be private or unavailable.")
    _debug_event("profile.source", "API success", username=username)

    media = user.get("edge_owner_to_timeline_media") or {}
    edges = media.get("edges") or []
    remember_post_dates(session, [edge.get("node") or {} for edge in edges])
    initial_links = []
    for edge in edges:
        node = edge.get("node") or {}
        shortcode = node.get("shortcode")
        if shortcode:
            initial_links.append(f"{IG_BASE_URL}/p/{shortcode}/")

    profile = {
        "username": user.get("username") or username,
        "full_name": user.get("full_name") or "Unnamed profile",
        "biography": user.get("biography"),
        "mediacount": media.get("count"),
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "followees": (user.get("edge_follow") or {}).get("count"),
        "avatar_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
    }
    profile.update(profile_metadata_from_user(user, username))
    profile['observed_at'] = datetime.now(UTC).isoformat()
    end_cursor = media.get("page_info", {}).get("end_cursor")
    user_id = str(user.get("id") or user.get("pk") or "")
    return profile, initial_links, end_cursor, user_id


def capture_browser_timeline(session, username, response):
    """Keep the actual browser query and its response, scoped to this profile."""
    endpoint = urlparse(response.url)
    if endpoint.hostname not in ('www.instagram.com', 'instagram.com') or endpoint.path.rstrip('/') not in ('/graphql/query', '/api/graphql'):
        return
    try:
        form = {key: values[-1] for key, values in parse_qs(response.request.post_data or "", keep_blank_values=True).items()}
        variables = json.loads(form.get("variables", "{}"))
        if variables.get("username", "").casefold() != username.casefold():
            return
        if response.status == 429:
            session._igdump_browser_timeline_limited = True
            return
        if response.status != 200:
            return
        payload = response.json()
        media = (payload.get("data") or {}).get("xdt_api__v1__feed__user_timeline_graphql_connection")
        if not isinstance(media, dict) or not form.get("doc_id"):
            return
        metadata = getattr(session, "_igdump_browser_profile_metadata", {})
        for user in [media.get("user"), *((edge.get("node") or {}).get("user") for edge in media.get("edges", []))]:
            metadata.update(profile_metadata_from_user(user, username))
        session._igdump_browser_profile_metadata = metadata
        pages = getattr(session, "_igdump_timeline_pages", {})
        pages[variables.get("after")] = payload
        session._igdump_timeline_pages = pages
        templates = getattr(session, "_igdump_timeline_templates", {})
        templates["next" if variables.get("after") else "first"] = {
            "url": response.url,
            "form": form, "variables": variables,
            "headers": {key: value for key, value in response.request.all_headers().items()
                        if key.lower() not in ("cookie", "host", "content-length", "accept-encoding")},
        }
        session._igdump_timeline_templates = templates
    except (ValueError, TypeError, PlaywrightError):
        return


def observe_browser_pagination(session, username, after_cursor):
    """Let Instagram generate its current pagination query instead of guessing it."""
    page = getattr(session, "_igdump_timeline_page", None)
    if page is None:
        return None
    def capture(response):
        capture_browser_profile(session, username, response)
        capture_browser_timeline(session, username, response)
    page.context.on("response", capture)
    try:
        for _ in range(8):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1000)
            if getattr(session, "_igdump_browser_timeline_limited", False):
                raise InstagramRateLimitError("Instagram rate limited browser timeline pagination (429). Cached progress is preserved.")
            pages = getattr(session, "_igdump_timeline_pages", {})
            if after_cursor in pages:
                return _parse_timeline_payload(session, username, pages.pop(after_cursor))
            if "next" in getattr(session, "_igdump_timeline_templates", {}):
                break
        return None
    finally:
        page.context.remove_listener("response", capture)


def resolve_profile_for_posts(session, browser, username, force_browser=False):
    session._igdump_browser = browser
    try:
        if force_browser:
            profile, links, cursor, uid = {'username': username}, [], None, ''
        else:
            profile, links, cursor, uid = fetch_profile_info(session, username)
    except (ProfileHTMLUnavailableError, requests.ConnectionError, requests.Timeout) as exc:
        if is_rate_limited(exc):
            raise
        if isinstance(exc, requests.exceptions.SSLError) and not re.search(r"UNEXPECTED_EOF|SSLEOFError", str(exc), re.I):
            raise
        logger.warning("%s Trying the logged-in Chrome profile page ...", exc)
        profile, links, cursor, uid = {"username": username}, [], None, ""
    if uid:
        return profile, links, cursor, uid
    logger.warning("Profile ID unavailable in public HTML. Reading the logged-in Chrome profile page ...")
    context = browser.require_context()
    def capture(response):
        capture_browser_profile(session, username, response)
        capture_browser_timeline(session, username, response)
    context.on("response", capture)
    page = browser.open_page(f"{IG_BASE_URL}/{username}/")
    try:
        page.wait_for_timeout(2500)
        assert_page_is_usable(page, f"{IG_BASE_URL}/{username}/")
        if "Please wait a few minutes before you try again" in (page.text_content("body") or ""):
            raise InstagramRateLimitError("Instagram limited the authenticated profile page. Cached data is preserved.")
        for attempt in range(10):
            if getattr(session, '_igdump_browser_timeline_limited', False) is True:
                raise InstagramRateLimitError('Instagram rate limited the browser profile timeline (429). Cached progress is preserved.')
            pages = getattr(session, '_igdump_timeline_pages', {})
            if isinstance(pages, dict) and pages:
                break
            if attempt == 0:
                page.mouse.wheel(0, 700)
            page.wait_for_timeout(500)
        for _ in range(6):
            metadata = getattr(session, '_igdump_browser_profile_metadata', {})
            if isinstance(metadata, dict) and all(key in metadata for key in ('followers','followees','mediacount','biography')):
                break
            page.wait_for_timeout(500)
        html = page.content()
        uid = _extract_user_id_from_html(html, username)
        if not uid:
            uid = _resolve_user_id_via_search(username, session=session)
        if not uid:
            raise ExportError(f"Could not identify @{username}: public HTML and the logged-in Chrome page supplied no profile ID.")
        empty_trays=getattr(session,'_igdump_empty_highlight_trays',{})
        if isinstance(empty_trays,dict) and uid in empty_trays:
            session._igdump_highlight_nodes=[]
            session._igdump_highlight_page_info=empty_trays[uid]
        metadata = getattr(session, "_igdump_browser_profile_metadata", {})
        profile.update(metadata)
        profile.update(profile_metadata_from_html(html, username))
        if not profile.get("avatar_url"):
            avatar = page.evaluate("""username => {
                const image = [...document.querySelectorAll('img[alt]')].find(img =>
                    img.alt.toLowerCase() === `${username.toLowerCase()}'s profile picture`);
                return image ? image.currentSrc || image.src : null;
            }""", username)
            if isinstance(avatar, str) and urlparse(avatar).scheme in ("http", "https"):
                profile["avatar_url"] = avatar
        _debug_event("profile.browser_ok", "Profile ID from authenticated page", username=username, uid=uid)
        templates = getattr(session, "_igdump_timeline_templates", None)
        if isinstance(templates, dict) and templates:
            session._igdump_timeline_page = page
        return profile, links, cursor, uid
    finally:
        context.remove_listener("response", capture)
        if getattr(session, "_igdump_timeline_page", None) is not page:
            page.close()


def unique_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _doc_id_guard(payload: dict[str, Any], context: str) -> None:
    """Check if Instagram responded with a meaningful data shape."""
    errors = payload.get("errors") or payload.get("error")
    if errors:
        logger.warning("Instagram API error for %s (doc_id may be stale): %s", context, errors)
        raise ExportError(f"Instagram API error during {context}: {errors}")


def fetch_user_timeline_rest(
    session: requests.Session,
    user_id: str,
    max_id: str | None,
) -> tuple[list[str], str | None, bool]:
    """Fetch user timeline page via the public REST API (no GraphQL doc_id needed).

    GET /api/v1/feed/user/{user_id}/?count=12&max_id=...
    """
    params: dict[str, Any] = {"count": 12}
    if max_id:
        params["max_id"] = max_id

    resp = session.get(
        f"{IG_BASE_URL}/api/v1/feed/user/{user_id}/",
        params=params,
        timeout=30,
        allow_redirects=False,
    )
    if resp.status_code in (302, 303, 307, 401):
        raise ExportError(
            "Instagram redirected to login on REST timeline — your sessionid is expired or invalid."
        )
    raise_if_rate_limited(resp, "REST timeline")
    data = api_payload(resp, 'REST timeline', 'items')

    items = data.get("items") or []
    remember_post_dates(session, items)
    links = []
    for item in items:
        code = item.get("code")
        if code:
            links.append(f"{IG_BASE_URL}/p/{code}/")

    return links, data.get("next_max_id"), bool(data.get("more_available", False))


def _try_graphql_timeline(
    session: requests.Session,
    username: str,
    after_cursor: str | None,
) -> tuple[list[str], str | None, bool]:
    """Try GraphQL timeline; raises ExportError on 403 or API error."""
    variables: dict[str, Any] = {
        "data": {
            "count": 12,
            "include_relationship_info": True,
            "latest_besties_reel_media": True,
            "latest_reel_media": True,
        },
        "username": username,
        "first": 12,
        "__relay_internal__pv__PolarisFeedShareMenurelayprovider": False,
    }
    if after_cursor:
        variables["after"] = after_cursor

    cached_pages = getattr(session, "_igdump_timeline_pages", {})
    if not isinstance(cached_pages, dict):
        cached_pages = {}
    browser = getattr(session, '_igdump_browser', None)
    templates = getattr(session, '_igdump_timeline_templates', {})
    if (after_cursor is None and None not in cached_pages and not templates
            and isinstance(session, requests.Session) and browser is not None
            and not getattr(session, '_igdump_browser_timeline_bootstrapped', False)):
        session._igdump_browser_timeline_bootstrapped = True
        # A profile API success can supply an ID without observing the actual
        # timeline query. Obtain that query through Chrome before guessing one.
        resolve_profile_for_posts(session, browser, username, force_browser=True)
        cached_pages = getattr(session, '_igdump_timeline_pages', {})
    if after_cursor in cached_pages:
        payload = cached_pages.pop(after_cursor)
        return _parse_timeline_payload(session, username, payload)
    templates = getattr(session, "_igdump_timeline_templates", {})
    if not isinstance(templates, dict):
        templates = {}
    if after_cursor and "next" not in templates and getattr(session, "_igdump_timeline_page", None) is not None:
        logger.info("Reading the pagination query generated by the logged-in Chrome page ...")
        observed = observe_browser_pagination(session, username, after_cursor)
        if observed is not None:
            return observed
        templates = getattr(session, "_igdump_timeline_templates", {})
        if "next" not in templates:
            raise ExportError("The Chrome profile page did not supply a pagination query.")
    template = templates.get("next" if after_cursor else "first")
    form = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "doc_id": AUTH_PROFILE_DOC_ID,
        "server_timestamps": "true",
    }
    headers = None
    if template:
        variables = {**template["variables"]}
        if after_cursor:
            variables["after"] = after_cursor
        else:
            variables.pop("after", None)
        form = {**template["form"], "variables": json.dumps(variables, separators=(",", ":"))}
        headers = template["headers"]
    browser_page = getattr(session, "_igdump_timeline_page", None)
    if template and browser_page is not None:
        url = template.get('url', f"{IG_BASE_URL}/graphql/query/")
        _debug_event("http.send", "Timeline through Chrome", method="POST", url=url)
        browser_response = browser_page.evaluate("""async ({url, body, headers}) => {
            const response = await fetch(url, {method: 'POST', body, headers,
                credentials: 'include', redirect: 'manual', signal: AbortSignal.timeout(20000)});
            return {status: response.status, body: await response.text()};
        }""", {"url": url, "body": urlencode(form), "headers": {
            **{key: value for key, value in headers.items() if key.lower().startswith("x-")},
            "content-type": "application/x-www-form-urlencoded",
        }})
        response = requests.Response()
        response.status_code = browser_response["status"]
        response.url = url
        response._content = browser_response["body"].encode("utf-8")
        response._content_consumed = True
        _debug_event("http.done", "Timeline through Chrome", status=response.status_code, url=url)
    else:
        response = session.post(
            f"{IG_BASE_URL}/graphql/query/", data=form, headers=headers,
            timeout=30, allow_redirects=False,
        )
    raise_if_rate_limited(response, "GraphQL timeline")
    if response.status_code in (302, 303, 307):
        raise ExportError("GraphQL endpoint redirected to login — session expired.")
    if response.status_code == 403:
        raise ExportError("GraphQL endpoint returned 403 (stale doc_id or missing csrftoken)")

    response.raise_for_status()
    payload = response.json()
    return _parse_timeline_payload(session, username, payload)


def _parse_timeline_payload(session, username, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise ExportError('Instagram returned an invalid GraphQL timeline response.')
    media = payload['data'].get("xdt_api__v1__feed__user_timeline_graphql_connection")
    errors = payload.get("errors")
    optional_location_errors = (isinstance(errors, list) and errors and isinstance(media, dict)
        and isinstance(media.get("edges"), list) and isinstance(media.get("page_info"), dict)
        and all(isinstance(error, dict) and isinstance(error.get("path"), list)
                and "location" in error["path"]
                and error["path"][-1] in ("profile_pic_url", "profile_pic_url_hd") for error in errors))
    if optional_location_errors:
        logger.warning("Instagram omitted location pictures on %s posts; timeline data is usable.", len(errors))
    else:
        _doc_id_guard(payload, f"pagination for @{username}")
    if not media:
        raise ExportError(
            f"Instagram did not return paginated profile media for @{username}. "
            "The internal GraphQL doc_id may have expired."
        )
    if not isinstance(media, dict) or not isinstance(media.get('edges'), list) or not isinstance(media.get('page_info'), dict):
        raise ExportError('Instagram returned an incomplete GraphQL timeline structure.')
    edges = media.get("edges") or []
    remember_post_dates(session, [edge.get("node") or {} for edge in edges])
    links = []
    for edge in edges:
        node = edge.get("node") or {}
        shortcode = node.get("code") or node.get("shortcode")
        if shortcode:
            links.append(f"{IG_BASE_URL}/p/{shortcode}/")
    page_info = media.get("page_info") or {}
    return links, page_info.get("end_cursor"), bool(page_info.get("has_next_page"))


def fetch_authenticated_timeline_page(
    session: requests.Session,
    username: str,
    after_cursor: str | None,
    user_id: str | None = None,
) -> tuple[list[str], str | None, bool]:
    """Fetch timeline page — tries GraphQL first, falls back to REST API.

    The REST fallback requires *user_id* (numeric ID of the profile owner).
    Retries once on transient errors.
    """
    # Cursors belong to the API that produced them. Persist the transport in
    # the saved cursor so resuming a REST scan never sends its token to GraphQL.
    rest_prefix = "igdump-rest:"
    rest_cursor = None
    use_rest = isinstance(after_cursor, str) and after_cursor.startswith(rest_prefix)
    if use_rest:
        rest_cursor = after_cursor[len(rest_prefix):]
    for retry in range(0 if use_rest else 2):
        try:
            _debug_event("timeline.method", "Trying GraphQL", username=username)
            return _try_graphql_timeline(session, username, after_cursor)
        except (ExportError, requests.RequestException) as exc:
            if is_rate_limited(exc):
                raise
            if retry == 0 and retryable_request_error(exc):
                logger.warning("GraphQL failed (%s). Retrying once after 3s ...", exc)
                _safe_sleep(3, jitter=0.3)
                continue
            logger.warning("GraphQL failed (%s). Trying REST fallback ...", exc)
            _debug_event("timeline.fallback", f"GraphQL → REST: {exc}", username=username, user_id=user_id)
            if after_cursor:
                logger.info("Starting REST timeline from its first page; GraphQL cursors cannot be reused. Saved links will be deduplicated.")
            break

    # Fall back to REST if we have user_id
    if user_id:
        for retry in range(2):
            try:
                _debug_event("timeline.method", "Trying REST", user_id=user_id)
                links, cursor, more = fetch_user_timeline_rest(session, user_id, rest_cursor)
                return links, rest_prefix + cursor if cursor else None, more
            except requests.RequestException as exc:
                if is_rate_limited(exc):
                    raise
                if retry == 0 and retryable_request_error(exc):
                    logger.warning("REST timeline failed (%s). Retrying once after 3s ...", exc)
                    _safe_sleep(3, jitter=0.3)
                    continue
                raise

    raise ExportError(
        f"Could not fetch timeline for @{username}: GraphQL failed and no user_id provided for REST fallback."
    )


def collect_post_links(
    session: requests.Session,
    username: str,
    total_available: int,
    output_dir: Path,
    args: argparse.Namespace,
    seed_links: list[str],
    initial_cursor: str | None,
    user_id: str | None = None,
) -> list[str]:
    save_post_metadata(session, output_dir, username)
    cache = load_post_links(output_dir, username) if not getattr(args, "refresh", False) else {"links": [], "completed": False, "cursor": None}
    links = unique_keep_order([*seed_links, *cache["links"]])
    overlap = bool(set(seed_links) & set(cache['links']))
    if cache["completed"] and overlap:
        # A freshly requested head reconnects to the completed saved history.
        save_post_links(output_dir, args, links, total_available, completed=True)
        return links
    if cache["completed"] and not seed_links and initial_cursor is None and (not total_available or len(links) >= total_available):
        logger.info("Loaded %s cached post links from %s", len(links), post_links_path(output_dir))
        return links

    # Determine starting cursor
    cached_cursor: str | None = cache.get("cursor")
    cursor: str | None = cached_cursor if cached_cursor is not None and (not seed_links or overlap) else initial_cursor

    # If we already have links and nowhere to continue — return as-is.
    if cursor is None and links:
        logger.info("No pagination cursor, returning %s cached links", len(links))
        if total_available and len(links) < total_available:
            logger.warning("Collected %s of %s links before cursor ran out", len(links), total_available)
            if getattr(args, "oldest", False):
                raise ExportError("Instagram returned an incomplete timeline without a next page. The earliest posts cannot be identified; retry later.")
        save_post_links(output_dir, args, links, total_available, completed=not (total_available and len(links) < total_available), cursor=None)
        return links

    if cache["links"]:
        logger.info("Resuming pagination from %s cached links, cursor=%s", len(links), "<saved>" if cached_cursor else "<fresh>")

    seen_cursors: set[str] = {cursor} if cursor else set()
    save_post_links(output_dir, args, links, total_available, completed=False, cursor=cursor)
    def finish_selection_if_ready():
        if cursor and limited_selection_ready(session, args, links):
            state = read_json(post_links_path(output_dir))
            state['selection'] = selection_key(args)
            write_json(post_links_path(output_dir), state)
            logger.info('Latest-post selection is complete; the rest of the profile timeline was not requested.')
            return True
        return False
    if finish_selection_if_ready():
        return links

    while True:
        page_links, next_cursor, _has_next = fetch_authenticated_timeline_page(session, username, cursor, user_id=user_id)
        if _has_next and next_cursor is None:
            raise ExportError("Instagram reports another page but returned no cursor. The timeline is incomplete; retry later.")
        if not _has_next:
            next_cursor = None
        if next_cursor is not None and next_cursor in seen_cursors:
            raise ExportError("Instagram repeated the page cursor. Progress is saved; retry later or use --refresh.")
        if next_cursor is not None:
            seen_cursors.add(next_cursor)
        before = len(links)
        links = unique_keep_order(links + page_links)
        new_count = len(links) - before
        cursor = next_cursor

        save_post_links(output_dir, args, links, total_available, completed=False, cursor=cursor)
        save_post_metadata(session, output_dir, username)
        if cache['completed'] and set(page_links) & set(cache['links']):
            save_post_links(output_dir, args, links, total_available, completed=True)
            return links
        if finish_selection_if_ready():
            return links

        if new_count:
            update_progress("Post links", len(links), total_available or None)
        else:
            update_progress("Post links", len(links), total_available or None, "cached page; checking for more")

        if cursor is None:
            break

    if total_available and len(links) < total_available:
        logger.warning("Collected %s of %s links before pagination stopped", len(links), total_available)
    exhausted = cursor is None and not (total_available and len(links) < total_available)
    save_post_links(output_dir, args, links, total_available, completed=exhausted, cursor=cursor)
    if getattr(args, "oldest", False) and (cursor is not None or (total_available and len(links) < total_available)):
        raise ExportError("Could not reach the beginning of the profile. Links are saved; repeat the same command later to continue.")
    return links


def deep_find_media_item(root: Any, shortcode: str) -> dict[str, Any] | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            node_code = node.get("code") or node.get("shortcode")
            media_type = node.get("media_type")
            if node_code == shortcode and isinstance(media_type, int):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def extract_media_from_scripts(page: Page, shortcode: str) -> dict[str, Any] | None:
    scripts = page.locator("script[type='application/json']").all_text_contents()
    marker = f'"{shortcode}"'
    for script in scripts:
        if marker not in script:
            continue
        try:
            hit = deep_find_media_item(json.loads(script), shortcode)
        except json.JSONDecodeError:
            continue
        if hit:
            return hit
    return None


def extract_fallback_post(page: Page, url: str) -> dict[str, Any]:
    image_url = page.locator("meta[property='og:image']").get_attribute("content") or ""
    video_url = page.locator("meta[property='og:video']").get_attribute("content") or ""
    description = page.locator("meta[name='description']").get_attribute("content") or ""
    timestamp = page.locator("time").first.get_attribute("datetime") if page.locator("time").count() else None
    shortcode = parse_shortcode_from_url(url)
    return {
        "code": shortcode,
        "media_type": 2 if video_url else 1,
        "caption": {"text": description},
        "taken_at": None,
        "image_versions2": {"candidates": [{"url": image_url}]} if image_url else {"candidates": []},
        "video_versions": [{"url": video_url}] if video_url else [],
        "user": {"username": urlparse(url).path.strip("/").split("/")[0]},
        "_fallback_timestamp": timestamp,
    }


def scrape_post_payload(browser: InstagramBrowser, url: str) -> dict[str, Any]:
    for attempt in range(2):
        try:
            return _scrape_post_payload_once(browser, url)
        except PlaywrightError as exc:
            if attempt:
                raise
            logger.warning("Could not open post: %s. Retrying in 3 seconds.", exc)
            _safe_sleep(3)
    raise AssertionError("unreachable")


def _scrape_post_payload_once(browser: InstagramBrowser, url: str) -> dict[str, Any]:
    page = browser.require_context().new_page()
    shortcode = parse_shortcode_from_url(url)
    marker = f'"code":"{shortcode}"'
    hits: list[dict[str, Any]] = []

    def handle_response(response) -> None:
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return
        try:
            text = response.text()
        except Exception:
            return
        if marker not in text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        hit = deep_find_media_item(payload, shortcode)
        if hit:
            hits.append(hit)

    page.on("response", handle_response)
    try:
        page.goto(url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
        assert_page_is_usable(page, url)
        try:
            page.wait_for_load_state("networkidle", timeout=POST_WAIT_MS)
        except TimeoutError:
            pass
        body_text = page.text_content("body") or ""
        if "Please wait a few minutes before you try again" in body_text:
            raise InstagramRateLimitError(f"Instagram rate-limited post page access for {url}. Wait and retry later.")

        if hits:
            return hits[0]

        script_hit = extract_media_from_scripts(page, shortcode)
        if script_hit:
            return script_hit

        return extract_fallback_post(page, url)
    finally:
        page.close()


def _extract_media_url(item: dict[str, Any], download_videos: bool = False) -> str:
    candidates = item.get("image_versions2", {}).get("candidates") or []
    versions = item.get("video_versions") or []
    if download_videos and versions:
        return versions[0].get("url", "")
    if candidates:
        return candidates[0]["url"]
    return ""


def media_payload_complete(payload, download_videos):
    if not isinstance(payload, dict):
        return False
    items = payload.get('carousel_media') if payload.get('media_type') == 8 else [payload]
    return bool(items) and all(_extract_media_url(item, download_videos)
        and not (download_videos and item.get('media_type') == 2 and not item.get('video_versions')) for item in items)


def _download_single_media(session: requests.Session, media_dir: Path, base_name: str, slide: int, item: dict[str, Any], download_videos: bool = False) -> Path:
    url = _extract_media_url(item, download_videos)
    if not url:
        raise ExportError(f"Could not find media URL for {base_name} slide {slide}")
    target = media_dir / f"{base_name}_{slide}" if slide > 0 else media_dir / base_name
    return download_binary(session, url, target)


def build_post_record(session: requests.Session, payload: dict[str, Any], media_dir: Path, index: int, url: str, download_videos: bool = False) -> dict[str, Any]:
    shortcode = payload.get("code") or parse_shortcode_from_url(url)
    media_type = payload.get("media_type", 1)
    base_name = safe_slug(shortcode) + ("-video" if download_videos else "-cover")

    caption = ((payload.get("caption") or {}).get("text") or "").strip()
    taken_at = payload.get("taken_at")
    date_value = None
    if isinstance(taken_at, int):
        try:
            date_value = datetime.fromtimestamp(taken_at, tz=UTC).astimezone()
        except (ValueError, OverflowError, OSError):
            pass
    if date_value is None:
        fallback_timestamp = payload.get("_fallback_timestamp")
        if fallback_timestamp:
            try:
                date_value = datetime.fromisoformat(fallback_timestamp.replace("Z", "+00:00")).astimezone()
            except ValueError:
                date_value = None
        else:
            date_value = None

    date_label = date_value.strftime("%d.%m.%Y %H:%M") if date_value else "Date unknown"

    kind_label = {1: "Image", 2: "Video", 8: "Carousel"}.get(media_type, "Post")
    likes = payload.get("like_count")
    comments = payload.get("comment_count")

    # Carousel — download every slide
    if media_type == 8 and payload.get("carousel_media"):
        items = payload["carousel_media"]
        media_paths: list[str] = []
        first_alt = ""
        for slide_idx, item in enumerate(items):
            media_file = _download_single_media(session, media_dir, base_name, slide_idx, item, download_videos)
            rel = f"media/{media_file.name}"
            media_paths.append(rel)
            if slide_idx == 0:
                first_alt = item.get("accessibility_caption") or ""
        alt_text = first_alt or caption[:120] or f"Instagram post {shortcode}"
        return {
            **publication_details(payload),
            "shortcode": shortcode,
            "caption": caption,
            "local_media_path": media_paths[0],
            "media_paths": media_paths,
            "download_videos": download_videos,
            "media_types": ["video" if download_videos and item.get("video_versions") else "image" for item in items],
            "instagram_url": normalized_post_url(url),
            "date_label": date_label,
            "likes_label": f"Likes: {format_count(likes if isinstance(likes, int) else None)}",
            "comments_label": f"Comments: {format_count(comments if isinstance(comments, int) else None)}",
            "kind_label": kind_label,
            "alt_text": alt_text,
        }

    # Single image / video
    item = payload
    url_from_item = _extract_media_url(item, download_videos)
    if not url_from_item:
        raise ExportError(f"Could not find media URL for {url}")

    media_file = download_binary(session, url_from_item, media_dir / base_name)
    alt_text = item.get("accessibility_caption") or payload.get("accessibility_caption") or caption[:120] or f"Instagram post {shortcode}"

    return {
        **publication_details(payload),
        "shortcode": shortcode,
        "caption": caption,
        "local_media_path": f"media/{media_file.name}",
        "media_paths": [f"media/{media_file.name}"],
        "download_videos": download_videos,
        "media_types": ["video" if download_videos and item.get("video_versions") else "image"],
        "instagram_url": normalized_post_url(url),
        "date_label": date_label,
        "likes_label": f"Likes: {format_count(likes if isinstance(likes, int) else None)}",
        "comments_label": f"Comments: {format_count(comments if isinstance(comments, int) else None)}",
        "kind_label": kind_label,
        "alt_text": alt_text,
    }


def build_mode_label(args: argparse.Namespace) -> str:
    oldest = getattr(args, "oldest", False)
    limit = getattr(args, "limit", 0)
    if limit:
        label = f"First {limit} selected posts, oldest first" if oldest else f"Most recent {limit} selected posts, newest first"
    else:
        label = "All selected posts, oldest first" if oldest else "All selected posts, newest first"
    dates = date_label(args.after, args.before)
    return f"{label} • {dates}" if dates else label


def generate_html(
    profile: dict[str, Any],
    post_records: list[dict[str, Any]],
    avatar_src: str,
    mode_label: str,
    output_dir: Path,
    batch_size: int,
    report_names: list[str] | None = None,
) -> None:
    saved_profile=read_json(output_dir/'.profile.json') or {}
    if saved_profile.get('username')==profile.get('username'):
        profile={**profile,**saved_profile.get('profile',{})}
    post_records = [
        {**record, "date_label": "Date unknown"}
        if record.get("date_label") == "\u0414\u0430\u0442\u0430 \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430"
        else record
        for record in post_records
    ]
    payload = {
        "profile": {
            "username": profile["username"],
            "avatar_src": avatar_src,
        },
        "posts": post_records,
        "batch_size": batch_size,
        "initial_order": 'oldest' if 'oldest first' in mode_label else 'newest',
        "selection_label": mode_label,
    }

    raw_json = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    html = HTML_TEMPLATE.format(
        title=escape(f"@{profile['username']} Instagram Export"),
        brand_title=escape(f"Instagram Export • @{profile['username']}"),
        brand_meta=escape(f"Profile export • {datetime.now().strftime('%d.%m.%Y')}"),
        stats_pill=escape(f"{len(post_records)} posts ready"),
        avatar_src=escape(avatar_src),
        username=escape(profile["username"]),
        full_name=escape(profile.get("full_name") or "Unnamed profile"),
        post_count=escape(format_count(profile.get("mediacount"))),
        followers=escape(format_count(profile.get("followers"))),
        followees=escape(format_count(profile.get("followees"))),
        mode_label=escape(mode_label),
        bio=escape(profile['biography'] or 'Biography is empty.' if isinstance(profile.get('biography'),str) else 'Biography was not received.'),
        post_data=raw_json,
    )
    report_links = "".join(f'<a href="{kind}.html">{escape(title)}</a> ' for kind, title in {**REPORTS, 'summary':'Text report', 'highlights':'Highlights'}.items()
                           if (report_names is None or kind in report_names) and (output_dir / f"{kind}.html").exists())
    if report_links:
        html = html.replace('<main class="page">', '<main class="page"><nav aria-label="Publication reports" style="padding:16px 0;display:flex;gap:20px;flex-wrap:wrap">' + report_links + '</nav>')
    index_file = output_dir / "index.html"
    for asset in ('archive.js','archive.css'):
        atomic_write(output_dir / asset, (TEMPLATE_DIR / asset).read_bytes())
    atomic_write(index_file, html)
    logger.debug("HTML saved to %s", index_file)


def save_profile_metadata(output_dir, username, profile, session=None):
    cached = read_json(output_dir / '.profile.json') or {}
    previous = cached.get('profile', {}) if cached.get('username') == username else {}
    if not previous:
        state = read_json(export_state_path(output_dir)) or {}
        if (state.get('job') or {}).get('username') == username:
            previous = state.get('profile') or {}
    merged = {**{key:value for key,value in previous.items() if value is not None and value not in ('No biography provided.','Unnamed profile')}, 'username': username}
    merged.update({key:value for key,value in profile.items() if value is not None and value not in ('No biography provided.','Unnamed profile')})
    observed = getattr(session, '_igdump_browser_profile_metadata', None) if session is not None else None
    if isinstance(observed, dict):
        merged.update({key:value for key,value in observed.items() if value is not None})
    write_json(output_dir / '.profile.json', {'username':username,'profile':merged})
    return merged


def enabled_report_names(args):
    return [name for name in REPORTS if getattr(args,name,False)] + (['summary'] if getattr(args,'text_report',False) else []) + (['highlights'] if getattr(args,'highlights',False) else [])


def collect_profile_highlights(args, browser, session, output_dir):
    try:
        nodes=getattr(session,'_igdump_highlight_nodes',None)
        if not isinstance(nodes,list):
            profile,_,_,_=resolve_profile_for_posts(session,browser,args.username,force_browser=True)
            save_profile_metadata(output_dir,args.username,profile,session)
            nodes=getattr(session,'_igdump_highlight_nodes',None)
        if not isinstance(nodes,list):raise ExportError('Instagram did not return the Highlights album list. Saved data is preserved.')
        info=getattr(session,'_igdump_highlight_page_info',{})
        result=export_highlights(output_dir,args.username,nodes,browser,
            lambda url,path:download_binary(session,url,path),videos=args.download_videos,
            refresh=args.refresh,limit=args.highlight_limit,tray_complete=isinstance(info,dict) and info.get('has_next_page') is False)
    except Exception as exc:
        logger.error('Highlights: %s',exc)
        if is_rate_limited(exc) or 'denied access' in str(exc):args._rate_limited=True
        cache=read_json(output_dir/'.highlights.json') or {}
        if cache.get('username')!=args.username:cache={'username':args.username,'albums':[]}
        cache['completed']=False;cache['reason']=str(exc)
        write_json(output_dir/'.highlights.json',cache)
        rebuild_highlights(output_dir,args.username,getattr(args,"highlight_limit",0))
        result=4
    args._highlights_result=result
    return result


def run_highlights(args):
    root=ensure_output_dir(args.output_dir,args)
    if args.offline:result=rebuild_highlights(root,args.username,args.highlight_limit)
    else:
        with InstagramBrowser(Path(args.browser_profile_dir).expanduser().resolve(),args.headful) as browser:
            browser.ensure_logged_in()
            session=browser.authenticated_session();_patch_session_for_logging(session)
            profile,_,_,_=resolve_profile_for_posts(session,browser,args.username)
            save_profile_metadata(root,args.username,profile,session)
            result=collect_profile_highlights(args,browser,session,root)
    _save_debug_log(root)
    print((root/'highlights.html').as_uri());open_export(root/'highlights.html',args)
    return result


def remember_post_dates(session: requests.Session, items: list[dict[str, Any]]) -> None:
    remember_details(session, items)
    dates = getattr(session, "_igdump_post_dates", None)
    if not isinstance(dates, dict):
        dates = {}
        session._igdump_post_dates = dates
    counts = getattr(session, "_igdump_post_likes", None)
    if not isinstance(counts, dict):
        counts = session._igdump_post_likes = {}
    for item in items:
        code = item.get("code") or item.get("shortcode")
        count = item.get("like_count")
        if count is None:
            count = (item.get("edge_media_preview_like") or item.get("edge_liked_by") or {}).get("count")
        if code and isinstance(count, int) and count >= 0:
            counts[code] = count
        value = post_date(item)
        if code and value:
            dates[code] = value.isoformat()
        stamp = item.get('taken_at', item.get('taken_at_timestamp'))
        if code and isinstance(stamp, (int, float)) and value:
            pin = item.get('is_pinned')
            if not isinstance(pin, bool) and isinstance(item.get('pinned_for_users'), list):
                pin = bool(item['pinned_for_users'])
            nodes = getattr(session, '_igdump_timeline_nodes', None)
            if not isinstance(nodes, dict):
                nodes = session._igdump_timeline_nodes = {}
            nodes[code] = {'stamp': stamp, 'pinned': pin if isinstance(pin, bool) else None}
        if code and isinstance(item.get('media_type'), int) and media_payload_complete(item, False):
            payloads = getattr(session, '_igdump_media_payloads', None)
            if not isinstance(payloads, dict):
                payloads = session._igdump_media_payloads = {}
            if code in payloads or len(payloads) < TIMELINE_MEDIA_CACHE_LIMIT:
                payloads[code] = item


def save_post_metadata(session, output_dir, username):
    save_details(session, output_dir, username)
    path = output_dir / ".post-likes.json"
    payload = read_json(path) or {}
    counts = payload.get("counts", {}) if payload.get("username") == username else {}
    if not isinstance(counts, dict):
        counts = {}
    fresh = getattr(session, "_igdump_post_likes", None)
    if isinstance(fresh, dict):
        counts.update(fresh)
    counts = {code: count for code, count in counts.items() if isinstance(count, int) and count >= 0}
    write_json(path, {"username": username, "counts": counts})
    return counts


def fetch_post_date(session: requests.Session, url: str) -> str | None:
    code = parse_shortcode_from_url(url)
    response = session.get(f"{IG_BASE_URL}/api/v1/media/{shortcode_to_media_id(code)}/info/",
                           timeout=30, allow_redirects=False)
    try:
        if response.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise ExportError("Instagram denied access to the publication date. Check login and post access; cached dates are saved.")
        if response.status_code == 429:
            raise ExportError("Instagram rate-limited date requests (429). Cached dates are saved; retry later.")
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "fail":
            raise ExportError(f"Instagram did not return a publication date for {code}: {data.get('message', 'API error')}")
        items = data.get("items") or []
        remember_post_dates(session, items)
        value = post_date(items[0]) if items else None
        return value.isoformat() if value else None
    finally:
        response.close()


def filter_links_by_date(links: list[str], args: argparse.Namespace, session: requests.Session, output_dir: Path) -> list[str]:
    details = {row['shortcode']: row for row in cached_details(output_dir, args.username, links)}
    fresh = getattr(session, '_igdump_post_details', None)
    if isinstance(fresh, dict):
        details.update({code: {**details.get(code, {}), **row} for code, row in fresh.items()})
    timestamps = {code: row['taken_at'] for code, row in details.items()
                  if isinstance(row.get('taken_at'), (int, float)) and not isinstance(row['taken_at'], bool)}
    def ordered(selected):
        if all(parse_shortcode_from_url(link) in timestamps for link in selected):
            return sorted(selected, key=lambda link: timestamps[parse_shortcode_from_url(link)], reverse=True)
        if selected:
            logger.warning('Some publication times are unavailable. Keeping Instagram order; latest/earliest ordering cannot be fully verified.')
        return selected
    if not args.after and not args.before:
        return ordered(links)
    path = output_dir / ".post-dates.json"
    payload = read_json(path) or {}
    dates = payload.get("dates", {}) if payload.get("username") == args.username and not args.refresh else {}
    if not isinstance(dates, dict):
        dates = {}
    if not args.refresh:
        for detail in cached_details(output_dir, args.username, links):
            value = post_date(detail)
            if value and detail["shortcode"] not in dates:
                dates[detail["shortcode"]] = value.isoformat()
    timeline_dates = getattr(session, "_igdump_post_dates", None)
    if isinstance(timeline_dates, dict):
        dates.update(timeline_dates)
    for code, stamp in timestamps.items():
        dates[code] = datetime.fromtimestamp(stamp, UTC).astimezone().date().isoformat()
    selected: list[str] = []
    unknown = 0
    write_json(path, {"username": args.username, "dates": dates})
    for index, url in enumerate(links, start=1):
        code = parse_shortcode_from_url(url)
        if code not in dates:
            if getattr(args, "offline", False):
                raise ExportError(f"No cached publication date for {code}; first run collection without --offline.")
            logger.info("Checking date %s/%s: %s", index, len(links), code)
            _safe_sleep(1)
            dates[code] = fetch_post_date(session, url)
            save_post_metadata(session, output_dir, args.username)
            write_json(path, {"username": args.username, "dates": dates})
        value = dates[code]
        if value is None:
            unknown += 1
            continue
        try:
            value = datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            raise ExportError("The date cache is invalid. Run again with --refresh.") from None
        if date_matches(value, args.after, args.before):
            selected.append(url)
        update_progress("Dates", index, len(links))
    logger.info("Date filters selected %s of %s posts (%s).", len(selected), len(links), date_label(args.after, args.before))
    if unknown:
        raise ExportError(f"For {unknown} posts the publication date is unknown. Selection is incomplete; use --refresh to recheck dates.")
    return ordered(sorted(selected, key=lambda url: dates[parse_shortcode_from_url(url)], reverse=True))


def select_post_links(all_links: list[str], args: argparse.Namespace) -> list[str]:
    limit = getattr(args, "limit", 0)
    if getattr(args, "oldest", False):
        selected = all_links[-limit:] if limit else all_links
        return list(reversed(selected))
    return all_links[:limit] if limit else all_links


def open_export(path: Path, args: argparse.Namespace) -> None:
    if args.no_open:
        return
    try:
        if not webbrowser.open(path.resolve().as_uri()):
            logger.warning("Could not open the archive automatically. Open this file: %s", path)
    except (OSError, webbrowser.Error) as exc:
        logger.warning("Could not open the archive: %s. Saved file: %s", exc, path)


def run(args: argparse.Namespace) -> int:
    output_dir = ensure_output_dir(args.output_dir, args)
    if args.offline:
        return run_posts_offline(args, output_dir)
    profile_dir = Path(args.browser_profile_dir).expanduser().resolve()

    with InstagramBrowser(profile_dir=profile_dir, headful=args.headful) as browser:
        browser.ensure_logged_in()
        preflight_instagram_access(browser)
        session = browser.authenticated_session()
        _patch_session_for_logging(session)
        args._shared_session = session
        update_progress("Profile", 0, detail="resolving profile")
        session._igdump_profile_transport = browser.require_context().request
        try:
            profile, _, _, user_id = resolve_profile_for_posts(session, browser, args.username)
        finally:
            del session._igdump_profile_transport
        profile = save_profile_metadata(output_dir, args.username, profile, session)
        logger.info("Resolved @%s profile metadata", profile["username"])

        # Try to resume from saved cursor; if missing, fetch first page fresh.
        cached = load_post_links(output_dir, profile["username"]) if not args.refresh else {"links": [], "cursor": None}
        saved_cursor = cached.get("cursor")
        if cached["links"] and saved_cursor is not None and not cached.get("completed") and not cached.get('selection'):
            initial_links: list[str] = []
            end_cursor: str | None = None
        else:
            initial_links, end_cursor, _has_next = fetch_authenticated_timeline_page(session, profile["username"], None, user_id=user_id)
            if _has_next and end_cursor is None:
                raise ExportError("Instagram returned no next-page cursor. Retry later: the beginning of the profile has not been reached.")
            if not _has_next:
                end_cursor = None

        all_links = collect_post_links(
            session=session,
            username=profile["username"],
            total_available=int(profile.get("mediacount") or 0),
            output_dir=output_dir,
            args=args,
            seed_links=initial_links,
            initial_cursor=end_cursor,
            user_id=user_id,
        )
        logger.info("Collected %s post URLs", len(all_links))
        profile = save_profile_metadata(output_dir, args.username, profile, session)
        timeline_state = read_json(post_links_path(output_dir))
        timeline_complete = timeline_state is None or timeline_satisfies_selection(output_dir, args)
        if not timeline_complete:
            logger.warning("The timeline is incomplete. The archive will be partial; retry later.")

        filtered_links = filter_links_by_date(all_links, args, session, output_dir)
        selected_links = select_post_links(filtered_links, args)
        args._selected_links = selected_links
        if args.dry_run:
            logger.info("Dry-run: %s/%s posts selected for export", len(selected_links), len(all_links))
            return 0

        with PostStore(output_dir / '.posts.sqlite', args.username) as post_store:
            state = load_export_state(output_dir, args)
            existing_records = state.get("post_records", []) if isinstance(state.get("post_records"), list) else []
            archive_state = read_json(export_state_path(output_dir)) or {}
            if (archive_state.get('job') or {}).get('username') == args.username:
                existing_records = archive_state.get('archive_records', archive_state.get('post_records', [])) + existing_records
            post_store.migrate(existing_records)
            existing_records = post_store.records()
            records_by_shortcode = {
                record.get("shortcode"): record for record in existing_records if isinstance(record, dict)
            }

            avatar_name = state.get("avatar_src") if isinstance(state.get("avatar_src"), str) else None
            if avatar_name and avatar_name != "avatar-placeholder.svg" and (output_dir / avatar_name).exists():
                avatar_file = output_dir / avatar_name
            else:
                avatar_url = profile.get("avatar_url")
                avatar_file = output_dir / "avatar-placeholder.svg"
                if avatar_url:
                    try:
                        avatar_file = download_binary(session, avatar_url, output_dir / "avatar")
                    except (ExportError, requests.RequestException, OSError) as exc:
                        if is_rate_limited(exc):
                            raise
                        logger.warning("Could not download the avatar: %s. Continuing without it.", exc)
                else:
                    logger.info("Avatar unavailable; continuing the post export.")
                if avatar_file.name == "avatar-placeholder.svg":
                    avatar_file.write_text(
                        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
                        '<circle cx="48" cy="48" r="48" fill="#e5e7eb"/>'
                        '<text x="48" y="60" text-anchor="middle" font-family="sans-serif" font-size="36" fill="#6b7280">'
                        + escape(profile["username"][:1].upper()) + '</text></svg>',
                        encoding="utf-8",
                    )
                avatar_name = avatar_file.name

            # Separate cached and new posts
            post_records: list[dict[str, Any]] = []
            need_scrape: list[tuple[int, str, str]] = []
            for index, post_url in enumerate(selected_links, start=1):
                shortcode = parse_shortcode_from_url(post_url)
                existing_record = records_by_shortcode.get(shortcode)
                if existing_record and not args.refresh and existing_record.get("download_videos", False) == args.download_videos:
                    media_path = existing_record.get("local_media_path")
                    media_paths = existing_record.get("media_paths") or [media_path] if media_path else []
                    all_exist = all(
                        isinstance(p, str) and (output_dir / p).exists()
                        for p in media_paths
                    )
                    if media_paths and all_exist:
                        logger.info("%s/%s %s already cached", index, len(selected_links), shortcode)
                        post_records.append(existing_record)
                        continue
                need_scrape.append((index, post_url, shortcode))

            def checkpoint(completed: bool = False) -> None:
                now = time.monotonic()
                if completed or not (output_dir / 'index.html').exists() or now - last_render[0] >= 10:
                    save_post_metadata(session, output_dir, args.username)
                    by_code = {record["shortcode"]: record for record in post_records}
                    ordered = [by_code[code] for url in selected_links
                               if (code := parse_shortcode_from_url(url)) in by_code]
                    save_export_state(output_dir, args, profile, avatar_name, ordered, completed=completed)
                    generate_html(profile=profile, post_records=ordered, avatar_src=avatar_name,
                                  mode_label=build_mode_label(args), output_dir=output_dir, batch_size=max(1, args.batch_size),
                                  report_names=enabled_report_names(args))
                    last_render[0] = now

            last_render = [float('-inf')]
            checkpoint()
            errors: list[str] = []
            update_progress("Posts", len(post_records), len(selected_links))
            for index, post_url, shortcode in need_scrape:
                logger.info("Post %s/%s: %s", index, len(selected_links), shortcode)
                try:
                    payloads = getattr(session, '_igdump_media_payloads', {})
                    payload = payloads.get(shortcode) if isinstance(payloads, dict) else None
                    from_timeline = media_payload_complete(payload, args.download_videos)
                    if not from_timeline:
                        payload = scrape_post_payload(browser, post_url)
                    remember_post_dates(session, [{**payload, "code": shortcode}])
                    try:
                        record = build_post_record(session, payload, output_dir / "media", index, post_url,
                                                   download_videos=args.download_videos)
                    except (ExportError, requests.RequestException) as exc:
                        if not from_timeline or is_rate_limited(exc):
                            raise
                        logger.info('Timeline media URL failed; refreshing this post through Chrome once.')
                        payload = scrape_post_payload(browser, post_url)
                        remember_post_dates(session, [{**payload, 'code': shortcode}])
                        record = build_post_record(session, payload, output_dir / 'media', index, post_url, download_videos=args.download_videos)
                except (ExportError, requests.RequestException, PlaywrightError, OSError) as exc:
                    logger.error("Could not export %s: %s", shortcode, exc)
                    errors.append(shortcode)
                    update_progress("Posts", len(post_records) + len(errors), len(selected_links), "failed")
                    if is_rate_limited(exc) or isinstance(exc, InstagramAccessError):
                        args._rate_limited = True
                        break
                    continue
                post_records.append(record)
                post_store.add(record)
                if isinstance(payloads, dict):
                    payloads.pop(shortcode, None)
                checkpoint()
                update_progress("Posts", len(post_records) + len(errors), len(selected_links))

            completed = len(post_records) == len(selected_links) and not errors and timeline_complete
            if getattr(args,'highlights',False) and not getattr(args,'_rate_limited',False):
                collect_profile_highlights(args,browser,session,output_dir)
                profile=save_profile_metadata(output_dir,args.username,profile,session)
            passive_reports(args, output_dir, session, selected_links)
            last_render[0] = float('-inf')  # Always render the final partial or complete result.
            checkpoint(completed)
            logger.info("Saved %s of %s posts; errors: %s. Archive: %s",
                        len(post_records), len(selected_links), len(errors), output_dir / "index.html")
            if errors:
                logger.warning("Repeat the same command to download missing posts: %s", ", ".join(errors))
            _save_debug_log(output_dir)
            print((output_dir / "index.html").as_uri())
            open_export(output_dir / "index.html", args)
            return 0 if completed and (args.mode=='full' or not getattr(args,'_highlights_result',0)) else 4


def passive_reports(args, output_dir, session=None, links=None):
    if args.dry_run or getattr(args, "_skip_insights", False):
        return []
    enabled = [kind for kind in REPORTS if getattr(args, kind, False)]
    if not enabled and not getattr(args,'text_report',False) and not getattr(args,'highlights',False):
        return []
    if session is not None:
        save_details(session, output_dir, args.username)
    if links is None:
        links = getattr(args, "_selected_links", None)
    if links is None:
        cached = load_post_links(output_dir, args.username)
        if args.oldest and not cached["completed"]:
            raise ExportError("The cached timeline is incomplete; earliest posts cannot be identified for passive reports.")
        offline_args = argparse.Namespace(**vars(args))
        offline_args.offline, offline_args.refresh = True, False
        links = select_post_links(filter_links_by_date(cached["links"], offline_args, requests.Session(), output_dir), args)
    reports = generate_reports(output_dir, args.username, links, enabled, build_mode_label(args), full=args.mode == "full")
    if getattr(args,'text_report',False):
        reports.append(generate_summary(output_dir,args.username,links))
    if getattr(args,'highlights',False):
        result=rebuild_highlights(output_dir,args.username,getattr(args,"highlight_limit",0)) if args.offline or not hasattr(args,'_highlights_result') else args._highlights_result
        reports.append(('Highlights','highlights.html',result))
    for title, filename, _ in reports:
        logger.info("Saved passive report: %s", output_dir / filename)
    return reports


def run_posts_offline(args, output_dir):
    cached = load_post_links(output_dir, args.username)
    if not cached["links"]:
        raise ExportError("No saved timeline. Use --output-dir to select the original collection folder.")
    if args.oldest and not cached["completed"]:
        raise ExportError("The cached timeline is incomplete; first run without --offline to identify the earliest posts.")
    links = select_post_links(filter_links_by_date(cached["links"], args, requests.Session(), output_dir), args)
    args._selected_links = links
    state = read_json(export_state_path(output_dir)) or {}
    if (state.get("job") or {}).get("username") != args.username:
        state = {}
    if state:
        state['profile'] = save_profile_metadata(output_dir, args.username, state.get('profile') or {})
    by_code = {record.get("shortcode"): record for record in state.get("archive_records", state.get("post_records", []))}
    if (output_dir / '.posts.sqlite').exists():
        with PostStore(output_dir / '.posts.sqlite', args.username) as store:
            by_code.update({record['shortcode']: record for record in store.records()})
    records = [by_code[code] for url in links if (code := parse_shortcode_from_url(url)) in by_code]
    reports = passive_reports(args, output_dir, links=links)
    if state:
        generate_html(state.get("profile") or {"username": args.username}, records, state.get("avatar_src") or "",
                      build_mode_label(args), output_dir, args.batch_size,
                      report_names=enabled_report_names(args))
        target = output_dir / "index.html"
    elif reports:
        target = output_dir / reports[0][1]
    else:
        raise ExportError("No cached post archive. Enable --analytics, --mentions, --contacts or --locations to analyze saved timeline data.")
    open_export(target, args)
    media_complete = all(
        bool(paths := (record.get('media_paths') or [record.get('local_media_path')]))
        and all(isinstance(path, str) and (output_dir / path).is_file() and (output_dir / path).stat().st_size > 0 for path in paths)
        for record in records)
    reports_complete = args.mode=='full' or all(result==0 for _,_,result in reports)
    return 0 if reports_complete and (not state or len(records) == len(links) and media_complete) and timeline_satisfies_selection(output_dir, args) else 4



def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args()
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    ui = TerminalUI(quiet=args.quiet)
    logging.basicConfig(level=level, handlers=[ui], force=True)
    with ui:
        result = execute(args)
        ui.finish(result)
        return result


def execute(args: argparse.Namespace) -> int:
    try:
        output_dir = ensure_output_dir(args.output_dir, args)
        args.output_dir = str(output_dir)
        with export_lock(output_dir):
            if args.mode=='highlights':
                return run_highlights(args)
            if args.mode == "full":
                return run_full(args)
            if args.mode in ("comments", "likes"):
                return run_comments(args)
            return run(args)
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        _debug_event("fatal", "KeyboardInterrupt")
        _try_save_debug_log(args)
        return 130
    except ExportError as exc:
        logger.error("%s", exc)
        _debug_event("fatal", f"ExportError: {exc}", exc=str(exc))
        _try_save_debug_log(args)
        return 2
    except requests.RequestException as exc:
        logger.error("Network request failed: %s", exc)
        _debug_event("fatal", f"Network error: {exc}",
                     exc=str(exc), url=getattr(exc.response, "url", None) if hasattr(exc, "response") else None,
                     status=getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None)
        _try_save_debug_log(args)
        return 3
    except (PlaywrightError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        logger.error('Collection stopped: %s. Saved data is preserved.', exc)
        _debug_event('fatal', f'{type(exc).__name__}: {exc}')
        _try_save_debug_log(args)
        return 2


def _try_save_debug_log(args: argparse.Namespace) -> None:
    """Try to save debug log; fails silently if output_dir isn't available."""
    out = getattr(args, "output_dir", None)
    if out:
        _save_debug_log(Path(out))
    elif getattr(args, "username", None):
        fallback = Path("exports") / f"{args.username}-debug"
        fallback.mkdir(parents=True, exist_ok=True)
        _save_debug_log(fallback)


# ── Comment Export ──────────────────────────────────────────────────────────

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_media_id(shortcode: str) -> str:
    decoded = 0
    for c in shortcode:
        decoded = decoded * 64 + SHORTCODE_ALPHABET.index(c)
    return str(decoded)


def _safe_sleep(base: float, jitter: float = 0.3) -> None:
    delay = random.uniform(base * (1 - jitter), base * (1 + jitter))
    time.sleep(delay)


COMMENTS_HEADERS = {
    **IG_API_HEADERS,
    "User-Agent": IG_MOBILE_USER_AGENT,
}


def fetch_comments_page(
    session: requests.Session,
    media_id: str,
    max_id: str | None,
    delay: float,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    _safe_sleep(delay, jitter=0.3)

    params: dict[str, Any] = {"count": 50}
    if max_id:
        params["max_id"] = max_id

    response = session.get(
        f"{IG_BASE_URL}/api/v1/media/{media_id}/comments/",
        params=params,
        headers=COMMENTS_HEADERS,
        timeout=30,
        allow_redirects=False,
    )

    if response.status_code in (302, 303, 307, 401):
        raise InstagramAccessError(
            "Instagram redirected to login — your sessionid is expired or invalid. "
            "Get a fresh one from your browser (F12 → Application → Cookies → sessionid) "
            "and pass it via --sessionid"
        )

    raise_if_rate_limited(response, "comments")

    data = api_payload(response, 'comments', 'comments')

    comments = data.get("comments", [])
    next_max_id = data.get("next_max_id")
    has_more = data.get("has_more_comments", False)
    return comments, next_max_id, has_more


def fetch_all_comments(
    session: requests.Session,
    shortcode: str,
    delay: float,
) -> list[dict[str, Any]]:
    media_id = shortcode_to_media_id(shortcode)
    all_comments: list[dict[str, Any]] = []
    max_id: str | None = None
    saved = getattr(session, '_igdump_comment_cursor', None)
    if isinstance(saved, str):
        max_id = saved
    callback = getattr(session, '_igdump_comment_page_callback', None)
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()

    while True:
        comments, next_max_id, has_more = fetch_comments_page(session, media_id, max_id, delay)
        for comment in comments:
            key = str(comment.get("pk") or comment.get("id") or "")
            if key and key in seen_ids:
                continue
            if key:
                seen_ids.add(key)
            all_comments.append(comment)
        if callable(callback):
            callback(shortcode, comments, next_max_id if has_more else None)
        if not has_more:
            break
        if not next_max_id or next_max_id in seen_cursors:
            raise ExportError("Instagram returned no new comment page. This post is unfinished; retry later.")
        seen_cursors.add(next_max_id)
        max_id = next_max_id

    return all_comments


def build_comment_record(comment: dict[str, Any], shortcode: str) -> dict[str, Any]:
    user = comment.get("user") or {}
    return {
        "comment_id": str(comment.get("pk", comment.get("id", ""))),
        "user_id": str(user.get('pk') or user.get('id') or user.get('username') or ''),
        "post_shortcode": shortcode,
        "username": user.get("username", "unknown"),
        "full_name": user.get("full_name", ""),
        "profile_pic_url": user.get("profile_pic_url", ""),
        "text": comment.get("text", ""),
        "created_at": comment.get("created_at", 0),
        "like_count": comment.get("like_count", 0),
        "child_comment_count": comment.get("child_comment_count", 0),
    }


def fetch_all_likers(session: requests.Session, shortcode: str, delay: float) -> list[dict[str, Any]]:
    users: dict[str, dict[str, Any]] = {}
    expected_count = None
    callback = getattr(session, "_igdump_liker_page_callback", None)
    streaming = callable(callback) and isinstance(session, requests.Session)
    resume = getattr(session, "_igdump_liker_resume_cursor", None)
    cursor = resume if isinstance(resume, str) else None
    seen_cursors = {cursor} if cursor else set()
    while True:
        _safe_sleep(delay)
        params = {"max_id": cursor} if cursor else {}
        for attempt in range(3):
            response = None
            try:
                session._igdump_liker_attempts = attempt + 1
                response = session.get(f"{IG_BASE_URL}/api/v1/media/{shortcode_to_media_id(shortcode)}/likers/",
                                       params=params, timeout=30, allow_redirects=False)
                if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                    raise ExportError("Instagram denied access to the list of users who liked this post. Check login and post access.")
                if response.status_code == 429:
                    raise ExportError("Instagram rate-limited like-list requests (429). Stop collection and retry later.")
                response.raise_for_status()
                data = api_payload(response, 'likers', 'users')
                break
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                if attempt == 2 or (isinstance(exc, requests.HTTPError) and response.status_code < 500):
                    raise
                logger.warning("Request failed for %s; retry %s/2 in %s seconds", shortcode, attempt + 1, 2 ** (attempt + 1))
                _safe_sleep(2 ** (attempt + 1), jitter=0)
            finally:
                if response is not None:
                    response.close()
        if data.get("status") == "fail" or "users" not in data:
            raise ExportError(f"Instagram returned no list of users who liked the post: {data.get('message', 'API error')}")
        page_users = data.get("users") or []
        if not streaming:
            for user in page_users:
                key = str(user.get("pk") or user.get("id") or user.get("username") or "")
                if key:
                    users[key] = user
        reported_count = data.get("user_count", data.get("like_count"))
        if isinstance(reported_count, int):
            expected_count = reported_count
        next_cursor = data.get("next_max_id")
        has_more = bool(data.get("has_more_likers", data.get("has_more", data.get("more_available", bool(next_cursor)))))
        if streaming:
            callback(shortcode, page_users, expected_count, next_cursor if has_more else None)
        if not has_more:
            break
        if not next_cursor or next_cursor in seen_cursors:
            raise ExportError("Instagram returned no new like-list page; this post is unfinished.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    counts = getattr(session, "_igdump_liker_counts", None)
    if not isinstance(counts, dict):
        counts = session._igdump_liker_counts = {}
    counts[shortcode] = expected_count
    incomplete = getattr(session, "_igdump_incomplete_likers", None)
    if not isinstance(incomplete, set):
        incomplete = session._igdump_incomplete_likers = set()
    incomplete.discard(shortcode)
    if not streaming and expected_count is not None and expected_count > len(users):
        incomplete.add(shortcode)
        logger.warning("Instagram returned %s of %s users who liked post %s. Statistics are partial.", len(users), expected_count, shortcode)
    return list(users.values())


def build_liker_record(user: dict[str, Any], shortcode: str) -> dict[str, Any]:
    return {"post_shortcode": shortcode, "user_id": str(user.get("pk") or user.get("id") or user.get("username") or ""),
            "username": user.get("username", "unknown"), "full_name": user.get("full_name", "")}


def aggregate_comment_stats(
    comment_records: list[dict[str, Any]],
    post_count: int,
) -> dict[str, Any]:
    total = len(comment_records)

    # Per-user stats
    user_stats: dict[str, dict[str, Any]] = {}
    for c in comment_records:
        u = c.get('user_id') or c['username']
        if u not in user_stats:
            user_stats[u] = {
                "username": c['username'],
                "full_name": c["full_name"],
                "profile_pic_url": c["profile_pic_url"],
                "count": 0,
                "likes_received": 0,
                "last_comment_ts": 0,
                "posts_commented": set(),
            }
        s = user_stats[u]
        s["count"] += 1
        s["likes_received"] += c.get("like_count", 0)
        s["posts_commented"].add(c["post_shortcode"])
        ts = c.get("created_at", 0)
        if ts > s["last_comment_ts"]:
            s["last_comment_ts"] = ts

    for s in user_stats.values():
        s["posts_commented"] = len(s["posts_commented"])

    commenters = sorted(user_stats.values(), key=lambda x: (-x["count"], x["username"]))
    unique = len(commenters)
    top_commenters = commenters[:30]

    # Top commented posts
    post_counts: dict[str, int] = {}
    for c in comment_records:
        sc = c["post_shortcode"]
        post_counts[sc] = post_counts.get(sc, 0) + 1
    top_posts = sorted(post_counts.items(), key=lambda x: -x[1])[:20]

    return {
        "total_comments": total,
        "unique_commenters": unique,
        "posts_with_comments": len(post_counts),
        "total_posts": post_count,
        "avg_per_post": round(total / max(post_count, 1), 1),
        "top_commenters": top_commenters,
        "participants": commenters,
        "top_posts": [{"shortcode": sc, "count": n} for sc, n in top_posts],
    }


CONFIG_DIR = Path.home() / ".insta-export"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_config() -> dict[str, Any]:
    return read_json(CONFIG_FILE) or {}


def _save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG_FILE, json.dumps(config, ensure_ascii=False), private=True)


def _session_from_sessionid(sessionid: str) -> requests.Session:
    """Build an authenticated requests.Session from a raw sessionid cookie value."""
    session = _make_session()
    session.cookies.set("sessionid", sessionid, domain=".instagram.com", path="/")

    user_id = sessionid.split("%")[0] if "%" in sessionid else ""
    if user_id:
        session.cookies.set("ds_user_id", user_id, domain=".instagram.com", path="/")

    csrf = _fetch_csrftoken(session)
    if csrf:
        session.headers["X-CSRFToken"] = csrf
        _debug_event("auth.csrf", "csrftoken obtained", source="sessionid")
    else:
        logger.warning("Could not obtain csrftoken; GraphQL queries may fail, REST fallback will be used.")
        _debug_event("auth.csrf", "csrftoken MISSING", source="sessionid")

    # Validate session
    if not _session_is_valid(session):
        raise ExportError(
            "The sessionid you provided is expired or invalid. "
            "Get a fresh one: open Instagram in your browser, press F12 → Application → Cookies → "
            "instagram.com → sessionid, copy the value, and pass it with --sessionid"
        )

    _patch_session_for_logging(session)
    _debug_event("auth.source", "Session from sessionid")
    return session


def _session_is_valid(session: requests.Session) -> bool:
    """Only a successful protected endpoint confirms authentication."""
    response = session.get(f"{IG_BASE_URL}/api/v1/accounts/current_user/",
                           timeout=15, allow_redirects=False)
    try:
        raise_if_rate_limited(response, "session validation")
        if response.status_code in (301, 302, 303, 307, 308, 400, 401, 403):
            return False
        response.raise_for_status()
        payload = response.json()
        return isinstance(payload, dict) and bool(payload.get("user"))
    finally:
        response.close()


def _fetch_csrftoken(session: requests.Session) -> str | None:
    """Try to obtain a fresh csrftoken via multiple methods."""
    # Method 1: lightweight fetch_headers endpoint
    for _attempt in range(2):
        try:
            resp = session.get(
                f"{IG_BASE_URL}/api/v1/si/fetch_headers/",
                params={"client_version": "1"},
                timeout=15,
            )
            raise_if_rate_limited(resp, "session headers")
            csrf = session.cookies.get("csrftoken")
            if csrf:
                return csrf
            break
        except requests.RequestException:
            _safe_sleep(2)
            continue

    # Method 2: scrape from the main page
    try:
        resp = session.get(f"{IG_BASE_URL}/", timeout=20)
        raise_if_rate_limited(resp, "session homepage")
        csrf = session.cookies.get("csrftoken")
        if csrf:
            return csrf
    except requests.RequestException:
        pass

    return None


def run_comments(args: argparse.Namespace) -> int:
    kind = args.mode
    is_likes = kind == "likes"
    output_dir = ensure_output_dir(args.output_dir, args)
    if getattr(args, "offline", False):
        cached = load_post_links(output_dir, args.username)
        if not cached["links"]:
            raise ExportError("No saved timeline in this folder. Use --output-dir to select the original collection folder.")
        if args.oldest and not cached["completed"]:
            raise ExportError("The cached timeline is incomplete; first run without --offline to identify the earliest posts.")
        cache_kind = "likers" if kind == "likes" else kind
        if not any((output_dir / name).exists() for name in (f".{cache_kind}.json", f".{cache_kind}.sqlite")):
            raise ExportError("No saved interaction data in this folder; first collect without --offline.")
        session = requests.Session()
        links = select_post_links(filter_links_by_date(cached["links"], args, session, output_dir), args)
        passive_reports(args, output_dir, links=links)
        if is_likes:
            return export_likers(args, session, output_dir, args.username, links)
        return export_cached_comments(args, output_dir, links)
    shared_links = getattr(args, "_selected_links", None)
    shared_session = getattr(args, "_shared_session", None)
    if shared_links is not None and shared_session is not None:
        passive_reports(args, output_dir, shared_session, shared_links)
        if is_likes:
            return export_likers(args, shared_session, output_dir, args.username, shared_links)
        return export_comments(args, shared_session, output_dir, args.username, shared_links)
    profile_dir = Path(args.browser_profile_dir).expanduser().resolve()
    if not args.sessionid and profile_dir.exists():
        with InstagramBrowser(profile_dir=profile_dir, headful=False) as browser:
            if browser.is_logged_in():
                return run_browser_interactions(args, browser, output_dir)

    session: requests.Session | None = getattr(args, "_shared_session", None)
    profile = None

    # 1. Try --sessionid flag (explicit)
    if session is None and args.sessionid:
        try:
            session = _session_from_sessionid(args.sessionid)
            _save_config({**{"sessionid": args.sessionid}})
            logger.info("Sessionid saved to %s", CONFIG_FILE)
        except ExportError as exc:
            if is_rate_limited(exc):
                raise
            logger.warning("Provided --sessionid is invalid: %s", exc)

    # 2. Try saved config
    if session is None:
        config = _load_config()
        saved = config.get("sessionid")
        if saved:
            try:
                session = _session_from_sessionid(saved)
                logger.debug("Using saved sessionid from %s", CONFIG_FILE)
            except ExportError as exc:
                if is_rate_limited(exc):
                    raise
                logger.warning("Saved sessionid expired: %s", exc)
                # Clear invalid sessionid so user is prompted for a fresh one
                _save_config({})

    # 3. Try Playwright browser profile
    if session is None and profile_dir.exists():
        try:
            with InstagramBrowser(profile_dir=profile_dir, headful=False) as browser:
                if browser.is_logged_in():
                    session = browser.authenticated_session()
        except Exception as exc:
            if is_rate_limited(exc):
                raise
            logger.debug("Playwright failed: %s", exc)

    # 4. Prompt user interactively
    while session is None:
        logger.warning("No Instagram session found. Paste your sessionid cookie (F12 → Application → Cookies → sessionid):")
        try:
            raw = prompt_input("sessionid: ", secret=True).strip()
            if not raw:
                logger.error("No sessionid provided.")
                _save_debug_log(output_dir)
                return 1
            try:
                session = _session_from_sessionid(raw)
                _save_config({"sessionid": raw})
                logger.info("Sessionid saved to %s", CONFIG_FILE)
            except ExportError as exc:
                if is_rate_limited(exc):
                    raise
                logger.warning("Invalid sessionid: %s. Try again.", exc)
                continue
        except (EOFError, KeyboardInterrupt):
            logger.error("Aborted.")
            _save_debug_log(output_dir)
            return 1

    _patch_session_for_logging(session)

    # Fetch profile metadata (non-fatal — comments can proceed without it)
    profile, initial_links, end_cursor, user_id = None, [], None, None
    try:
        fetched_profile, fetched_links, fetched_cursor, fetched_uid = fetch_profile_info(session, args.username)
        profile = fetched_profile
        initial_links = fetched_links
        end_cursor = fetched_cursor
        user_id = fetched_uid
        logger.info("Resolved @%s", profile["username"])
    except (ExportError, requests.RequestException) as exc:
        if is_rate_limited(exc):
            raise
        logger.warning("Could not resolve profile via API: %s", exc)
        # Fallback: HTML scrape + search API
        try:
            scraped_profile, scraped_links, _, scraped_uid = _scrape_profile_html(args.username, session=session)
            profile = scraped_profile or profile
            initial_links = scraped_links or initial_links
            user_id = user_id or scraped_uid
        except (ExportError, requests.RequestException) as inner:
            if is_rate_limited(inner):
                raise
            logger.debug("HTML scrape also failed: %s", inner)
        if not user_id:
            user_id = _resolve_user_id_via_search(args.username, session=session) or user_id

    # Collect all post links
    username = profile["username"] if profile else args.username
    cached = load_post_links(output_dir, username) if not args.refresh else {"links": [], "cursor": None}
    saved_cursor = cached.get("cursor")
    if cached["links"] and saved_cursor is not None and not cached.get("completed") and not cached.get('selection'):
        initial_links = []
        end_cursor = None
    else:
        _debug_event("timeline.initial", "Fetching first page of posts")
        initial_links, end_cursor, has_next = fetch_authenticated_timeline_page(session, username, None, user_id=user_id or None)
        if has_next and end_cursor is None:
            raise ExportError("Instagram returned no next-page cursor. The interaction timeline is incomplete.")
        if not has_next:
            end_cursor = None

    mediacount = profile.get("mediacount") if profile else 0
    all_links = collect_post_links(
        session=session,
        username=username,
        total_available=int(mediacount or 0),
        output_dir=output_dir,
        args=args,
        seed_links=initial_links,
        initial_cursor=end_cursor,
        user_id=user_id or None,
    )
    logger.info("Collected %s post URLs", len(all_links))
    selected_links = select_post_links(filter_links_by_date(all_links, args, session, output_dir), args)

    if args.dry_run:
        logger.info("Dry-run: %s/%s posts selected for comments export", len(selected_links), len(all_links))
        _save_debug_log(output_dir)
        return 0

    passive_reports(args, output_dir, session, selected_links)
    if is_likes:
        return export_likers(args, session, output_dir, username, selected_links)

    return export_comments(args, session, output_dir, username, selected_links)


def run_browser_interactions(args, browser, output_dir):
    session = browser.authenticated_session()
    _patch_session_for_logging(session)
    session._igdump_profile_transport = browser.require_context().request
    try:
        profile, _, _, uid = resolve_profile_for_posts(session, browser, args.username)
    finally:
        del session._igdump_profile_transport
    cached = load_post_links(output_dir, args.username) if not args.refresh else {'links': [], 'cursor': None}
    if cached['links'] and cached.get('cursor') and not cached.get('completed') and not cached.get('selection'):
        links, cursor = [], None
    else:
        links, cursor, more = fetch_authenticated_timeline_page(session, args.username, None, user_id=uid)
        if more and not cursor:
            raise ExportError('Instagram did not return a next-page cursor.')
        if not more:
            cursor = None
    links = collect_post_links(session, args.username, int(profile.get('mediacount') or 0), output_dir, args, links, cursor, uid)
    selected = select_post_links(filter_links_by_date(links, args, session, output_dir), args)
    if args.dry_run:
        logger.info('Dry-run: %s/%s posts selected', len(selected), len(links))
        return 0
    passive_reports(args, output_dir, session, selected)
    return export_likers(args, session, output_dir, args.username, selected) if args.mode == 'likes' else export_comments(args, session, output_dir, args.username, selected)


def export_comments(args, session, output_dir, username, selected_links):
    store = CommentStore(output_dir / '.comments.sqlite', username)
    try:
        store.migrate(output_dir / '.comments.json')
        return _export_comments_store(args, session, output_dir, username, selected_links, store)
    finally:
        session._igdump_comment_page_callback = None
        session._igdump_comment_cursor = None
        store.close()


def _export_comments_store(args, session, output_dir, username, selected_links, store):
    kind = "comments"
    delay = args.comment_delay

    comments_file = output_dir / f".{kind}.json"
    all_cached_records, completed_posts = store.snapshot()
    session._igdump_comment_page_callback = lambda code, rows, cursor: store.add_page(code, [build_comment_record(row, code) for row in rows], cursor)
    selected_codes = {parse_shortcode_from_url(url) for url in selected_links}
    timeline_state = read_json(post_links_path(output_dir))
    errors: list[str] = [] if timeline_state is None or timeline_satisfies_selection(output_dir, args) else ["timeline incomplete"]
    for index, post_url in enumerate(selected_links, start=1):
        shortcode = parse_shortcode_from_url(post_url)
        if shortcode in completed_posts and not args.refresh:
            update_progress("Comments", index, len(selected_links), "cached")
            continue
        logger.info("%s %s/%s: %s", "Comments", index, len(selected_links), shortcode)
        if args.refresh:
            store.reset(shortcode)
        session._igdump_comment_cursor = store.state(shortcode).get('cursor')
        try:
            raw_comments = fetch_all_comments(session, shortcode, delay)
            records = [build_comment_record(c, shortcode) for c in raw_comments]
            if records:
                store.add_page(shortcode, records, None)
            store.mark(shortcode, 'complete')
            if args.refresh:
                store.finish_refresh(shortcode, True)
        except (ExportError, requests.RequestException, ValueError) as exc:
            logger.error("Could not collect %s for %s: %s", "comments", shortcode, exc)
            errors.append(shortcode)
            store.mark(shortcode, 'failed', str(exc))
            if args.refresh:
                store.finish_refresh(shortcode, False)
            all_cached_records, completed_posts = store.snapshot()
            save_comment_cache(comments_file, username, all_cached_records, completed_posts)
            update_progress("Comments", index, len(selected_links), "failed")
            if is_rate_limited(exc) or isinstance(exc, InstagramAccessError):
                args._rate_limited = True
                break
            continue
        all_cached_records, completed_posts = store.snapshot()
        save_comment_cache(comments_file, username, all_cached_records, completed_posts)
        logger.info("Saved %s records", len(records))
        update_progress("Comments", index, len(selected_links))

    all_comment_records = [record for record in all_cached_records if record.get("post_shortcode") in selected_codes]
    logger.info("Processed %s of %s posts; records: %s; errors: %s",
                len(selected_codes & completed_posts), len(selected_codes), len(all_comment_records), len(errors))
    _debug_event("comments.done", "Comments export finished", count=len(all_comment_records), errors=errors)

    stats = aggregate_comment_stats(all_comment_records, len(selected_links))
    _save_comments_csv(all_comment_records, output_dir)
    write_interaction_report(args, output_dir, stats, f"Processed {len(selected_codes & completed_posts)} of {len(selected_codes)} posts. Errors: {len(errors)}.")
    _save_debug_log(output_dir)
    return 4 if errors else 0


def write_interaction_report(args, output_dir, stats, coverage, issues=None):
    likes = args.mode == "likes"
    label = "Likes" if likes else "Comments"
    user_rows = []
    for i, user in enumerate(stats["top_commenters"], 1):
        name = escape(user["username"])
        extra = "" if likes else f'<td>{user["posts_commented"]}</td>'
        user_rows.append(f'<tr><td>{i}</td><td><a href="https://www.instagram.com/{name}/">{name}</a><span class="name">{escape(user["full_name"])}</span></td><td>{user["count"]}</td>{extra}</tr>')
    post_rows = []
    for i, post in enumerate(stats["top_posts"][:10], 1):
        code = escape(post["shortcode"])
        extra = f'<td>{post.get("collected", post["count"])}</td>' if likes else ""
        value = str(post["count"]) + (" (collected)" if likes and post.get("estimated") else "")
        post_rows.append(f'<tr><td>{i}</td><td><a href="https://www.instagram.com/p/{code}/">{code}</a></td><td>{value}</td>{extra}</tr>')
    template = Template((TEMPLATE_DIR / "interactions.html").read_text(encoding="utf-8"))
    issue_html = ""
    if issues:
        rows = "".join(f'<tr><td><a href="https://www.instagram.com/p/{escape(code)}/">{escape(code)}</a></td><td>{escape(reason)}</td></tr>' for code, reason in issues)
        issue_html = f'<details><summary>Incomplete posts: {len(issues)} — reasons</summary><div class="tables"><table><thead><tr><th>Post</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table></div></details>'
    html = template.substitute(title=escape(f"{label} — @{args.username}"), kind=args.mode, label=label,
        date=datetime.now().strftime("%d.%m.%Y %H:%M"), range=escape(date_label(args.after, args.before) or "All publication dates"),
        coverage=escape(coverage), total=stats["total_comments"], users=stats["unique_commenters"], posts=stats["total_posts"],
        extra_link='<a href="likes-coverage.csv">Coverage by post · CSV</a>' if likes else "",
        users_title="Top users by likes" if likes else "Top commenters", posts_title="Top-10 most liked posts" if likes else "Top-10 most commented posts",
        user_extra_head="" if likes else "<th>Posts</th>", post_extra_head="<th>Collected users</th>" if likes else "",
        user_rows="".join(user_rows), post_rows="".join(post_rows), issues=issue_html,
        ranking_note=("Ranked by Instagram's reported count where available; cached posts without a count use collected users. Counts and accessible lists may differ." if likes else "Based on collected comments. Replies may not be included."))
    path = output_dir / f"{args.mode}.html"
    atomic_write(path, html)
    _save_participant_stats(stats, output_dir, args.mode)
    logger.info("Report saved: %s", path)
    print(path.as_uri())
    open_export(path, args)
    return path


def export_likers(args, session, output_dir, username, selected_links):
    store = LikerStore(output_dir / ".likers.sqlite", username)
    try:
        store.migrate(output_dir / ".likers.json")
        metadata_path = output_dir / ".post-likes.json"
        post_counts = save_post_metadata(session, output_dir, username)
        # Full exports already have post counters: reuse them without a metadata request.
        post_state = read_json(output_dir / ".export-state.json") or {}
        if post_state.get("job", {}).get("username") == username:
            for post in post_state.get("post_records", []):
                label = post.get("likes_label", "").removeprefix("Likes: ").replace(" ", "")
                if label.isdecimal():
                    post_counts.setdefault(post["shortcode"], int(label))
        write_json(metadata_path, {"username": username, "counts": post_counts})
        codes = [parse_shortcode_from_url(url) for url in selected_links]
        store.select(set(codes))
        failed = []
        offline = getattr(args, "offline", False)
        session._igdump_liker_page_callback = store.add_page
        for index, code in enumerate(codes, 1):
            state = store.state(code)
            if code in post_counts:
                count = post_counts[code]
                store.mark(code, "partial" if state["state"] == "complete" and store.count(code) < count else state["state"], expected=count)
                state = store.state(code)
            update_progress("Users who liked posts", index - 1, len(codes), code)
            if offline or (not args.refresh and state["state"] == "complete"):
                continue
            if args.refresh:
                store.reset(code)
                state = store.state(code)
            session._igdump_liker_resume_cursor = state.get("cursor") if state["state"] in ("failed", "collecting") else None
            try:
                recheck = getattr(args, 'recheck_likes', True)
                for check in range(2 if recheck else 1):
                    previous_count = store.count(code)
                    users = fetch_all_likers(session, code, args.like_delay)
                    # Also supports callers that return records without page callbacks.
                    if users:
                        store.add_page(code, users, None, None)
                    expected = store.state(code).get("expected")
                    counts = getattr(session, "_igdump_liker_counts", None)
                    if isinstance(counts, dict) and counts.get(code) is not None:
                        expected = counts[code]
                    if expected is None and code in post_counts:
                        expected = post_counts[code]
                    if expected is not None:
                        post_counts[code] = expected
                    collected = store.count(code)
                    partial = expected is not None and collected < expected
                    if not partial:
                        store.mark(code, "complete", expected)
                        break
                    if check == 0 and recheck and collected > previous_count:
                        logger.info("%s: collected %s/%s users. Automatically rechecking the incomplete list once.", code, collected, expected)
                        session._igdump_liker_resume_cursor = None
                        continue
                    reason = (f"Instagram ended the list without a next-page cursor: {collected} of {expected} users collected. "
                              + ("Immediate recheck disabled. " if not recheck else "The automatic recheck returned no additional users. " if collected == previous_count else "The automatic recheck still returned an incomplete list. ")
                              + "The API does not identify the missing accounts or provide another page; the exact reason for the counter/list mismatch is unavailable.")
                    store.mark(code, "partial", expected, error=reason)
                    logger.warning("%s: %s", code, reason)
                    break
                if args.refresh:
                    store.finish_refresh(code)
            except (ExportError, requests.RequestException, ValueError) as exc:
                if args.refresh:
                    store.restore_refresh()
                reason = str(exc)
                if isinstance(exc, requests.RequestException):
                    attempts = getattr(session, "_igdump_liker_attempts", 1)
                    reason = f"Request failed after {attempts} attempt(s): {exc}. Collected pages are saved; repeat the command to resume."
                else:
                    reason += " Collected pages are saved."
                store.mark(code, "failed", error=reason)
                failed.append(code)
                logger.error("%s: %s", code, reason)
                if is_rate_limited(exc) or (isinstance(exc, ExportError) and "denied access" in str(exc)):
                    args._rate_limited = True
                    break
        update_progress("Users who liked posts", len(codes), len(codes), "calculating statistics")
        states = [store.state(code) for code in codes]
        complete = sum(state["state"] == "complete" for state in states)
        partial = sum(state["state"] == "partial" for state in states)
        pending = len(codes) - complete - partial
        issues = []
        for code, state in zip(codes, states):
            if state["state"] != "complete":
                reason = state.get("error")
                if not reason:
                    reason = ("Cached incomplete list; offline mode cannot retry requests." if offline else
                              "Not requested: collection stopped after Instagram denied or limited access.")
                issues.append((code, reason))
        reasons = dict(issues)
        def coverage_rows():
            for code, state in zip(codes, states):
                expected, count = state.get("expected"), store.count(code)
                yield {"post_shortcode": code, "state": state["state"], "reported_likes": expected,
                       "collected_users": count, "missing": max(0, expected-count) if expected is not None else "",
                       "reason": reasons.get(code, "")}
        write_csv(output_dir / "likes-coverage.csv", ["post_shortcode", "state", "reported_likes", "collected_users", "missing", "reason"], coverage_rows())
        write_json(metadata_path, {'username': username, 'counts': post_counts, 'observed_at': datetime.now(UTC).isoformat()})
        stats = store.stats()
        _save_likers_csv(store.rows(), output_dir)
        timeline = load_post_links(output_dir, username)
        note = f"Complete lists: {complete}. Partial lists: {partial}. Unfinished posts: {pending}. "
        if not timeline_satisfies_selection(output_dir, args):
            note += "The profile timeline is incomplete. "
        note += "Per-post reasons are listed below and in the coverage CSV. Only observed users returned by Instagram are counted. Cached lists may have been collected at different times; these are archived observations, not a live snapshot of current likes."
        write_interaction_report(args, output_dir, stats, note, issues=issues)
        _save_debug_log(output_dir)
        return 4 if failed or partial or pending or not timeline_satisfies_selection(output_dir, args) else 0
    finally:
        session._igdump_liker_page_callback = None
        store.close()


def export_cached_comments(args, output_dir, links):
    if (output_dir / '.comments.sqlite').exists():
        store = CommentStore(output_dir / '.comments.sqlite', args.username)
        try:
            store.migrate(output_dir / '.comments.json')
            records, completed = store.snapshot()
        finally:
            store.close()
    else:
        records, completed = load_comment_cache(output_dir / ".comments.json", args.username)
    codes = {parse_shortcode_from_url(url) for url in links}
    records = [row for row in records if row.get("post_shortcode") in codes]
    stats = aggregate_comment_stats(records, len(codes))
    _save_comments_csv(records, output_dir)
    write_interaction_report(args, output_dir, stats, f"Cached posts: {len(codes & completed)} of {len(codes)}. Offline recalculation; no network requests.")
    return 0 if codes <= completed else 4


def run_full(args):
    root = ensure_output_dir(args.output_dir, args)
    phases = []
    failure_reasons = []
    for mode, title, filename in (("full", "Posts", "index.html"), ("comments", "Commenters", "comments.html"), ("likes", "Likes", "likes.html")):
        phase = argparse.Namespace(**vars(args))
        phase.mode, phase.output_dir, phase.no_open = mode, str(root), True
        phase._skip_insights = True
        phase_started = time.monotonic()
        result = 2
        # Reuse the timeline collected by the first phase rather than refreshing it three times.
        try:
            result = run(phase) if mode == "full" else run_comments(phase)
        except (ExportError, requests.RequestException) as exc:
            logger.error("%s: %s", title, exc)
            failure_reasons.append(f"{title}: {exc}")
            result = 2
            phase._rate_limited = is_rate_limited(exc)
        finally:
            _debug_event('phase.done', title, elapsed_ms=int((time.monotonic()-phase_started)*1000), result=result)
            if getattr(phase, "_shared_session", None) is not None:
                args._shared_session = phase._shared_session
            if getattr(phase, "_selected_links", None) is not None:
                args._selected_links = phase._selected_links
            if hasattr(phase,'_highlights_result'):
                args._highlights_result=phase._highlights_result
        phases.append((title, filename, result))
        if mode == "full" and result and getattr(args, "_selected_links", None) is None:
            logger.error("Commenters and likes skipped: the post timeline could not be collected.")
            phases.extend([("Commenters (skipped: timeline unavailable)", "comments.html", 2),
                           ("Likes (skipped: timeline unavailable)", "likes.html", 2)])
            break
        if getattr(phase, "_rate_limited", False):
            logger.warning("Remaining phases postponed because Instagram limited access. Repeat full later.")
            remaining = [("Commenters", "comments.html"), ("Likes", "likes.html")] if mode == "full" else [("Likes", "likes.html")] if mode == "comments" else []
            phases.extend((name + " (skipped: Instagram limited access)", file, 2) for name, file in remaining)
            break
        if args.dry_run:
            return result
    try:
        if getattr(args, "_selected_links", None) is not None or not any(result for _, _, result in phases):
            phases.extend(passive_reports(args, root, getattr(args, "_shared_session", None)))
    except ExportError as exc:
        logger.warning("Passive reports: %s", exc)
        phases.append(("Passive reports", "analytics.html", 2))
    state = read_json(export_state_path(root)) or {}
    selected_links = getattr(args, "_selected_links", None)
    if selected_links is not None and (state.get("job") or {}).get("username") == args.username and (root / "index.html").exists():
        codes = {parse_shortcode_from_url(url) for url in selected_links}
        records = [record for record in state.get("post_records", []) if record.get("shortcode") in codes]
        # Keep the order chosen by --oldest/--limit, even for a cached subset.
        by_code = {record["shortcode"]: record for record in records}
        records = [by_code[code] for url in selected_links if (code := parse_shortcode_from_url(url)) in by_code]
        generate_html(state.get("profile") or {"username": args.username}, records, state.get("avatar_src") or "",
                      build_mode_label(args), root, args.batch_size,
                      report_names=enabled_report_names(args))
    rows = []
    for title, filename, result in phases:
        label = f'<a href="{filename}">{title}</a>' if (root / filename).exists() else title
        rows.append(f'<li>{label} — {"complete" if result == 0 else "partial or failed; see console and coverage"}</li>')
    if failure_reasons:
        rows.insert(0, "<li>Collection failed: " + "<br>".join(escape(reason) for reason in failure_reasons) + "</li>")
    path = root / "full.html"
    atomic_write(path, '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Full export</title><style>body{font:16px system-ui;margin:32px auto;padding:0 24px;max-width:960px}li{margin:16px 0}a{color:#c02e65}</style>' + f'<h1>Full export — @{escape(args.username)}</h1><ul>{"".join(rows)}</ul></html>')
    _save_debug_log(root)
    print(path.as_uri())
    if any(result == 0 for _, _, result in phases[:3]):
        open_export(path, args)
    return 4 if any(result for _, _, result in phases) else 0


def _save_likers_csv(records, output_dir: Path) -> None:
    write_csv(output_dir / "likes.csv", ["post_shortcode", "user_id", "username", "full_name"], records)


def _save_participant_stats(stats: dict[str, Any], output_dir: Path, kind: str) -> None:
    is_likes = kind == "likes"
    fields = ["username", "full_name", "likes_count" if is_likes else "comment_count", "posts_commented"]
    if is_likes:
        fields = fields[:3]
    else:
        fields += ["likes_received", "last_comment_at"]
    def rows():
        for user in stats["participants"]:
            row = {"username": user["username"], "full_name": user["full_name"],
                   fields[2]: user["count"]}
            if not is_likes:
                row["posts_commented"] = user["posts_commented"]
                timestamp = user["last_comment_ts"]
                row.update(likes_received=user["likes_received"], last_comment_at=datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else "")
            yield row
    write_csv(output_dir / f"{kind}-stats.csv", fields, rows())


def _save_comments_csv(records: list[dict[str, Any]], output_dir: Path) -> None:
    path = output_dir / "comments.csv"
    fieldnames = [
        "post_shortcode", "username", "full_name", "text",
        "created_at", "like_count", "child_comment_count", "comment_id",
    ]
    write_csv(path, fieldnames, records)
    logger.info("CSV saved to %s (UTF-8 BOM)", path)


if __name__ == "__main__":
    raise SystemExit(main())
