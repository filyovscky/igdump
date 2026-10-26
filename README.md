# igdump

[Русский](README.ru.md)

Export Instagram posts to an HTML archive, or collect comments and users who liked posts
with HTML summaries and CSV statistics.

## Install

Python 3.11+ is required. Install Google Chrome for post exports.

```console
pip install -r requirements.txt
```

To create an isolated environment and install the `igdump` command:

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
igdump -h
```

```bash
# Linux / macOS, from the project folder; Python 3.11+
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
igdump -h
```

Online `posts`, `full` and `highlights` use an installed Google Chrome. The first login needs
a graphical session. `--offline` does not launch a browser. Actual runs on
Linux/macOS have not yet been verified.

In Windows Command Prompt, activate with `.venv\Scripts\activate.bat`.
To use the tested dependency versions, install with
`python -m pip install -c requirements-tested.txt -e .` inside the environment.
Avoid installing into a shared Python used by other OSINT tools.

## Commands

### Gallery, text report and Highlights (0.7)

The post archive switches between **Feed** and **Photo grid**. The grid displays
every saved carousel image separately, keeping slide order within each post.
Hover/focus reveals the publication date; clicking opens a viewer with previous/next
media navigation. **Post order** switches between newest and oldest first without
changing the saved collection. View/order preferences are remembered locally.
Carousels use arrows, swipe and a frame counter instead of horizontal scrollbars.

`full` includes `summary.html` and `summary.txt`: an editable, copyable Russian report
with profile counts, up to ten leading commenters/likers, all received account
mentions/tags and received place names. Rankings use the selected post sample;
missing fields and incomplete lists are explicitly marked. Profile counts and bio
are read from the authenticated profile query as well as HTML/API fallbacks. An empty
bio and an unavailable bio are different states.

Highlights are enabled in online `full`; other post exports can enable `--highlights`.
They use additional authenticated page requests, unlike passive analytics. Cached
completed albums resume locally; `--refresh` rechecks selected albums. Public/profile
access restrictions still apply. The page stores only items actually returned by
Instagram and reports partial/failed albums. It does not click the story-view button
or send a separate mark-seen request; Instagram may still record access to its pages.

```console
igdump highlights "@username"
igdump highlights "@username" --highlight-limit 1 --download-videos
igdump full "@username" --no-highlights --no-text-report
igdump full "@username" --highlight-limit 3
igdump full "@username" --offline --output-dir "exports/username-full-0"
```

Post `--limit`, date filters and gallery sort do not limit Highlights. Use
`--highlight-limit N` for albums (0 = all received albums). By default, photos and
video covers are saved; **`--download-videos` also applies to Highlights**.
Highlights are in `highlights.html`, `highlights.csv`, `.highlights.json` and
`highlights/`. Offline mode rebuilds saved albums without Instagram requests.
An old archive without Highlights data is reported as not collected; a one-album
cache is not represented as the whole profile. Use `--no-highlights` to rebuild only
existing post/interaction reports. `--text-report` is also available on `posts`,
`comments` and `likes`; it makes no additional network requests.

### Reliability and updates (0.6)

- A completed archive checks the fresh timeline head on its next online run and
  reconnects it to the saved history. An unfinished scan resumes its saved cursor.
  Previously archived posts are retained; this is not a live mirror of deletions.
- Publication order uses full timestamps when available, including pinned posts.
  Unknown times produce a warning rather than a claim of verified chronology.
  Latest-N scans can stop early only when dates, pin status and chronological
  ordinary-post order are known; otherwise the full timeline is traversed.
- HTTP 200 containing an API failure or missing required list is an error, not
  a successful empty result. Missing location pictures remain non-fatal.
- Posts and comment pages are checkpointed in `.posts.sqlite` and `.comments.sqlite`.
  Existing JSON archives migrate automatically. Post HTML/JSON snapshots update
  approximately every 10 seconds and at normal completion; SQLite commits each post.
  Offline reports can recover records newer than the last JSON snapshot.
- Changing the selection preserves the account archive. A folder lock prevents
  simultaneous CLI writers, and corrupt JSON caches are reported without erasing them.
- Full timeline media payloads are reused before opening individual post pages.
  Missing/expired media falls back to Chrome. Media uses a separate session without
  Instagram cookies or CSRF headers. Existing public-profile fallbacks are retained.
- `igdump full "@username" --no-recheck-likes` skips the immediate extra request for
  an incomplete like list. The default remains one recheck. Coverage stays partial;
  a fresh reported count can be lower than an older cached count.
- Cached likes are archived observations collected at different times, not a live
  snapshot. Use `--refresh` to replace selected lists, including old empty results
  that may have been created by earlier versions after an API failure.
- Exit code `4` also applies to incomplete offline archives/interaction lists.
  Creating an HTML report does not establish that every selected item was collected.
- `Tracked HTTP requests` excludes requests initiated internally by Chrome.
  Debug logs include phase elapsed times. Date filters use the export computer's
  local zone; changing the analytics chart zone does not change the selected posts.

Commands choose what to collect: `posts`, `comments`, `likes`, or `full`.
Options choose which posts to use. Profiles can be specified as `@username`,
`username`, or `https://www.instagram.com/username/` (including tracking parameters).
Install the `igdump` command with `pip install -e .`; alternatively, use
`python insta_html_export.py` as shown below.

