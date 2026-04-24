By https://t.me/cpb_msk OSINT communtiy

# igdump

Exports an Instagram account's posts into a self-contained HTML feed with all media saved locally.

## Features

- Dumps all posts from an account or only the N earliest ones. No need to scroll manually! 
- Downloads photos and video thumbnails into a `media/` folder next to the HTML
- Resumes interrupted runs from where they left off — cursor and collected links are persisted
- Generates a single `index.html` with lazy-loaded post cards

## Requirements

```
python >= 3.11
playwright (chromium)
requests
```

```bash
pip install playwright requests
playwright install chromium
```

Google Chrome must also be installed — it is used to hold the Instagram session.

## Authentication

On the first run a Chrome window will open. Log into Instagram manually, then press Enter in the terminal. The session is saved to `~/.insta-export/chrome-profile` and reused on subsequent runs.

## Usage

### All posts

```bash
python igdump.py all <username>
```

### N earliest posts

```bash
python igdump.py oldest <username> --limit 50
```

### Options

| Option | Default | Description |
|---|---|---|
| `--output-dir` | `./exports/<username>-<mode>` | Where to save the export |
| `--batch-size` | `9` | Cards to render per scroll batch |
| `--browser-profile-dir` | `~/.insta-export/chrome-profile` | Path to the Chrome profile with the session |
| `--headful` | off | Keep the browser window visible throughout |

### Examples

```bash
# All posts, custom output directory
python igdump.py all natgeo --output-dir ~/Desktop/natgeo-export

# 100 earliest posts with the browser visible
python igdump.py oldest someuser --limit 100 --headful
```

## Export layout

```
exports/
└── username-all/
    ├── index.html          # open directly in any browser
    ├── avatar.<ext>
    ├── media/
    │   ├── 0001-<shortcode>.<ext>
    │   └── ...
    ├── .post-links.json    # cached links and pagination cursor
    └── .export-state.json
```

## Resuming an interrupted run

Re-run the same command — the script picks up the saved pagination cursor and skips already downloaded media.

> To start fresh, delete `.post-links.json` and `.export-state.json` from the export folder.

## Limitations

- Only works with **public** accounts or accounts the logged-in user follows
- Instagram may temporarily rate-limit the session — the script will report this and exit; just wait a few minutes and retry
- Videos are saved as thumbnail images, not the actual video file
