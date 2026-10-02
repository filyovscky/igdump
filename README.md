# igdump

[Русский](README.ru.md)

Export Instagram posts to an HTML archive, or collect comments and users who liked posts
with HTML summaries and CSV statistics.

## Install

Python 3.11+ is required. Install Google Chrome for post exports.

```console
pip install -r requirements.txt
```

## Commands

Choose one command: `all`, `oldest`, `comments`, `likes`, or `full`.
Usernames work with or without `@`.

```console
# First 100 posts from the beginning of the profile, oldest first
python insta_html_export.py oldest "@kharlamova_alena" --limit 100

# All available posts, newest first
python insta_html_export.py all "@kharlamova_alena"

# Comments / users who liked the 100 most recent posts
python insta_html_export.py comments "@kharlamova_alena" --limit 100
python insta_html_export.py likes "@kharlamova_alena" --limit 100

# Posts, comment statistics and like statistics together
python insta_html_export.py full "@kharlamova_alena" --limit 100
```

`oldest` requires a positive `--limit`. For `comments`, `likes`, and `full`,
omitting `--limit` or using `--limit 0` selects all available posts.
The script first collects the timeline to identify the earliest posts.
Pinned posts may affect Instagram's ordering.

## Date filters

Dates use **DD.MM.YYYY**, in the computer's local time zone. Both boundaries
include the entire specified day. Filters select posts by **publication date**,
not by the date of a comment or like. The limit applies **after** filtering.

```console
# From 1 January 2024 onward, including that day
python insta_html_export.py all "@kharlamova_alena" --after 01.01.2024

# Through 31 December 2024, including that day
python insta_html_export.py all "@kharlamova_alena" --before 31.12.2024

# First 100 posts within 2024
python insta_html_export.py oldest "@kharlamova_alena" --limit 100 --after 01.01.2024 --before 31.12.2024
```

The same filters work with `comments`, `likes`, and `full`. Invalid dates and reversed
ranges are rejected. If a post's date cannot be determined, filtering stops
with an explanation instead of silently skipping it. Dates are cached.

## Login and results

For `all` / `oldest` / `full`, log into the Chrome window on first use and press Enter
in the terminal. The session is reused later.

For `comments` / `likes`, the script can request your `sessionid` cookie:
Chrome → F12 → Application → Cookies → instagram.com → sessionid.
It is saved locally in `~/.insta-export/config.json`.

Results are saved under `exports/`, in a folder named for the profile, command,
limit and any date filters:

| Command | Files |
|---|---|
| `all` / `oldest` | `index.html`, local media |
| `comments` | `comments.html`, `comments.csv`, `comments-stats.csv` |
| `likes` | `likes.html`, `likes.csv`, `likes-stats.csv`, `likes-coverage.csv` |
| `full` | All the above, with `full.html` linking to the reports |

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
`comments` also supports it. `--refresh` recollects selected lists.

Users who liked posts are saved per page in SQLite; old JSON caches migrate automatically
and remain as backups. SQL counts the likes; CSV rows are streamed. Memory
scales with a page and the number of unique users, rather than all likes.
The first JSON migration temporarily loads the old file into memory. Existing
cache folders and the previous command spelling remain compatible.

Request cost: profile/login checks + timeline pages + pages of users who liked posts for selected
uncached or incomplete posts, plus one recheck for newly incomplete lists.
Date filters may need a metadata request per uncached date.
`full` reuses one session and one selected timeline across its three phases.
In an interactive terminal, logs appear above a fixed bottom progress panel;
redirected output uses plain, occasional progress lines.

## Options

| Option | Purpose |
|---|---|
| `--after DD.MM.YYYY` / `--before DD.MM.YYYY` | Inclusive publication-date bounds |
| `--download-videos` | Save videos for `all` / `oldest` / `full`; default: covers only |
| `--refresh` | Recheck the timeline and update selected posts / interactions |
| `--offline` | Rebuild interaction reports without network requests (`comments` / `likes`) |
| `--no-open` | Do not open the finished HTML automatically |
| `--output-dir "C:\Archives\instagram"` | Custom output folder |
| `--dry-run` | Select and count posts without downloading media / interactions; needs Instagram access |
| `--headful` | Keep Chrome visible during post export |
| `--comment-delay 3` / `--like-delay 3` | Request delay in seconds for the corresponding command (default: 2) |
| `--verbose` / `--quiet` | Detailed / minimal console output |
| `--help` | Command help, e.g. `python insta_html_export.py likes --help` |

Put options after the command. Downloaded videos play in the HTML archive,
including mixed image/video carousels.