```console
# A profile URL instead of a username works with every command
python insta_html_export.py full "https://www.instagram.com/kharlamova_alena/"

# First 100 posts from the beginning of the profile, oldest first
python insta_html_export.py posts "@kharlamova_alena" --oldest --limit 100

# Latest 100 posts
python insta_html_export.py posts "@kharlamova_alena" --limit 100

# All available posts, newest first
python insta_html_export.py posts "@kharlamova_alena"

# Comments / users who liked the 100 most recent posts
python insta_html_export.py comments "@kharlamova_alena" --limit 100
python insta_html_export.py likes "@kharlamova_alena" --limit 100

# Users who liked the earliest 100 posts
python insta_html_export.py likes "@kharlamova_alena" --oldest --limit 100

# Posts and both statistics for 2024
python insta_html_export.py full "@kharlamova_alena" --after 01.01.2024 --before 31.12.2024
```

For every command, omitting `--limit` or using `--limit 0` selects all posts.
`--limit N` selects the latest N; `--oldest --limit N` selects the earliest N.
`--oldest` without a limit selects all posts, oldest first. Dates apply before the limit.
The earliest selection requires a complete timeline; if collection is incomplete,
the script saves progress and stops so that a later run can resume.
Legacy commands remain supported: `all` means `posts`, `oldest --limit N` means
`posts --oldest --limit N` (a positive limit is still required), and `likers` means `likes`.
Matching old cache folders are reused automatically.
Pinned posts may affect Instagram's ordering.

## Date filters

Dates use **DD.MM.YYYY**, in the computer's local time zone. Both boundaries
include the entire specified day. Filters select posts by **publication date**,
not by the date of a comment or like. The limit applies **after** filtering.

```console
# From 1 January 2024 onward, including that day
python insta_html_export.py posts "@kharlamova_alena" --after 01.01.2024

# Through 31 December 2024, including that day
python insta_html_export.py posts "@kharlamova_alena" --before 31.12.2024

# First 100 posts within 2024
python insta_html_export.py posts "@kharlamova_alena" --oldest --limit 100 --after 01.01.2024 --before 31.12.2024
```

The same filters work with `comments`, `likes`, and `full`. Invalid dates and reversed
ranges are rejected. If a post's date cannot be determined, filtering stops
with an explanation instead of silently skipping it. Dates are cached.

## Login and results

For `posts` / `full`, log into the Chrome window on first use and press Enter
in the terminal. The session is reused later.

For `comments` / `likes`, the script can request your `sessionid` cookie:
Chrome → F12 → Application → Cookies → instagram.com → sessionid.
It is saved locally in `~/.insta-export/config.json`.

Results are saved under `exports/`, in a folder named for the profile, command,
limit, `--oldest` selection and any date filters:

| Command | Files |
|---|---|
| `posts` | `index.html`, local media |
| `comments` | `comments.html`, `comments.csv`, `comments-stats.csv` |
| `likes` | `likes.html`, `likes.csv`, `likes-stats.csv`, `likes-coverage.csv` |
| `full` | All the above, four passive reports below, with `full.html` linking to the reports |

