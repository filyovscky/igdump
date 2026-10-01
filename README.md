# igdump

[Русский](README.ru.md)

Export Instagram posts to an HTML archive, or collect commenters and likers
with HTML summaries and CSV statistics.

## Install

Python 3.11+ is required. Install Google Chrome for post exports.

```console
pip install -r requirements.txt
```

## Commands

Choose one command: `all`, `oldest`, `comments`, or `likers`.
Usernames work with or without `@`.

```console
# First 100 posts from the beginning of the profile, oldest first
python insta_html_export.py oldest @kharlamova_alena --limit 100

# All available posts, newest first
python insta_html_export.py all @kharlamova_alena

# Commenters / likers of the 100 most recent posts
python insta_html_export.py comments @kharlamova_alena --limit 100
python insta_html_export.py likers @kharlamova_alena --limit 100
```

`oldest` requires a positive `--limit`. For `comments` and `likers`,
omitting `--limit` or using `--limit 0` selects all available posts.
The script first collects the timeline to identify the earliest posts.
Pinned posts may affect Instagram's ordering.

## Date filters

Dates use **DD.MM.YYYY**, in the computer's local time zone. Both boundaries
include the entire specified day. Filters select posts by **publication date**,
not by the date of a comment or like. The limit applies **after** filtering.

```console
# From 1 January 2024 onward, including that day
python insta_html_export.py all @kharlamova_alena --after 01.01.2024

# Through 31 December 2024, including that day
python insta_html_export.py all @kharlamova_alena --before 31.12.2024

# First 100 posts within 2024
python insta_html_export.py oldest @kharlamova_alena --limit 100 --after 01.01.2024 --before 31.12.2024
```

The same filters work with `comments` and `likers`. Invalid dates and reversed
ranges are rejected. If a post's date cannot be determined, filtering stops
with an explanation instead of silently skipping it. Dates are cached.

## Login and results

For `all` / `oldest`, log into the Chrome window on first use and press Enter
in the terminal. The session is reused later.

For `comments` / `likers`, the script can request your `sessionid` cookie:
Chrome → F12 → Application → Cookies → instagram.com → sessionid.
It is saved locally in `~/.insta-export/config.json`.

Results are saved under `exports/`, in a folder named for the profile, command,
limit and any date filters:

| Command | Files |
|---|---|
| `all` / `oldest` | `index.html`, local media |
| `comments` | `comments.html`, `comments.csv`, `comments-stats.csv` |
| `likers` | `likers.html`, `likers.csv`, `likers-stats.csv` |

HTML opens automatically when collection finishes, including partial results.
Console progress shows the current stage and counts. Completed posts are saved
incrementally; rerun the same command to retry failures. Finished timeline
caches are reused even when the total post count is unknown.

Commenter statistics include comment count, posts commented on, likes received
on comments and the latest collected comment date. Liker statistics include
collected likes and posts liked per user. HTML shows the top users and posts;
statistics CSVs include every collected user.

Instagram may limit access or return only part of a liker list; totals describe
**collected accounts**, not a guarantee of all likes. Comment replies may not
be included. Incomplete collection is reported. A `429` means rate limiting;
wait before retrying. A missing avatar does not prevent an export.

## Options

| Option | Purpose |
|---|---|
| `--after DD.MM.YYYY` / `--before DD.MM.YYYY` | Inclusive publication-date bounds |
| `--download-videos` | Save videos for `all` / `oldest`; default: covers only |
| `--refresh` | Recheck the timeline and update selected posts / interactions |
| `--no-open` | Do not open the finished HTML automatically |
| `--output-dir "C:\Archives\instagram"` | Custom output folder |
| `--dry-run` | Select and count posts without downloading media / interactions; needs Instagram access |
| `--headful` | Keep Chrome visible during post export |
| `--comment-delay 3` / `--like-delay 3` | Request delay in seconds for the corresponding command (default: 2) |
| `--verbose` / `--quiet` | Detailed / minimal console output |
| `--help` | Command help, e.g. `python insta_html_export.py likers --help` |

Put options after the command. Downloaded videos play in the HTML archive,
including mixed image/video carousels.
