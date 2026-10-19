# Changelog

## 0.6.0 — 2026-10-19

- Capture both `/api/graphql` and `/graphql/query` browser timeline responses,
  retain their actual endpoint for replay, and wait for delayed initial queries.

- Validate API response semantics and required list structures before accepting
  empty results; preserve non-fatal missing location-picture handling.
- Reconnect completed timeline caches to a freshly fetched head; order known
  publications by timestamp and conservatively stop eligible latest-N scans early.
- Persist posts and comment pages in SQLite, resume interrupted comment cursors,
  retain account archives across selections and recover posts beyond JSON checkpoints.
- Reuse complete timeline media payloads, refresh expired URLs through Chrome,
  and batch post HTML/JSON snapshots while committing each record immediately.
- Use the authenticated Chrome profile fallback for standalone interaction scans.
- Separate media credentials, enforce HTTPS redirects, protect config-file permissions,
  hide session-cookie input, lock export folders and atomically replace reports.
- Report corrupt caches and partial offline exports; use fresh decreasing like counts;
  add optional `--no-recheck-likes` and explain archived liker observations.
- Safely encode JSON embedded in HTML and aggregate comments by stable user ID.
- Add audit regressions, browser CI and macOS to the test matrix; require Requests
  2.33+ and record a tested dependency set for isolated installations.