HTML opens automatically when collection finishes, including partial results.
Console progress shows the current stage and counts. Completed posts are saved
incrementally; rerun the same command to retry failures. Finished timeline
caches are reused even when the total post count is unknown.

Commenter statistics include comment count, posts commented on, likes received
on comments and the latest collected comment date. Like participation statistics include
collected likes per user. HTML shows the top users and posts;
statistics CSVs include every collected user. Top-10 posts use saved Instagram
like counters when available; old caches without counters use collected users
and mark those figures explicitly.

Instagram may limit access or return only part of a list of users who liked a post; totals describe
**collected accounts**, not a guarantee of all likes. Comment replies may not
be included. Incomplete collection is reported. A `429` means rate limiting;
wait before retrying. A missing avatar does not prevent an export.

## Incomplete lists and resource use

`likes` downloads no publications, photos, videos or avatars. It collects
post links, then requests pages of users who liked posts. Completed lists are cached. Network
errors get at most two extra attempts; saved pages resume from their cursor.
A `429` stops collection instead of retrying every remaining post.
A 429 from the profile API first falls back to public profile HTML, using
the original separate session and desktop headers. If that fallback fails, or
timeline collection is rate-limited, `full` stops the remaining network phases,
preserves cached progress, and saves a diagnostic summary. If the post timeline fails, comments and likes are skipped rather than
fetching the same timeline again. HTTP 400 is not retried unchanged; GraphQL can
fall back to REST when the numeric profile ID is available. Wait before rerunning
after a 429; restarting immediately does not remove Instagram's rate limit.
During a post scan, an `SSL EOF` on profile lookup triggers one retry through
the existing Playwright context's HTTP client with the same login. Certificate
verification stays enabled; a profile API 429 on either transport uses the
same HTML fallback.
If public HTML redirects to login or lacks the profile ID, post scans read the
profile through the existing logged-in Chrome. Connection resets, SSL EOF and
timeouts during profile lookup also use this Chrome fallback. Timeline responses
already received by Chrome are reused; pagination uses the observed GraphQL request
and Chrome session instead of guessing a replacement document ID. Missing
location pictures do not invalidate an otherwise usable timeline.
If the next-page query has not been observed yet, Chrome scrolls the profile
to obtain the pagination request generated by Instagram. Switching to REST
starts at its first page and deduplicates saved links; GraphQL and REST cursors
are kept separate, including when resuming from cache. The first received page
is saved before requesting the next one.
Profile fallback also reads the target account's avatar and available profile
fields from embedded page data, metadata or the displayed profile picture.
A saved placeholder is retried when a later online run obtains an avatar URL.

A counter of 43 with 42 returned users is a partial list, not a lost CSV row.
The API may return fewer accessible accounts than its counter; rechecking
can find missing users but cannot guarantee all 43. Inspect `likes-coverage.csv`.
Incomplete lists are automatically rechecked once, without a flag. Cached
partial lists are rechecked on the next online run. If users remain missing,
the console, HTML report and coverage CSV explain the observed reason: no next
page, no additional users, an exhausted network retry, or denied/rate-limited
access. The API may not disclose why its counter and list differ. Run normally:

```console
python insta_html_export.py likes "@kharlamova_alena"
python insta_html_export.py likes "@kharlamova_alena" --offline
```

Use the same limit, dates and output folder as the original run. `--offline`
rebuilds HTML/CSV from the cache with **zero HTTP requests** and no login.
Every command supports it. `--refresh` recollects selected lists.

Users who liked posts are saved per page in SQLite; old JSON caches migrate automatically
and remain as backups. SQL counts the likes; CSV rows are streamed. Memory
scales with a page and the number of unique users, rather than all likes.
The first JSON migration temporarily loads the old file into memory. Existing
cache folders and the previous command spelling remain compatible.

Request cost: profile/login checks + timeline pages + pages of users who liked posts for selected
uncached or incomplete posts, plus one recheck for newly incomplete lists.
Date filters may need a metadata request per uncached date.
`full` reuses the Chrome session and one selected timeline across its three phases.
In an interactive terminal, logs appear above a fixed bottom progress panel;
redirected output uses plain, occasional progress lines.

## Passive reports

