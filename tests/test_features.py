import contextlib
import csv
import io
import tempfile
import unittest
from datetime import date, datetime, UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import insta_html_export as app
from igdump_dates import date_matches, post_date
from igdump_progress import format_progress


class DateTests(unittest.TestCase):
    def test_strict_dates_and_invalid_ranges(self):
        for flags in (["--after", "1.01.2024"], ["--before", "31.02.2024"],
                      ["--after", "2024-01-01"], ["--after", "02.01.2024", "--before", "01.01.2024"]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(["all", "natgeo", *flags])
        self.assertEqual(app.parse_args(["all", "natgeo", "--after", "29.02.2024"]).after, date(2024, 2, 29))

    def test_open_bounds_and_inclusive_same_day(self):
        day = date(2024, 6, 1)
        self.assertTrue(date_matches(day, day, None))
        self.assertTrue(date_matches(day, None, day))
        self.assertTrue(date_matches(day, day, day))
        self.assertFalse(date_matches(date(2024, 5, 31), day, None))
        self.assertFalse(date_matches(date(2024, 6, 2), None, day))
        stamp = datetime(2024, 6, 1, 12, tzinfo=UTC).timestamp()
        self.assertEqual(post_date({"taken_at": stamp}), datetime.fromtimestamp(stamp, UTC).astimezone().date())

    def test_dates_apply_before_oldest_limit_and_cache_is_reused(self):
        args = app.parse_args(["oldest", "natgeo", "--limit", "1", "--after", "01.01.2024", "--before", "31.12.2024"])
        links = [f"https://www.instagram.com/p/{code}/" for code in "ABCD"]
        session = requests.Session()
        session._igdump_post_dates = {"A": "2025-01-01", "B": "2024-12-31", "C": "2024-01-01", "D": "2023-12-31"}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "fetch_post_date") as fetch:
            filtered = app.filter_links_by_date(links, args, session, Path(directory))
            self.assertEqual(app.select_post_links(filtered, args), [links[2]])
            del session._igdump_post_dates
            self.assertEqual(app.filter_links_by_date(links, args, session, Path(directory)), filtered)
            fetch.assert_not_called()

    def test_date_cache_survives_failure_and_resume(self):
        args = app.parse_args(["all", "natgeo", "--after", "01.01.2024"])
        links = [f"https://www.instagram.com/p/{code}/" for code in "AB"]
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "_safe_sleep"):
            root = Path(directory)
            with patch.object(app, "fetch_post_date", side_effect=["2024-01-01", app.ExportError("429")]):
                with self.assertRaises(app.ExportError):
                    app.filter_links_by_date(links, args, requests.Session(), root)
            self.assertEqual(app.read_json(root / ".post-dates.json")["dates"], {"A": "2024-01-01"})
            with patch.object(app, "fetch_post_date", return_value="2024-02-01") as fetch:
                self.assertEqual(app.filter_links_by_date(links, args, requests.Session(), root), list(reversed(links)))
                fetch.assert_called_once()

    def test_date_filter_separates_output_folders_and_export_states(self):
        plain = app.parse_args(["oldest", "natgeo", "--limit", "100"])
        dated = app.parse_args(["oldest", "natgeo", "--limit", "100", "--after", "01.01.2024"])
        self.assertNotEqual(app.build_job_slug(plain), app.build_job_slug(dated))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app.save_export_state(root, plain, {}, None, [], True)
            self.assertEqual(app.load_export_state(root, dated), {})


class LikerTests(unittest.TestCase):
    def test_likers_paginate_and_deduplicate(self):
        alice = {"pk": "1", "username": "alice"}
        bob = {"pk": "2", "username": "bob"}
        first = MagicMock(status_code=200)
        first.json.return_value = {"users": [alice], "next_max_id": "next", "has_more": True}
        last = MagicMock(status_code=200)
        last.json.return_value = {"users": [alice, bob], "has_more": False}
        session = MagicMock()
        session.get.side_effect = [first, last]
        with patch.object(app, "_safe_sleep"):
            self.assertEqual(app.fetch_all_likers(session, "A", 0), [alice, bob])
        self.assertEqual(session.get.call_args.kwargs["params"], {"max_id": "next"})

    def test_repeated_liker_cursor_is_not_accepted(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"users": [], "next_max_id": "same", "has_more": True}
        session = MagicMock()
        session.get.return_value = response
        with patch.object(app, "_safe_sleep"), self.assertRaises(app.ExportError):
            app.fetch_all_likers(session, "A", 0)

    def test_partial_liker_list_is_marked(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"users": [{"pk": "1", "username": "alice"}], "user_count": 100}
        session = MagicMock()
        session.get.return_value = response
        with patch.object(app, "_safe_sleep"):
            self.assertEqual(len(app.fetch_all_likers(session, "A", 0)), 1)
        self.assertIn("A", session._igdump_incomplete_likers)

    def test_liker_export_generates_html_rows_and_user_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["likers", "natgeo", "--limit", "2", "--sessionid", "test", "--output-dir", directory])
            links = [f"https://www.instagram.com/p/{code}/" for code in "AB"]
            alice = {"pk": "1", "username": "alice", "full_name": "Alice"}
            bob = {"pk": "2", "username": "bob", "full_name": "Bob"}
            with (
                patch.object(app, "_session_from_sessionid", return_value=requests.Session()),
                patch.object(app, "_save_config"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 2}, [], None, "1")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=(links, None, False)),
                patch.object(app, "fetch_all_likers", side_effect=[[alice], [alice, bob]]) as fetch,
                patch.object(app.webbrowser, "open", return_value=True) as opener,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run_comments(args), 0)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(app.run_comments(args), 0)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(opener.call_count, 2)
            root = Path(directory)
            self.assertIn("Top likers", (root / "likers.html").read_text(encoding="utf-8"))
            with (root / "likers-stats.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["username"], "alice")
            self.assertEqual(rows[0]["likes_count"], "2")
            self.assertEqual(rows[0]["posts_liked"], "2")
            self.assertEqual(len(rows), 2)
            self.assertFalse((root / "comments.html").exists())


class ProgressTests(unittest.TestCase):
    def test_bar_and_unknown_count(self):
        self.assertIn("[############------------] 50/100 (50%)", format_progress("Posts", 50, 100))
        unknown = format_progress("Links", 398)
        self.assertIn("398", unknown)
        self.assertNotIn("%", unknown)


if __name__ == "__main__":
    unittest.main()
