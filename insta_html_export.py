#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from playwright.sync_api import BrowserContext, Page, Playwright, TimeoutError, sync_playwright


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
SCROLL_PAUSE_MS = 1400
POST_WAIT_MS = 2500
NAVIGATION_TIMEOUT_MS = 30_000
MAX_IDLE_SCROLLS = 6
PUBLIC_PROFILE_DOC_ID = "7950326061742207"
AUTH_PROFILE_DOC_ID = "7898261790222653"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #fafafa;
      --surface: rgba(255, 255, 255, 0.92);
      --surface-strong: #ffffff;
      --text: #111111;
      --muted: #6b7280;
      --line: rgba(17, 17, 17, 0.08);
      --shadow: 0 24px 60px rgba(17, 24, 39, 0.08);
      --accent: #ff4f8b;
      --accent-2: #ff8a00;
      --radius: 26px;
      --feed-width: 540px;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      color: var(--text);
      font-family: "Avenir Next", "Helvetica Neue", Helvetica, Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(255, 79, 139, 0.10), transparent 32%),
        radial-gradient(circle at top right, rgba(255, 138, 0, 0.10), transparent 24%),
        linear-gradient(180deg, #fefefe 0%, #f8f8f8 50%, #f5f5f5 100%);
    }}

    a {{
      color: inherit;
    }}

    .topbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      backdrop-filter: blur(14px);
      background: rgba(255, 255, 255, 0.86);
      border-bottom: 1px solid var(--line);
    }}

    .topbar-inner {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }}

    .brand-mark {{
      width: 38px;
      height: 38px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%);
      box-shadow: 0 10px 24px rgba(255, 79, 139, 0.28);
      position: relative;
      flex: 0 0 auto;
    }}

    .brand-mark::before {{
      content: "";
      position: absolute;
      inset: 8px;
      border: 2px solid rgba(255, 255, 255, 0.95);
      border-radius: 10px;
    }}

    .brand-mark::after {{
      content: "";
      position: absolute;
      width: 7px;
      height: 7px;
      right: 8px;
      top: 8px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.95);
    }}

    .brand-copy {{
      min-width: 0;
    }}

    .brand-title {{
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .brand-meta {{
      margin-top: 2px;
      color: var(--muted);
      font-size: 0.92rem;
    }}

    .stats-pill {{
      flex: 0 0 auto;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(17, 17, 17, 0.04);
      font-size: 0.92rem;
      color: var(--muted);
      white-space: nowrap;
    }}

    .page {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 32px 24px 56px;
    }}

    .profile-card {{
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 24px;
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 32px;
      background: var(--surface);
      box-shadow: var(--shadow);
      margin-bottom: 28px;
    }}

    .avatar-wrap {{
      width: 112px;
      height: 112px;
      padding: 4px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%);
    }}

    .avatar {{
      display: block;
      width: 100%;
      height: 100%;
      border-radius: 50%;
      object-fit: cover;
      background: #ececec;
      border: 4px solid white;
    }}

    .profile-head {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px 16px;
      margin-bottom: 14px;
    }}

    .username {{
      font-size: clamp(1.4rem, 1.6vw, 1.7rem);
      font-weight: 700;
      letter-spacing: -0.04em;
    }}

    .realname {{
      color: var(--muted);
      font-size: 1rem;
    }}

    .meta-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 20px;
      margin-bottom: 14px;
      font-size: 0.95rem;
    }}

    .meta-grid strong {{
      color: var(--text);
    }}

    .bio {{
      white-space: pre-wrap;
      line-height: 1.5;
      color: #232323;
    }}

    .feed-shell {{
      display: flex;
      justify-content: center;
    }}

    .feed-column {{
      width: min(100%, var(--feed-width));
    }}

    .post-card {{
      margin-bottom: 22px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      background: var(--surface-strong);
      box-shadow: 0 20px 48px rgba(17, 24, 39, 0.08);
      transform: translateY(18px);
      opacity: 0;
      animation: slide-in 360ms ease forwards;
    }}

    .post-head {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 16px 18px;
    }}

    .post-head .avatar {{
      width: 40px;
      height: 40px;
      border: 0;
    }}

    .post-owner {{
      flex: 1 1 auto;
      min-width: 0;
    }}

    .post-owner strong,
    .post-owner span {{
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .post-owner span {{
      margin-top: 2px;
      color: var(--muted);
      font-size: 0.88rem;
    }}

    .post-link {{
      flex: 0 0 auto;
      color: var(--muted);
      text-decoration: none;
      font-size: 0.9rem;
    }}

    .post-media {{
      display: block;
      width: 100%;
      height: auto;
      object-fit: contain;
      background: #efefef;
    }}

    .post-body {{
      padding: 16px 18px 20px;
    }}

    .post-stats {{
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
      margin-bottom: 10px;
      font-size: 0.93rem;
    }}

    .post-stats span {{
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(17, 17, 17, 0.04);
    }}

    .caption {{
      white-space: pre-wrap;
      line-height: 1.55;
      overflow-wrap: anywhere;
    }}

    .caption-empty {{
      color: var(--muted);
      font-style: italic;
    }}

    .render-progress {{
      text-align: center;
      color: var(--muted);
      font-size: 0.93rem;
      margin: 12px 0 6px;
    }}

    .loader {{
      width: 100%;
      display: flex;
      justify-content: center;
      padding: 14px 0 0;
      color: var(--muted);
      font-size: 0.95rem;
    }}

    .loader.hidden {{
      display: none;
    }}

    .empty-state {{
      text-align: center;
      padding: 44px 24px;
      border: 1px dashed var(--line);
      border-radius: var(--radius);
      background: rgba(255, 255, 255, 0.8);
      color: var(--muted);
    }}

    @keyframes slide-in {{
      from {{
        opacity: 0;
        transform: translateY(18px);
      }}
      to {{
        opacity: 1;
        transform: translateY(0);
      }}
    }}

    @media (max-width: 760px) {{
      .topbar-inner,
      .page {{
        padding-left: 14px;
        padding-right: 14px;
      }}

      .profile-card {{
        grid-template-columns: 1fr;
        justify-items: center;
        text-align: center;
        gap: 18px;
      }}

      .profile-head,
      .meta-grid {{
        justify-content: center;
      }}

      .stats-pill {{
        display: none;
      }}

      .post-card {{
        border-radius: 22px;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"></div>
        <div class="brand-copy">
          <div class="brand-title">{brand_title}</div>
          <div class="brand-meta">{brand_meta}</div>
        </div>
      </div>
      <div class="stats-pill">{stats_pill}</div>
    </div>
  </header>

  <main class="page">
    <section class="profile-card">
      <div class="avatar-wrap">
        <img class="avatar" src="{avatar_src}" alt="Avatar @{username}">
      </div>
      <div>
        <div class="profile-head">
          <div class="username">@{username}</div>
          <div class="realname">{full_name}</div>
        </div>
        <div class="meta-grid">
          <div><strong>{post_count}</strong> posts</div>
          <div><strong>{followers}</strong> followers</div>
          <div><strong>{followees}</strong> following</div>
          <div><strong>{mode_label}</strong></div>
        </div>
        <div class="bio">{bio}</div>
      </div>
    </section>

    <section class="feed-shell">
      <div class="feed-column">
        <div class="render-progress" id="render-progress"></div>
        <div id="feed"></div>
        <div id="empty" class="empty-state" hidden>Посты не найдены.</div>
        <div id="loader" class="loader">Загружаю следующую порцию…</div>
        <div id="sentinel"></div>
      </div>
    </section>
  </main>

  <script id="post-data" type="application/json">{post_data}</script>
  <script>
    const payload = JSON.parse(document.getElementById("post-data").textContent);
    const posts = payload.posts || [];
    const feed = document.getElementById("feed");
    const empty = document.getElementById("empty");
    const sentinel = document.getElementById("sentinel");
    const loader = document.getElementById("loader");
    const progress = document.getElementById("render-progress");
    const batchSize = Math.max(1, payload.batch_size || 9);
    let cursor = 0;

    const escapeHtml = (value) => value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

    const formatCaption = (caption) => {{
      if (!caption) {{
        return '<div class="caption caption-empty">Без описания</div>';
      }}
      return `<div class="caption">${{escapeHtml(caption)}}</div>`;
    }};

    const renderProgress = () => {{
      if (!posts.length) {{
        progress.textContent = "";
        return;
      }}
      progress.textContent = `Показано ${{Math.min(cursor, posts.length)}} из ${{posts.length}} постов`;
    }};

    const createCard = (post, index) => {{
      const card = document.createElement("article");
      card.className = "post-card";
      card.style.animationDelay = `${{Math.min(index * 35, 220)}}ms`;
      card.innerHTML = `
        <div class="post-head">
          <img class="avatar" src="${{escapeHtml(payload.profile.avatar_src)}}" alt="">
          <div class="post-owner">
            <strong>@${{escapeHtml(payload.profile.username)}}</strong>
            <span>${{escapeHtml(post.date_label)}}</span>
          </div>
          <a class="post-link" href="${{escapeHtml(post.instagram_url)}}" target="_blank" rel="noreferrer">Instagram</a>
        </div>
        <img class="post-media" src="${{escapeHtml(post.local_media_path)}}" alt="${{escapeHtml(post.alt_text)}}" loading="lazy">
        <div class="post-body">
          <div class="post-stats">
            <span>${{escapeHtml(post.likes_label)}}</span>
            <span>${{escapeHtml(post.comments_label)}}</span>
            <span>${{escapeHtml(post.kind_label)}}</span>
          </div>
          ${{formatCaption(post.caption)}}
        </div>
      `;
      return card;
    }};

    const renderNextBatch = () => {{
      if (!posts.length) {{
        empty.hidden = false;
        loader.classList.add("hidden");
        return;
      }}

      const chunk = posts.slice(cursor, cursor + batchSize);
      chunk.forEach((post, idx) => feed.appendChild(createCard(post, idx)));
      cursor += chunk.length;
      renderProgress();

      if (cursor >= posts.length) {{
        loader.classList.add("hidden");
        observer.disconnect();
      }}
    }};

    const observer = new IntersectionObserver((entries) => {{
      for (const entry of entries) {{
        if (entry.isIntersecting) {{
          renderNextBatch();
        }}
      }}
    }}, {{ rootMargin: "220px 0px" }});

    renderNextBatch();
    observer.observe(sentinel);
  </script>
</body>
</html>
"""


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
        except TimeoutError:
            pass

        print(f"[auth] No saved Instagram session in {self.profile_dir}")
        print("[auth] A Chrome window was opened. Log into Instagram there, then return here.")
        try:
            input("Press Enter after the Instagram home page is fully loaded, or Ctrl+C to abort: ")
        finally:
            page.close()

        if not self.is_logged_in():
            raise ExportError(
                "Instagram login was not detected in the browser profile. Complete login in the opened Chrome window "
                "and rerun the command."
            )

        print(f"[auth] Instagram session saved in {self.profile_dir}")

    def open_page(self, url: str) -> Page:
        page = self.require_context().new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except TimeoutError:
            pass
        return page

    def authenticated_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                **IG_API_HEADERS,
                "User-Agent": IG_MOBILE_USER_AGENT,
            }
        )
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


def public_api_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            **IG_API_HEADERS,
            "User-Agent": IG_MOBILE_USER_AGENT,
        }
    )
    return session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Экспортирует Instagram-посты в статический HTML-фид через logged-in Chrome session."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("username", help="Instagram username без символа @")
    common_parent.add_argument(
        "--output-dir",
        default=None,
        help="Куда сохранить HTML-архив. По умолчанию: ./exports/<username>-all или ./exports/<username>-oldest-<N>.",
    )
    common_parent.add_argument(
        "--batch-size",
        type=int,
        default=9,
        help="Сколько карточек подгружать за один экран при прокрутке. По умолчанию 9.",
    )
    common_parent.add_argument(
        "--browser-profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Persistent Chrome profile для Instagram-сессии. По умолчанию ~/.insta-export/chrome-profile.",
    )
    common_parent.add_argument(
        "--headful",
        action="store_true",
        help="Открывать Chrome в видимом режиме на всём протяжении запуска.",
    )

    subparsers.add_parser(
        "all",
        parents=[common_parent],
        help="Выгрузить все посты пользователя. Порядок: как в Instagram, от новых к старым.",
    )

    oldest = subparsers.add_parser(
        "oldest",
        parents=[common_parent],
        help="Выгрузить только первые N постов пользователя, от самого раннего к самому позднему.",
    )
    oldest.add_argument("--limit", type=int, required=True, help="Сколько самых ранних постов выгрузить.")
    return parser.parse_args()


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "export"


def build_job_slug(args: argparse.Namespace) -> str:
    if args.mode == "all":
        return f"{safe_slug(args.username)}-all"
    return f"{safe_slug(args.username)}-oldest-{args.limit}"


def format_count(value: int | None) -> str:
    if value is None:
        return "0"
    return f"{value:,}".replace(",", " ")


def ensure_output_dir(base: str | None, args: argparse.Namespace) -> Path:
    if base:
        root = Path(base).expanduser().resolve()
    else:
        root = (Path.cwd() / "exports" / build_job_slug(args)).resolve()
    (root / "media").mkdir(parents=True, exist_ok=True)
    return root


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
            "version": 3,
            "job": {
                "username": args.username,
                "mode": args.mode,
                "limit": getattr(args, "limit", None),
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
    if (
        job.get("username") != args.username
        or job.get("mode") != args.mode
        or job.get("limit") != getattr(args, "limit", None)
    ):
        return {}
    return payload


def save_post_links(output_dir: Path, args: argparse.Namespace, links: list[str], total: int | None, completed: bool, cursor: str | None = None) -> None:
    write_json(
        post_links_path(output_dir),
        {
            "version": 1,
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
        "completed": bool(payload.get("completed")),
        "cursor": payload.get("cursor"),
    }


def existing_binary_path(destination_base: Path) -> Path | None:
    matches = sorted(destination_base.parent.glob(f"{destination_base.name}.*"))
    return matches[0] if matches else None


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
        try:
            response = session.get(url, timeout=30, stream=True)
            response.raise_for_status()
            suffix = extension_for_response(url, response)
            destination = destination_base.with_suffix(suffix)
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
            return destination
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 3:
                print(f"[retry] Media download failed on attempt {attempt}/3: {exc}. Retrying ...")
            else:
                break
        finally:
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


def fetch_profile_info(session: requests.Session, username: str) -> tuple[dict[str, Any], list[str], str | None, str]:
    response = session.get(
        f"{IG_BASE_URL}/api/v1/users/web_profile_info/?username={username}",
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    user = payload.get("data", {}).get("user")
    if not user:
        raise ExportError(f"Could not resolve profile @{username}. It may be private or unavailable.")

    media = user.get("edge_owner_to_timeline_media") or {}
    edges = media.get("edges") or []
    initial_links = []
    for edge in edges:
        node = edge.get("node") or {}
        shortcode = node.get("shortcode")
        if shortcode:
            initial_links.append(f"{IG_BASE_URL}/p/{shortcode}/")

    profile = {
        "username": user.get("username") or username,
        "full_name": user.get("full_name") or "Без имени",
        "biography": user.get("biography") or "Биография не указана.",
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


def fetch_public_timeline_page(
    session: requests.Session,
    user_id: str,
    after_cursor: str | None,
) -> tuple[list[str], str | None, bool]:
    variables: dict[str, Any] = {"id": user_id, "first": 12}
    if after_cursor:
        variables["after"] = after_cursor
    response = session.get(
        f"{IG_BASE_URL}/graphql/query/",
        params={
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": PUBLIC_PROFILE_DOC_ID,
            "server_timestamps": "true",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    media = payload.get("data", {}).get("user", {}).get("edge_owner_to_timeline_media")
    if not media:
        raise ExportError("Instagram did not return paginated profile media.")
    edges = media.get("edges") or []
    links = []
    for edge in edges:
        node = edge.get("node") or {}
        shortcode = node.get("shortcode")
        if shortcode:
            links.append(f"{IG_BASE_URL}/p/{shortcode}/")
    page_info = media.get("page_info") or {}
    return links, page_info.get("end_cursor"), bool(page_info.get("has_next_page"))


def fetch_authenticated_timeline_page(
    session: requests.Session,
    username: str,
    after_cursor: str | None,
) -> tuple[list[str], str | None, bool]:
    variables: dict[str, Any] = {
        "data": {
            "count": 12,
            "include_relationship_info": True,
            "latest_besties_reel_media": True,
            "latest_reel_media": True,
        },
        "username": username,
        "__relay_internal__pv__PolarisFeedShareMenurelayprovider": False,
    }
    if after_cursor:
        variables.update(
            {
                "after": after_cursor,
                "before": None,
                "first": 12,
                "last": None,
            }
        )
    response = session.post(
        f"{IG_BASE_URL}/graphql/query/",
        data={
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": AUTH_PROFILE_DOC_ID,
            "server_timestamps": "true",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    media = payload.get("data", {}).get("xdt_api__v1__feed__user_timeline_graphql_connection")
    if not media:
        raise ExportError("Instagram did not return authenticated paginated profile media.")
    edges = media.get("edges") or []
    links = []
    for edge in edges:
        node = edge.get("node") or {}
        shortcode = node.get("code") or node.get("shortcode")
        if shortcode:
            links.append(f"{IG_BASE_URL}/p/{shortcode}/")
    page_info = media.get("page_info") or {}
    return links, page_info.get("end_cursor"), bool(page_info.get("has_next_page"))


def collect_post_links(
    session: requests.Session,
    username: str,
    total_available: int,
    output_dir: Path,
    args: argparse.Namespace,
    seed_links: list[str],
    initial_cursor: str | None,
) -> list[str]:
    cache = load_post_links(output_dir, username)
    links = unique_keep_order([*cache["links"], *seed_links])
    if cache["completed"] and len(links) >= total_available > 0:
        print(f"[resume] Loaded {len(links)} cached post links from {post_links_path(output_dir)}")
        return links

    # If we have a saved cursor from a previous run, resume from it.
    # Otherwise use the caller-supplied initial_cursor (first-page result).
    cached_cursor: str | None = cache.get("cursor")
    cursor: str | None = cached_cursor if cached_cursor is not None else initial_cursor
    if cache["links"]:
        print(f"[resume] Resuming pagination from {len(links)} cached links, cursor={'<saved>' if cached_cursor else '<fresh>'}")

    idle_rounds = 0
    MAX_IDLE = 3

    # Continue as long as Instagram gives us a cursor, regardless of has_next_page flag.
    # Stop only when: cursor exhausted + no new links, OR we reached the declared total.
    while True:
        if total_available and len(links) >= total_available:
            break

        page_links, next_cursor, _has_next = fetch_authenticated_timeline_page(session, username, cursor)
        before = len(links)
        links = unique_keep_order(links + page_links)
        new_count = len(links) - before
        cursor = next_cursor

        save_post_links(output_dir, args, links, total_available, completed=False, cursor=cursor)

        if new_count:
            print(f"[fetch] Collected {len(links)}/{total_available or '?'} post links")
            idle_rounds = 0
        else:
            idle_rounds += 1
            if idle_rounds >= MAX_IDLE:
                print(f"[fetch] No new links after {MAX_IDLE} consecutive pages, stopping pagination")
                break

        if cursor is None:
            break

    if total_available and len(links) < total_available:
        print(f"[warn] Collected {len(links)} of {total_available} links before pagination stopped")
    save_post_links(output_dir, args, links, total_available, completed=bool(total_available and len(links) >= total_available), cursor=cursor)
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
        "taken_at": int(datetime.now(UTC).timestamp()),
        "image_versions2": {"candidates": [{"url": image_url}]} if image_url else {"candidates": []},
        "video_versions": [{"url": video_url}] if video_url else [],
        "user": {"username": urlparse(url).path.strip("/").split("/")[0]},
        "_fallback_timestamp": timestamp,
    }


def scrape_post_payload(browser: InstagramBrowser, url: str) -> dict[str, Any]:
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
        page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        assert_page_is_usable(page, url)
        page.wait_for_timeout(POST_WAIT_MS)
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


def build_post_record(session: requests.Session, payload: dict[str, Any], media_dir: Path, index: int, url: str) -> dict[str, Any]:
    shortcode = payload.get("code") or parse_shortcode_from_url(url)
    media_type = payload.get("media_type", 1)
    media_node = payload
    if media_type == 8 and payload.get("carousel_media"):
        media_node = payload["carousel_media"][0]

    image_candidates = media_node.get("image_versions2", {}).get("candidates") or []
    video_versions = media_node.get("video_versions") or []
    media_url = image_candidates[0]["url"] if image_candidates else ""
    if media_type == 2 and image_candidates:
        media_url = image_candidates[0]["url"]
    elif media_type == 2 and video_versions:
        media_url = video_versions[0].get("url", "")

    if not media_url:
        raise ExportError(f"Could not find media URL for {url}")

    media_target = media_dir / f"{index:04d}-{safe_slug(shortcode)}"
    local_media = download_binary(session, media_url, media_target)
    caption = ((payload.get("caption") or {}).get("text") or "").strip()
    taken_at = payload.get("taken_at")
    if isinstance(taken_at, int):
        date_value = datetime.fromtimestamp(taken_at, tz=UTC).astimezone()
    else:
        fallback_timestamp = payload.get("_fallback_timestamp")
        if fallback_timestamp:
            try:
                date_value = datetime.fromisoformat(fallback_timestamp.replace("Z", "+00:00")).astimezone()
            except ValueError:
                date_value = datetime.now().astimezone()
        else:
            date_value = datetime.now().astimezone()

    kind_label = {1: "Image", 2: "Video", 8: "Carousel"}.get(media_type, "Post")
    likes = payload.get("like_count")
    comments = payload.get("comment_count")
    alt_text = media_node.get("accessibility_caption") or payload.get("accessibility_caption") or caption[:120] or f"Instagram post {shortcode}"

    return {
        "shortcode": shortcode,
        "caption": caption,
        "local_media_path": f"media/{local_media.name}",
        "instagram_url": normalized_post_url(url),
        "date_label": date_value.strftime("%d.%m.%Y %H:%M"),
        "likes_label": f"Likes: {format_count(likes if isinstance(likes, int) else 0)}",
        "comments_label": f"Comments: {format_count(comments if isinstance(comments, int) else 0)}",
        "kind_label": kind_label,
        "alt_text": alt_text,
    }


def build_mode_label(args: argparse.Namespace) -> str:
    if args.mode == "all":
        return "Все посты, новые сверху"
    return f"Первые {args.limit} постов, ранние сверху"


def generate_html(
    profile: dict[str, Any],
    post_records: list[dict[str, Any]],
    avatar_src: str,
    mode_label: str,
    output_dir: Path,
    batch_size: int,
) -> None:
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
        brand_meta=escape(f"Экспорт профиля • {datetime.now().strftime('%d.%m.%Y')}"),
        stats_pill=escape(f"{len(post_records)} posts ready"),
        avatar_src=escape(avatar_src),
        username=escape(profile["username"]),
        full_name=escape(profile.get("full_name") or "Без имени"),
        post_count=escape(format_count(profile.get("mediacount"))),
        followers=escape(format_count(profile.get("followers"))),
        followees=escape(format_count(profile.get("followees"))),
        mode_label=escape(mode_label),
        bio=escape(profile.get("biography") or "Биография не указана."),
        post_data=raw_json,
    )
    index_file = output_dir / "index.html"
    index_file.write_text(html, encoding="utf-8")
    print(f"[done] HTML saved to {index_file}")


def select_post_links(all_links: list[str], args: argparse.Namespace) -> list[str]:
    if args.mode == "all":
        return all_links
    if args.limit <= 0:
        raise ExportError("--limit must be greater than 0")
    return list(reversed(all_links[-args.limit:]))


def run(args: argparse.Namespace) -> int:
    output_dir = ensure_output_dir(args.output_dir, args)
    profile_dir = Path(args.browser_profile_dir).expanduser().resolve()

    with InstagramBrowser(profile_dir=profile_dir, headful=args.headful) as browser:
        browser.ensure_logged_in()
        preflight_instagram_access(browser)
        session = browser.authenticated_session()
        public_session = public_api_session()

        profile, _initial_links, _public_cursor, _user_id = fetch_profile_info(public_session, args.username)
        print(f"[fetch] Resolved @{profile['username']} profile metadata")

        # Only fetch the first page if we have no cached links yet — otherwise
        # collect_post_links will resume from the saved cursor directly.
        cached = load_post_links(output_dir, profile["username"])
        if cached["links"]:
            initial_links: list[str] = []
            end_cursor: str | None = None  # ignored; collect_post_links uses saved cursor
        else:
            initial_links, end_cursor, _has_next = fetch_authenticated_timeline_page(session, profile["username"], None)

        all_links = collect_post_links(
            session=session,
            username=profile["username"],
            total_available=int(profile.get("mediacount") or len(initial_links)),
            output_dir=output_dir,
            args=args,
            seed_links=initial_links,
            initial_cursor=end_cursor,
        )
        print(f"[fetch] Collected {len(all_links)} post URLs")

        selected_links = select_post_links(all_links, args)
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
            if not avatar_url:
                raise ExportError(f"Could not determine avatar URL for @{profile['username']}")
            avatar_file = download_binary(session, avatar_url, output_dir / "avatar")
            avatar_name = avatar_file.name

        post_records: list[dict[str, Any]] = []
        for index, post_url in enumerate(selected_links, start=1):
            shortcode = parse_shortcode_from_url(post_url)
            existing_record = records_by_shortcode.get(shortcode)
            if existing_record:
                media_path = existing_record.get("local_media_path")
                if isinstance(media_path, str) and (output_dir / media_path).exists():
                    print(f"[resume] {index}/{len(selected_links)} {shortcode} already downloaded")
                    post_records.append(existing_record)
                    continue

            print(f"[download] {index}/{len(selected_links)} {shortcode}")
            payload = scrape_post_payload(browser, post_url)
            record = build_post_record(session, payload, output_dir / "media", index, post_url)
            post_records.append(record)
            save_export_state(output_dir, args, profile, avatar_name, post_records, completed=False)

        generate_html(
            profile=profile,
            post_records=post_records,
            avatar_src=avatar_name,
            mode_label=build_mode_label(args),
            output_dir=output_dir,
            batch_size=max(1, args.batch_size),
        )
        save_export_state(output_dir, args, profile, avatar_name, post_records, completed=True)
        print(f"[open] file://{output_dir / 'index.html'}")
        return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n[abort] Interrupted by user", file=sys.stderr)
        return 130
    except ExportError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"[error] Network request failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())