`full` enables four additional reports by default. For other commands use
`--analytics`, `--mentions`, `--contacts`, and `--locations` independently.
Disable them in `full` with `--no-analytics`, `--no-mentions`, `--no-contacts`,
or `--no-locations`.

| Report | Contents |
|---|---|
| `analytics.html` | Posting frequency over the observed span, monthly/weekday/hour charts, and a weekday × hour heatmap |
| `mentions.html` | Caption mentions and media tags, with evidence type and source post |
| `contacts.html` | Phones, emails and links in captions, normalized values, original captions and source posts |
| `locations.html` | Received place names, IDs and coordinates, an offline coordinate map and source posts |

Each report has CSV and JSON exports. The saved analytics files use the export
computer's local time zone. The HTML shows an explicit time zone selector,
initially set to the browser's local IANA zone (for example `Europe/Moscow`).
Changing it recalculates dates, months, weekdays, hours, heatmaps and frequency
locally, including daylight saving at each publication date. Buttons download
CSV and JSON for the selected zone; existing export files are not overwritten.
Analytics describes publication times, not audience activity. Frequency includes
zero-post days between the first and last dated publications in the selection.
The same date filters and post limit apply to every report.
In the contacts table, type, normalized value, matched text and source columns
have fixed widths. Original captions use the remaining space and wrap; long
captions scroll inside their cell without shrinking the other columns.

Measured collection time and incomplete like-list findings are documented in
[collection diagnostics](docs/collection-findings.md).

All fields come from normal scan responses or existing caches. These flags add
**no Instagram requests**: they do not look up mentioned profiles, visit links,
or request geocoding. Locations include an offline geographic map with zoom/pan
and source markers (Natural Earth outlines, about 124 KB). Pins show publication
dates in the export computer's local time zone; click for exact times with UTC
offsets and source posts. Identical coordinates share a pin with a date range.
The **Show pin dates** checkbox toggles labels on both maps. **Load street map
(Internet)** optionally enables OpenStreetMap tiles; no external map requests
occur before clicking it. Leaflet is bundled locally (about 162 KB). Reports
show coverage instead of inventing missing timestamps,
tags or locations. Older caches may lack these fields; recovered local date
labels are identified separately. Text matches do not verify contact ownership
or real-world identity.

```console
igdump posts "@username" --analytics --mentions --contacts --locations

# Rebuild only from an existing folder; no network or media downloads
igdump posts "@username" --offline --analytics --mentions --contacts --locations --output-dir "exports/username-posts-0"

# All reports from an existing full export
igdump full "@username" --offline --output-dir "exports/username-full-0"
```

All CSV exports protect against spreadsheet formulas by prefixing potentially
executable text with an apostrophe. Original cache and JSON values are unchanged.

## Options

| Option | Purpose |
|---|---|
| `--limit N` | Latest N posts after date filtering; omitted or 0 = all |
| `--oldest` | Earliest posts, oldest first; works with every command |
| `--after DD.MM.YYYY` / `--before DD.MM.YYYY` | Inclusive publication-date bounds |
| `--download-videos` | Save videos for `posts` / `full`; default: covers only |
| `--refresh` | Recheck the timeline and update selected posts / interactions |
| `--offline` | Rebuild reports without network requests or login; supported by every command |
| `--analytics` / `--mentions` / `--contacts` / `--locations` | Passive reports; enabled by default in `full`, disabled with `--no-…` |
| `--no-open` | Do not open the finished HTML automatically |
| `--output-dir "C:\Archives\instagram"` | Custom output folder |
| `--dry-run` | Select and count posts without downloading media / interactions; needs Instagram access |
| `--headful` | Keep Chrome visible during post export (`posts` / `full`) |
| `--batch-size N` | Cards per scroll batch in the HTML archive (`posts` / `full`, default: 9); does not change API requests |
| `--browser-profile-dir PATH` | Saved Chrome profile for login |
| `--sessionid COOKIE` | Session cookie for `comments` / `likes`; saved sessions are reused |
| `--comment-delay 3` / `--like-delay 3` | Request delay in seconds for the corresponding command (default: 2) |
| `--verbose` / `--quiet` | Detailed / minimal console output |
| `-h` / `--help` | General help: `igdump -h`; command options: `igdump full -h` |

Put options after the command. Downloaded videos play in the HTML archive,
including mixed image/video carousels.
