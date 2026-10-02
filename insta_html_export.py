#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import random
import re
import time
import sys
import webbrowser
import traceback
from datetime import UTC, datetime
from html import escape
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from pathlib import Path
from typing import Any
from collections import deque
from urllib.parse import urlparse
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
from igdump_storage import read_json, write_json, load_comment_cache, save_comment_cache, LikerStore
from igdump_dates import parse_date, post_date, date_matches, date_label
from igdump_progress import update_progress, TerminalUI, record_request, prompt_input
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
TEMPLATE_DIR = Path(__file__).parent / "igdump_assets"
HTML_TEMPLATE = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")

try:
    VERSION = _pkg_version("igdump")
except PackageNotFoundError:
    VERSION = "0.5.1"


class ExportError(RuntimeError):
    pass


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
    username = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        raise argparse.ArgumentTypeError("Enter a username such as @kharlamova_alena, without a URL or spaces.")
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
        description="Export Instagram posts, comments, likes or everything together.",
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
      Posts and both statistics for the specified date range.

Commands choose what to collect. Options choose which posts to use.
No --limit (or --limit 0) means all posts. --oldest selects earliest posts.
Dates include both boundary days and apply before --limit.
--oldest requires the complete timeline; incomplete timelines stop selection.
Quote @usernames in PowerShell. The @ prefix is optional.
Without the installed command, use: python insta_html_export.py posts USERNAME
Command help: igdump posts --help
Legacy post commands: all = posts; oldest = posts --oldest --limit N.
""",
    )
    parser.add_argument("--version", action="version", version=f"igdump {VERSION}")
    parser.set_defaults(batch_size=9, headful=False, download_videos=False,
                        sessionid=None, comment_delay=2.0, like_delay=2.0,
                        offline=False, retry_missing=False)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("username", type=instagram_username, metavar="USERNAME", help="Instagram username, with or without @.")
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
        help="Export posts and both participation statistics.",
        description="Posts, comments and users who liked the same selected posts. All phases use the Chrome session. full.html links to the three reports and opens after collection.")
    for command in (posts, full):
        command.add_argument("--download-videos", action="store_true", help="Download videos; by default, save only photos and video covers.")
        command.add_argument("--batch-size", type=positive_int, default=9, metavar="N", help="Cards loaded per scroll batch in the post archive (default: 9). Does not change request batching.")
        command.add_argument("--headful", action="store_true", help="Keep Chrome visible throughout post collection.")
    for command in (comments, likes):
        command.add_argument("--sessionid", default=None, help="Instagram sessionid cookie for collection without Chrome. A saved session is used automatically.")
    for command in (comments, full):
        command.add_argument("--comment-delay", type=nonnegative_delay, default=2.0, metavar="SECONDS", help="Delay between comment requests (default: 2 seconds).")
    for command in (likes, full):
        command.add_argument("--like-delay", type=nonnegative_delay, default=2.0, metavar="SECONDS", help="Delay between like-list requests, including automatic rechecks (default: 2 seconds).")
        command.add_argument("--retry-missing", action="store_true", help=argparse.SUPPRESS)
    for command in (comments, likes):
        command.add_argument("--offline", action="store_true", help="Rebuild HTML and CSV from the original collection folder without login or HTTP requests. Keep the same selection or specify --output-dir.")

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
    if legacy == "oldest" and parsed.limit <= 0:
        parser.error("The legacy oldest command requires --limit N greater than 0. Use posts --oldest for an unlimited selection.")
    if parsed.after and parsed.before and parsed.after > parsed.before:
        parser.error("--after must not be later than --before.")
    if parsed.offline and (parsed.refresh or parsed.dry_run or parsed.retry_missing):
        parser.error("--offline cannot be combined with --refresh, --dry-run or --retry-missing.")
    return parsed


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "export"


def build_job_slug(args: argparse.Namespace) -> str:
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
    }


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
    destination_base.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        response = None
        partial: Path | None = None
        try:
            response = session.get(url, timeout=30, stream=True)
            response.raise_for_status()
            suffix = extension_for_response(url, response)
            destination = destination_base.with_suffix(suffix)
            partial = destination.with_suffix(destination.suffix + ".part")
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
            partial.replace(destination)
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
            raise ExportError(
                "Instagram is temporarily rate-limiting this browser session before export start. Wait and retry later."
            )
    finally:
        page.close()


def _scrape_profile_html(username: str) -> tuple[dict[str, Any], list[str], str | None, str]:
    """Scrape profile info from Instagram's public HTML page (no API call).

    Falls back to parsing the server-rendered <script> data and <meta> tags.
    """
    _debug_event("profile.source", "HTML scrape", username=username)
    sess = _make_session()
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    resp = sess.get(f"{IG_BASE_URL}/{username}/", timeout=30)
    resp.raise_for_status()
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
                or data.get("pk")
                or ""
            )

            profile = {
                "username": user_obj.get("username") or username,
                "full_name": user_obj.get("full_name") or username,
                "biography": user_obj.get("biography") or "",
                "mediacount": (user_obj.get("edge_owner_to_timeline_media") or {}).get("count"),
                "followers": (user_obj.get("edge_followed_by") or {}).get("count"),
                "followees": (user_obj.get("edge_follow") or {}).get("count"),
                "avatar_url": user_obj.get("profile_pic_url_hd") or user_obj.get("profile_pic_url") or "",
            }
            _debug_event("profile.init_state_ok", f"links={len(links)}, uid={uid!r}", links_count=len(links), uid=uid or None)
            if not uid:
                uid = _resolve_user_id_via_search(username)
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
    scraped_username = (re.search(r"@(\w+)", raw).group(1) if re.search(r"@(\w+)", raw) else username)

    uid = _extract_user_id_from_html(text)

    profile = {
        "username": scraped_username,
        "full_name": display_name,
        "biography": "",
        "mediacount": None,
        "followers": None,
        "followees": None,
        "avatar_url": "",
    }
    _debug_event("profile.regex_fallback", f"links={len(links)}, uid={uid!r}", links_count=len(links), uid=uid or None)

    # 3. If still no user_id, try search API
    if not uid:
        uid = _resolve_user_id_via_search(username)

    return profile, links, None, uid


def _extract_user_id_from_html(text: str) -> str:
    """Try multiple methods to find the numeric user id in HTML."""
    # Method 1: ld+json structured data (most reliable)
    for ld_m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', text, re.DOTALL):
        try:
            ld = json.loads(ld_m.group(1))
            if isinstance(ld, dict):
                val = ld.get("identifier") or ""
                if val:
                    return str(val)
        except (json.JSONDecodeError, TypeError):
            continue

    # Method 2: regex patterns on raw HTML
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


def _resolve_user_id_via_search(username: str) -> str:
    """Resolve numeric user_id via Instagram's search/topsearch API.

    This endpoint has different rate-limit characteristics from web_profile_info.
    """
    sess = _make_session()
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
            if resp.status_code == 429:
                _debug_event("search.429", f"search API rate limited, retrying in 10s")
                _safe_sleep(10, jitter=0.2)
                continue
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
            if attempt == 0:
                _safe_sleep(3)
                continue
            return ""
    return ""


def fetch_profile_info(session: requests.Session, username: str) -> tuple[dict[str, Any], list[str], str | None, str]:
    response = session.get(
        f"{IG_BASE_URL}/api/v1/users/web_profile_info/?username={username}",
        timeout=30,
    )
    if response.status_code == 429:
        logger.warning("API rate limited (429). Trying HTML scrape ...")
        _debug_event("profile.fallback", "API 429 → HTML scrape", username=username)
        return _scrape_profile_html(username)
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
        "biography": user.get("biography") or "No biography provided.",
        "mediacount": media.get("count") or 0,
        "followers": user.get("edge_followed_by", {}).get("count") or 0,
        "followees": user.get("edge_follow", {}).get("count") or 0,
        "avatar_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
    }
    end_cursor = media.get("page_info", {}).get("end_cursor")
    user_id = str(user.get("id"))
    return profile, initial_links, end_cursor, user_id


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
    if resp.status_code == 429:
        logger.warning("REST timeline rate limited (429). Waiting 30s ...")
        _safe_sleep(30, jitter=0.2)
        resp = session.get(
            f"{IG_BASE_URL}/api/v1/feed/user/{user_id}/",
            params=params,
            timeout=30,
            allow_redirects=False,
        )
    resp.raise_for_status()
    data = resp.json()

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

    response = session.post(
        f"{IG_BASE_URL}/graphql/query/",
        data={
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": AUTH_PROFILE_DOC_ID,
            "server_timestamps": "true",
        },
        timeout=30,
        allow_redirects=False,
    )
    if response.status_code in (302, 303, 307):
        raise ExportError("GraphQL endpoint redirected to login — session expired.")
    if response.status_code == 403:
        raise ExportError("GraphQL endpoint returned 403 (stale doc_id or missing csrftoken)")

    response.raise_for_status()
    payload = response.json()
    _doc_id_guard(payload, f"pagination for @{username}")

    media = payload.get("data", {}).get("xdt_api__v1__feed__user_timeline_graphql_connection")
    if not media:
        raise ExportError(
            f"Instagram did not return paginated profile media for @{username}. "
            "The internal GraphQL doc_id may have expired."
        )
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
    for retry in range(2):
        try:
            _debug_event("timeline.method", "Trying GraphQL", username=username)
            return _try_graphql_timeline(session, username, after_cursor)
        except (ExportError, requests.RequestException) as exc:
            if retry == 0:
                logger.warning("GraphQL failed (%s). Retrying once after 3s ...", exc)
                _safe_sleep(3, jitter=0.3)
                continue
            logger.warning("GraphQL failed (%s). Trying REST fallback ...", exc)
            _debug_event("timeline.fallback", f"GraphQL → REST: {exc}", username=username, user_id=user_id)

    # Fall back to REST if we have user_id
    if user_id:
        for retry in range(2):
            try:
                _debug_event("timeline.method", "Trying REST", user_id=user_id)
                return fetch_user_timeline_rest(session, user_id, after_cursor)
            except requests.RequestException as exc:
                if retry == 0:
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
    if cache["completed"] and (not total_available or len(links) >= total_available):
        logger.info("Loaded %s cached post links from %s", len(links), post_links_path(output_dir))
        return links

    # Determine starting cursor
    cached_cursor: str | None = cache.get("cursor")
    cursor: str | None = cached_cursor if cached_cursor is not None else initial_cursor

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
            raise ExportError(f"Instagram rate-limited post page access for {url}. Wait and retry later.")

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
) -> None:
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
    }

    raw_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
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
        bio=escape(profile.get("biography") or "No biography provided."),
        post_data=raw_json,
    )
    index_file = output_dir / "index.html"
    index_file.write_text(html, encoding="utf-8")
    logger.debug("HTML saved to %s", index_file)


def remember_post_dates(session: requests.Session, items: list[dict[str, Any]]) -> None:
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


def save_post_metadata(session, output_dir, username):
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
        value = post_date(items[0]) if items else None
        return value.isoformat() if value else None
    finally:
        response.close()


def filter_links_by_date(links: list[str], args: argparse.Namespace, session: requests.Session, output_dir: Path) -> list[str]:
    if not args.after and not args.before:
        return links
    path = output_dir / ".post-dates.json"
    payload = read_json(path) or {}
    dates = payload.get("dates", {}) if payload.get("username") == args.username and not args.refresh else {}
    if not isinstance(dates, dict):
        dates = {}
    timeline_dates = getattr(session, "_igdump_post_dates", None)
    if isinstance(timeline_dates, dict):
        dates.update(timeline_dates)
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
    selected.sort(key=lambda url: dates[parse_shortcode_from_url(url)], reverse=True)
    return selected


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
    profile_dir = Path(args.browser_profile_dir).expanduser().resolve()

    with InstagramBrowser(profile_dir=profile_dir, headful=args.headful) as browser:
        browser.ensure_logged_in()
        preflight_instagram_access(browser)
        session = browser.authenticated_session()
        _patch_session_for_logging(session)
        args._shared_session = session
        profile, _, _, user_id = fetch_profile_info(session, args.username)
        logger.info("Resolved @%s profile metadata", profile["username"])

        # Try to resume from saved cursor; if missing, fetch first page fresh.
        cached = load_post_links(output_dir, profile["username"]) if not args.refresh else {"links": [], "cursor": None}
        saved_cursor = cached.get("cursor")
        if cached["links"] and (saved_cursor is not None or cached.get("completed")):
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
        timeline_state = read_json(post_links_path(output_dir))
        timeline_complete = timeline_state is None or bool(timeline_state.get("completed"))
        if not timeline_complete:
            logger.warning("The timeline is incomplete. The archive will be partial; retry later.")

        filtered_links = filter_links_by_date(all_links, args, session, output_dir)
        selected_links = select_post_links(filtered_links, args)
        args._selected_links = selected_links
        if args.dry_run:
            logger.info("Dry-run: %s/%s posts selected for export", len(selected_links), len(all_links))
            return 0

        state = load_export_state(output_dir, args)
        existing_records = state.get("post_records", []) if isinstance(state.get("post_records"), list) else []
        records_by_shortcode = {
            record.get("shortcode"): record for record in existing_records if isinstance(record, dict)
        }

        avatar_name = state.get("avatar_src") if isinstance(state.get("avatar_src"), str) else None
        if avatar_name and (output_dir / avatar_name).exists():
            avatar_file = output_dir / avatar_name
        else:
            avatar_url = profile.get("avatar_url")
            avatar_file = output_dir / "avatar-placeholder.svg"
            if avatar_url:
                try:
                    avatar_file = download_binary(session, avatar_url, output_dir / "avatar")
                except (ExportError, requests.RequestException, OSError) as exc:
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
            save_post_metadata(session, output_dir, args.username)
            by_code = {record["shortcode"]: record for record in post_records}
            ordered = [by_code[code] for url in selected_links
                       if (code := parse_shortcode_from_url(url)) in by_code]
            save_export_state(output_dir, args, profile, avatar_name, ordered, completed=completed)
            generate_html(profile=profile, post_records=ordered, avatar_src=avatar_name,
                          mode_label=build_mode_label(args), output_dir=output_dir, batch_size=max(1, args.batch_size))

        checkpoint()
        errors: list[str] = []
        update_progress("Posts", len(post_records), len(selected_links))
        for index, post_url, shortcode in need_scrape:
            logger.info("Post %s/%s: %s", index, len(selected_links), shortcode)
            try:
                payload = scrape_post_payload(browser, post_url)
                remember_post_dates(session, [{**payload, "code": shortcode}])
                record = build_post_record(session, payload, output_dir / "media", index, post_url,
                                           download_videos=args.download_videos)
            except (ExportError, requests.RequestException, PlaywrightError, OSError) as exc:
                logger.error("Could not export %s: %s", shortcode, exc)
                errors.append(shortcode)
                update_progress("Posts", len(post_records) + len(errors), len(selected_links), "failed")
                continue
            post_records.append(record)
            checkpoint()
            update_progress("Posts", len(post_records) + len(errors), len(selected_links))

        completed = len(post_records) == len(selected_links) and not errors and timeline_complete
        checkpoint(completed)
        logger.info("Saved %s of %s posts; errors: %s. Archive: %s",
                    len(post_records), len(selected_links), len(errors), output_dir / "index.html")
        if errors:
            logger.warning("Repeat the same command to download missing posts: %s", ", ".join(errors))
        _save_debug_log(output_dir)
        print((output_dir / "index.html").as_uri())
        open_export(output_dir / "index.html", args)
        return 0 if completed else 4



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
        return execute(args)


def execute(args: argparse.Namespace) -> int:
    try:
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
        raise ExportError(
            "Instagram redirected to login — your sessionid is expired or invalid. "
            "Get a fresh one from your browser (F12 → Application → Cookies → sessionid) "
            "and pass it via --sessionid"
        )

    if response.status_code == 429:
        wait = 60
        logger.warning("Rate limited (429) on comments. Waiting %ss ...", wait)
        _safe_sleep(wait, jitter=0.1)
        response = session.get(
            f"{IG_BASE_URL}/api/v1/media/{media_id}/comments/",
            params=params,
            headers=COMMENTS_HEADERS,
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code in (302, 303, 307, 401):
            raise ExportError("Session expired or invalid (redirected to login after rate limit).")
        if response.status_code == 429:
            raise ExportError("Rate limited twice consecutively on comments. Aborting to protect your account.")

    response.raise_for_status()
    data = response.json()

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
                data = response.json()
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
    usernames = [c["username"] for c in comment_records]
    unique = len(set(usernames))

    # Per-user stats
    user_stats: dict[str, dict[str, Any]] = {}
    for c in comment_records:
        u = c["username"]
        if u not in user_stats:
            user_stats[u] = {
                "username": u,
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
    write_json(CONFIG_FILE, config)


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
            if resp.status_code == 429:
                _safe_sleep(5, jitter=0.2)
                continue
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
        if resp.status_code != 429:
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
        if is_likes:
            return export_likers(args, session, output_dir, args.username, links)
        return export_cached_comments(args, output_dir, links)
    shared_links = getattr(args, "_selected_links", None)
    shared_session = getattr(args, "_shared_session", None)
    if shared_links is not None and shared_session is not None:
        if is_likes:
            return export_likers(args, shared_session, output_dir, args.username, shared_links)
        return export_comments(args, shared_session, output_dir, args.username, shared_links)
    profile_dir = Path(args.browser_profile_dir).expanduser().resolve()

    session: requests.Session | None = getattr(args, "_shared_session", None)
    profile = None

    # 1. Try --sessionid flag (explicit)
    if session is None and args.sessionid:
        try:
            session = _session_from_sessionid(args.sessionid)
            _save_config({**{"sessionid": args.sessionid}})
            logger.info("Sessionid saved to %s", CONFIG_FILE)
        except ExportError as exc:
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
            logger.debug("Playwright failed: %s", exc)

    # 4. Prompt user interactively
    while session is None:
        logger.warning("No Instagram session found. Paste your sessionid cookie (F12 → Application → Cookies → sessionid):")
        try:
            raw = prompt_input("sessionid: ").strip()
            if not raw:
                logger.error("No sessionid provided.")
                _save_debug_log(output_dir)
                return 1
            try:
                session = _session_from_sessionid(raw)
                _save_config({"sessionid": raw})
                logger.info("Sessionid saved to %s", CONFIG_FILE)
            except ExportError as exc:
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
        logger.warning("Could not resolve profile via API: %s", exc)
        # Fallback: HTML scrape + search API
        try:
            scraped_profile, scraped_links, _, scraped_uid = _scrape_profile_html(args.username)
            profile = scraped_profile or profile
            initial_links = scraped_links or initial_links
            user_id = user_id or scraped_uid
        except (ExportError, requests.RequestException) as inner:
            logger.debug("HTML scrape also failed: %s", inner)
        if not user_id:
            user_id = _resolve_user_id_via_search(args.username) or user_id

    # Collect all post links
    username = profile["username"] if profile else args.username
    cached = load_post_links(output_dir, username) if not args.refresh else {"links": [], "cursor": None}
    saved_cursor = cached.get("cursor")
    if cached["links"] and (saved_cursor is not None or cached.get("completed")):
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

    if is_likes:
        return export_likers(args, session, output_dir, username, selected_links)

    return export_comments(args, session, output_dir, username, selected_links)


def export_comments(args, session, output_dir, username, selected_links):
    kind = "comments"
    delay = args.comment_delay

    comments_file = output_dir / f".{kind}.json"
    all_cached_records, completed_posts = load_comment_cache(comments_file, username)
    selected_codes = {parse_shortcode_from_url(url) for url in selected_links}
    timeline_state = read_json(post_links_path(output_dir))
    errors: list[str] = [] if timeline_state is None or timeline_state.get("completed") else ["timeline incomplete"]
    for index, post_url in enumerate(selected_links, start=1):
        shortcode = parse_shortcode_from_url(post_url)
        if shortcode in completed_posts and not args.refresh:
            update_progress("Comments", index, len(selected_links), "cached")
            continue
        logger.info("%s %s/%s: %s", "Comments", index, len(selected_links), shortcode)
        try:
            raw_comments = fetch_all_comments(session, shortcode, delay)
            records = [build_comment_record(c, shortcode) for c in raw_comments]
        except (ExportError, requests.RequestException, ValueError) as exc:
            logger.error("Could not collect %s for %s: %s", "comments", shortcode, exc)
            errors.append(shortcode)
            update_progress("Comments", index, len(selected_links), "failed")
            if "429" in str(exc) or "Rate limited" in str(exc):
                args._rate_limited = True
                break
            continue
        all_cached_records = [record for record in all_cached_records if record.get("post_shortcode") != shortcode]
        all_cached_records.extend(records)
        completed_posts.add(shortcode)
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
    path.write_text(html, encoding="utf-8")
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
                for check in range(2):
                    previous_count = store.count(code)
                    users = fetch_all_likers(session, code, args.like_delay)
                    # Also supports callers that return records without page callbacks.
                    if users:
                        store.add_page(code, users, None, None)
                    expected = store.state(code).get("expected")
                    counts = getattr(session, "_igdump_liker_counts", None)
                    if isinstance(counts, dict) and counts.get(code) is not None:
                        expected = counts[code]
                    if code in post_counts:
                        expected = max(post_counts[code], expected or 0)
                    collected = store.count(code)
                    partial = expected is not None and collected < expected
                    if not partial:
                        store.mark(code, "complete", expected)
                        break
                    if check == 0 and collected > previous_count:
                        logger.info("%s: collected %s/%s users. Automatically rechecking the incomplete list once.", code, collected, expected)
                        session._igdump_liker_resume_cursor = None
                        continue
                    reason = (f"Instagram ended the list without a next-page cursor: {collected} of {expected} users collected. "
                              + ("The automatic recheck returned no additional users. " if collected == previous_count else "The automatic recheck still returned an incomplete list. ")
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
                if isinstance(exc, ExportError) and ("429" in str(exc) or "denied access" in str(exc)):
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
        with (output_dir / "likes-coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["post_shortcode", "state", "reported_likes", "collected_users", "missing", "reason"])
            for code, state in zip(codes, states):
                expected = state.get("expected")
                count = store.count(code)
                writer.writerow([code, state["state"], expected if expected is not None else "", count,
                                 max(0, expected-count) if expected is not None else "", reasons.get(code, "")])
        stats = store.stats()
        _save_likers_csv(store.rows(), output_dir)
        timeline = load_post_links(output_dir, username)
        note = f"Complete lists: {complete}. Partial lists: {partial}. Unfinished posts: {pending}. "
        if not timeline.get("completed"):
            note += "The profile timeline is incomplete. "
        note += "Incomplete lists are automatically rechecked during online collection. Per-post reasons are listed below and in the coverage CSV. Only users returned by Instagram are counted."
        write_interaction_report(args, output_dir, stats, note, issues=issues)
        _save_debug_log(output_dir)
        return 4 if not offline and (failed or partial or pending or not timeline.get("completed")) else 0
    finally:
        session._igdump_liker_page_callback = None
        store.close()


def export_cached_comments(args, output_dir, links):
    records, completed = load_comment_cache(output_dir / ".comments.json", args.username)
    codes = {parse_shortcode_from_url(url) for url in links}
    records = [row for row in records if row.get("post_shortcode") in codes]
    stats = aggregate_comment_stats(records, len(codes))
    _save_comments_csv(records, output_dir)
    write_interaction_report(args, output_dir, stats, f"Cached posts: {len(codes & completed)} of {len(codes)}. Offline recalculation; no network requests.")
    return 0


def run_full(args):
    root = ensure_output_dir(args.output_dir, args)
    phases = []
    for mode, title, filename in (("full", "Posts", "index.html"), ("comments", "Commenters", "comments.html"), ("likes", "Likes", "likes.html")):
        phase = argparse.Namespace(**vars(args))
        phase.mode, phase.output_dir, phase.no_open = mode, str(root), True
        # Reuse the timeline collected by the first phase rather than refreshing it three times.
        try:
            result = run(phase) if mode == "full" else run_comments(phase)
            if getattr(phase, "_shared_session", None) is not None:
                args._shared_session = phase._shared_session
            if getattr(phase, "_selected_links", None) is not None:
                args._selected_links = phase._selected_links
        except (ExportError, requests.RequestException) as exc:
            logger.error("%s: %s", title, exc)
            result = 2
        phases.append((title, filename, result))
        if getattr(phase, "_rate_limited", False):
            logger.warning("Remaining phases postponed because Instagram limited access. Repeat full later.")
            break
        if args.dry_run:
            return result
    rows = []
    for title, filename, result in phases:
        label = f'<a href="{filename}">{title}</a>' if (root / filename).exists() else title
        rows.append(f'<li>{label} — {"complete" if result == 0 else "partial or failed; see console and coverage"}</li>')
    path = root / "full.html"
    path.write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Full export</title><style>body{font:16px system-ui;margin:32px auto;padding:0 24px;max-width:960px}li{margin:16px 0}a{color:#c02e65}</style>' + f'<h1>Full export — @{escape(args.username)}</h1><ul>{"".join(rows)}</ul></html>', encoding="utf-8")
    print(path.as_uri())
    open_export(path, args)
    return 4 if any(result for _, _, result in phases) else 0


def _save_likers_csv(records, output_dir: Path) -> None:
    with (output_dir / "likes.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["post_shortcode", "user_id", "username", "full_name"])
        writer.writeheader()
        writer.writerows({key: record.get(key, "") for key in writer.fieldnames} for record in records)


def _save_participant_stats(stats: dict[str, Any], output_dir: Path, kind: str) -> None:
    is_likes = kind == "likes"
    fields = ["username", "full_name", "likes_count" if is_likes else "comment_count", "posts_commented"]
    if is_likes:
        fields = fields[:3]
    else:
        fields += ["likes_received", "last_comment_at"]
    with (output_dir / f"{kind}-stats.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for user in stats["participants"]:
            row = {"username": user["username"], "full_name": user["full_name"],
                   fields[2]: user["count"]}
            if not is_likes:
                row["posts_commented"] = user["posts_commented"]
                timestamp = user["last_comment_ts"]
                row.update(likes_received=user["likes_received"], last_comment_at=datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else "")
            writer.writerow(row)


def _save_comments_csv(records: list[dict[str, Any]], output_dir: Path) -> None:
    path = output_dir / "comments.csv"
    fieldnames = [
        "post_shortcode", "username", "full_name", "text",
        "created_at", "like_count", "child_comment_count", "comment_id",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            row = {k: rec.get(k, "") for k in fieldnames}
            writer.writerow(row)
    logger.info("CSV saved to %s (UTF-8 BOM)", path)


if __name__ == "__main__":
    raise SystemExit(main())
