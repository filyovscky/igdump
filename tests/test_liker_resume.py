import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import insta_html_export as app
from igdump_storage import LikerStore, save_comment_cache


def response(users, **values):
    result = MagicMock(status_code=200)
    result.json.return_value = {"users": users, **values}
    return result


class ResumeTests(unittest.TestCase):
    def test_cli_help_is_english_and_uses_likes(self):
        for command in ([], ["likes"], ["full"], ["oldest"], ["comments"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
                app.parse_args([*command, "--help"])
            self.assertEqual(exit.exception.code, 0)
            self.assertNotRegex(output.getvalue(), r"[\u0400-\u04ff]")
            self.assertNotIn("likers", output.getvalue())
            self.assertNotIn("--retry-missing", output.getvalue())
        self.assertEqual(app.parse_args(["likers", "natgeo"]).mode, "likes")

    def test_likes_reuses_legacy_folder_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["likes", "natgeo"])
            legacy = Path(directory) / "exports" / "natgeo-likers-0"
            legacy.mkdir(parents=True)
            with patch.object(Path, "cwd", return_value=Path(directory)):
                self.assertEqual(app.ensure_output_dir(None, args), legacy.resolve())
            self.assertFalse((Path(directory) / "exports" / "natgeo-likes-0").exists())

    def test_ranking_uses_timeline_counts_instead_of_capped_user_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["likes", "natgeo", "--no-open"])
            links = [f"https://www.instagram.com/p/{code}/" for code in "AB"]
            app.save_post_links(root, args, links, 2, True)
            session = requests.Session()
            app.remember_post_dates(session, [{"code": "A", "like_count": 500}, {"code": "B", "edge_media_preview_like": {"count": 200}}])
            with patch.object(app, "fetch_all_likers", return_value=[{"pk": 1, "username": "alice"}]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.export_likers(args, session, root, "natgeo", links), 4)
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            db.select(set("AB"))
            top = db.stats()["top_posts"]
            self.assertEqual([(p["shortcode"], p["count"], p["collected"]) for p in top], [("A", 500, 1), ("B", 200, 1)])
            db.close()

    def test_connection_retries_are_bounded(self):
        session = requests.Session()
        user = {"pk": 1, "username": "alice"}
        with patch.object(session, "get", side_effect=[requests.ConnectionError("closed"), response([user])]) as get, patch.object(app, "_safe_sleep"):
            self.assertEqual(app.fetch_all_likers(session, "A", 0), [user])
            self.assertEqual(get.call_count, 2)
        with patch.object(session, "get", side_effect=requests.ConnectionError("closed")) as get, patch.object(app, "_safe_sleep"), self.assertRaises(requests.ConnectionError):
            app.fetch_all_likers(session, "A", 0)
        self.assertEqual(get.call_count, 3)

    def test_rate_limit_does_not_retry(self):
        session = requests.Session()
        with patch.object(session, "get", return_value=MagicMock(status_code=429)) as get, patch.object(app, "_safe_sleep"), self.assertRaises(app.ExportError):
            app.fetch_all_likers(session, "A", 0)
        get.assert_called_once()

    def test_page_survives_failure_and_cursor_resumes_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["likes", "natgeo", "--output-dir", directory, "--no-open"])
            links = ["https://www.instagram.com/p/A/"]
            app.save_post_links(root, args, links, 1, True)
            session = requests.Session()
            first = response([{"pk": 1, "username": "alice"}], next_max_id="next", has_more=True, user_count=2)
            with patch.object(session, "get", side_effect=[first, requests.ConnectionError("closed"), requests.ConnectionError("closed"), requests.ConnectionError("closed")]), patch.object(app, "_safe_sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.export_likers(args, session, root, "natgeo", links), 4)
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            self.assertEqual(db.count("A"), 1)
            self.assertEqual(db.state("A")["cursor"], "next")
            db.close()
            last = response([{"pk": 1, "username": "alice"}, {"pk": 2, "username": "bob"}], user_count=2, has_more=False)
            with patch.object(session, "get", return_value=last) as get, patch.object(app, "_safe_sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.export_likers(args, session, root, "natgeo", links), 0)
            self.assertEqual(get.call_args.kwargs["params"], {"max_id": "next"})
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            self.assertEqual(db.count("A"), 2)
            db.close()
            self.assertFalse((root / "media").exists())

    def test_cached_partial_is_retried_automatically_and_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["likes", "natgeo", "--output-dir", directory, "--no-open"])
            links = ["https://www.instagram.com/p/A/"]
            app.save_post_links(root, args, links, 1, True)
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            db.add_page("A", [{"pk": 1, "username": "alice"}], 2, None)
            db.mark("A", "partial")
            db.close()
            with patch.object(app, "fetch_all_likers", return_value=[{"pk": 1, "username": "alice"}, {"pk": 2, "username": "bob"}]) as fetch, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.export_likers(args, requests.Session(), root, "natgeo", links), 0)
                fetch.assert_called_once()

    def test_new_partial_is_rechecked_once_and_records_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["likes", "natgeo", "--no-open"])
            links = ["https://www.instagram.com/p/A/"]
            app.save_post_links(root, args, links, 1, True)
            session = requests.Session()
            incomplete = response([{"pk": 1, "username": "alice"}], user_count=2)
            with patch.object(session, "get", return_value=incomplete) as get, patch.object(app, "_safe_sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.export_likers(args, session, root, "natgeo", links), 4)
            self.assertEqual(get.call_count, 2)
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            reason = db.state("A")["error"]
            self.assertIn("no additional users", reason)
            self.assertIn("without a next-page cursor", reason)
            self.assertEqual(db.count("A"), 1)
            db.close()
            self.assertIn(reason, (root / "likes-coverage.csv").read_text(encoding="utf-8-sig"))
            self.assertIn("no additional users", (root / "likes.html").read_text(encoding="utf-8"))

    def test_automatic_recheck_can_complete_a_new_partial_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["likes", "natgeo", "--no-open"])
            links = ["https://www.instagram.com/p/A/"]
            app.save_post_links(root, args, links, 1, True)
            session = requests.Session()
            first = response([{"pk": 1, "username": "alice"}], user_count=2)
            last = response([{"pk": 1, "username": "alice"}, {"pk": 2, "username": "bob"}], user_count=2)
            with patch.object(session, "get", side_effect=[first, last]) as get, patch.object(app, "_safe_sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.export_likers(args, session, root, "natgeo", links), 0)
            self.assertEqual(get.call_count, 2)
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            self.assertEqual(db.state("A")["state"], "complete")
            self.assertEqual(db.count("A"), 2)
            db.close()

    def test_offline_migration_and_report_do_not_access_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["likes", "natgeo", "--output-dir", directory, "--offline", "--no-open"])
            links = [f"https://www.instagram.com/p/{code}/" for code in "ABCDEFGHIJKL"]
            app.save_post_links(root, args, links, 12, True)
            records = [{"post_shortcode": code, "user_id": "1", "username": "alice", "full_name": "<script>"} for code in "ABCDEFGHIJKL"]
            save_comment_cache(root / ".likers.json", "natgeo", records, set("ABCDEFGHIJKL"))
            with patch.object(requests.Session, "request", side_effect=AssertionError("network")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_comments(args), 0)
                self.assertEqual(app.run_comments(args), 0)
            html = (root / "likes.html").read_text(encoding="utf-8")
            self.assertIn("Top-10 most liked posts", html)
            self.assertNotIn("<th>Posts</th>", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertEqual(html.count('href="https://www.instagram.com/p/'), 10)
            db = LikerStore(root / ".likers.sqlite", "natgeo")
            self.assertEqual(db.count("A"), 1)
            db.close()

    def test_refresh_interruption_keeps_previous_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite"
            db = LikerStore(path, "natgeo")
            db.add_page("A", [{"pk": 1, "username": "alice"}], 1, None)
            db.mark("A", "complete")
            db.reset("A")
            db.add_page("A", [{"pk": 2, "username": "bob"}], 2, "cursor")
            db.close()
            db = LikerStore(path, "natgeo")
            self.assertEqual(db.count("A"), 2)
            self.assertEqual(db.state("A")["state"], "failed")
            db.close()

    def test_full_runs_all_phases_and_opens_only_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["full", "natgeo", "--limit", "100", "--output-dir", directory])
            modes = []
            def phase(arg):
                modes.append((arg.mode, arg.limit, arg.no_open, arg.output_dir))
                return 0
            with patch.object(app, "run", side_effect=phase), patch.object(app, "run_comments", side_effect=phase), patch.object(app.webbrowser, "open", return_value=True) as opener, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_full(args), 0)
            self.assertEqual([item[0] for item in modes], ["full", "comments", "likes"])
            self.assertTrue(all(item[1:3] == (100, True) for item in modes))
            opener.assert_called_once_with((Path(directory).resolve() / "full.html").as_uri())

    def test_full_reuses_selected_links_and_session_without_profile_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(["full", "natgeo", "--limit", "1", "--output-dir", directory, "--no-open"])
            def posts(phase):
                phase._shared_session = requests.Session()
                phase._selected_links = ["https://www.instagram.com/p/A/"]
                app.save_post_links(root, phase, phase._selected_links, 1, True)
                (root / "index.html").write_text("posts", encoding="utf-8")
                return 0
            with patch.object(app, "run", side_effect=posts), patch.object(app, "fetch_profile_info", side_effect=AssertionError("extra profile request")), patch.object(app, "fetch_all_comments", return_value=[]), patch.object(app, "fetch_all_likers", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_full(args), 0)
            self.assertTrue(all((root / name).exists() for name in ["index.html", "comments.html", "likes.html", "full.html"]))

    def test_full_does_not_start_next_phase_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["full", "natgeo", "--output-dir", directory, "--no-open"])
            def limited(phase):
                phase._rate_limited = True
                return 4
            with patch.object(app, "run", return_value=0), patch.object(app, "run_comments", side_effect=limited) as interactions, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_full(args), 4)
            interactions.assert_called_once()

    def test_help_survives_windows_legacy_encoding(self):
        script = Path(app.__file__).resolve()
        for command in ([], ["likes"], ["full"]):
            result = subprocess.run([sys.executable, str(script), *command, "--help"], env={**os.environ, "PYTHONIOENCODING": "cp1252:strict"}, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
