import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import insta_html_export as app


class CommandTests(unittest.TestCase):
    def test_first_100_posts_and_at_username(self):
        args = app.parse_args(["oldest", "@kharlamova_alena", "--limit", "100"])
        self.assertEqual(args.username, "kharlamova_alena")
        links = [str(i) for i in range(150, 0, -1)]
        self.assertEqual(app.select_post_links(links, args), [str(i) for i in range(1, 101)])

    def test_comments_limit_selects_recent_posts(self):
        args = app.parse_args(["comments", "natgeo", "--limit", "2"])
        self.assertEqual(app.select_post_links(["new", "middle", "old"], args), ["new", "middle"])
        args.limit = 0
        self.assertEqual(app.select_post_links(["new", "old"], args), ["new", "old"])

    def test_invalid_arguments_fail_before_export(self):
        cases = [
            ["all", "oldest", "100", "@kharlamova_alena"],
            ["oldest", "natgeo", "--limit", "0"],
            ["oldest", "natgeo", "--limit", "-1"],
            ["oldest", "natgeo"],
            ["comments", "natgeo", "--limit", "-1"],
            ["all", "natgeo", "--batch-size", "0"],
            ["all", "https://instagram.com/natgeo"],
            ["all", "natgeo", "--quiet", "--verbose"],
            ["comments", "natgeo", "--comment-delay", "nan"],
            ["comments", "natgeo", "--comment-delay", "-1"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    app.parse_args(argv)
                self.assertEqual(error.exception.code, 2)

    def test_help_has_copyable_example(self):
        for argv in (["--help"], ["oldest", "--help"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                app.parse_args(argv)
            self.assertIn("oldest @kharlamova_alena --limit 100", out.getvalue())


class ExportTests(unittest.TestCase):
    def setUp(self):
        opener = patch.object(app.webbrowser, "open", return_value=True)
        self.browser_open = opener.start()
        self.addCleanup(opener.stop)

    def test_completed_timeline_with_unknown_total_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100"])
            root = Path(directory)
            app.save_post_links(root, args, ["new", "old"], 0, True)
            with patch.object(app, "fetch_authenticated_timeline_page") as fetch:
                links = app.collect_post_links(MagicMock(), "natgeo", 0, root, args, [], None, "1")
            fetch.assert_not_called()
            self.assertEqual(links, ["new", "old"])

    def test_duplicate_pages_do_not_stop_valid_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100"])
            root = Path(directory)
            app.save_post_links(root, args, ["new", "old"], 0, False, "cursor0")
            pages = [(["new"], f"cursor{i}", True) for i in range(1, 6)] + [(["old"], None, False)]
            with patch.object(app, "fetch_authenticated_timeline_page", side_effect=pages) as fetch:
                links = app.collect_post_links(MagicMock(), "natgeo", 0, root, args, [], None, "1")
            self.assertEqual(fetch.call_count, 6)
            self.assertEqual(links, ["new", "old"])
            self.assertTrue(app.load_post_links(root, "natgeo")["completed"])

    def test_repeated_cursor_stops_without_losing_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100"])
            root = Path(directory)
            app.save_post_links(root, args, ["new"], 0, False, "cursor")
            with patch.object(app, "fetch_authenticated_timeline_page", return_value=(["new"], "cursor", True)):
                with self.assertRaises(app.ExportError):
                    app.collect_post_links(MagicMock(), "natgeo", 0, root, args, [], None, "1")
            self.assertEqual(app.load_post_links(root, "natgeo")["cursor"], "cursor")

    def test_browser_opens_file_uri_and_can_be_disabled(self):
        args = app.parse_args(["all", "natgeo"])
        path = Path(tempfile.gettempdir()) / "Архив Instagram" / "index.html"
        app.open_export(path, args)
        self.browser_open.assert_called_once_with(path.resolve().as_uri())
        self.browser_open.reset_mock()
        args.no_open = True
        app.open_export(path, args)
        self.browser_open.assert_not_called()

    def test_post_navigation_retries_transient_browser_error(self):
        with patch.object(app, "_scrape_post_payload_once", side_effect=[app.PlaywrightError("ERR_QUIC_PROTOCOL_ERROR"), {"code": "A"}]) as scrape, patch.object(app, "_safe_sleep"):
            self.assertEqual(app.scrape_post_payload(MagicMock(), "https://www.instagram.com/p/A/"), {"code": "A"})
            self.assertEqual(scrape.call_count, 2)

    def test_atomic_state_write_keeps_previous_state_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            app.write_json(path, {"saved": 1})
            with patch.object(Path, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    app.write_json(path, {"saved": 2})
            self.assertEqual(app.read_json(path), {"saved": 1})
            self.assertFalse(path.with_name("state.json.tmp").exists())

    def test_session_requires_authenticated_user(self):
        response = MagicMock(status_code=200)
        session = MagicMock()
        session.get.return_value = response
        response.json.return_value = {}
        self.assertFalse(app._session_is_valid(session))
        response.json.return_value = {"user": {"pk": "1"}}
        self.assertTrue(app._session_is_valid(session))
        response.raise_for_status.side_effect = requests.HTTPError("429")
        with self.assertRaises(requests.HTTPError):
            app._session_is_valid(session)

    def test_empty_comment_export_produces_files_and_skips_next_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["comments", "natgeo", "--sessionid", "test", "--output-dir", directory])
            with (
                patch.object(app, "_session_from_sessionid", return_value=MagicMock()),
                patch.object(app, "_save_config"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 1}, [], None, "1")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=(["https://www.instagram.com/p/A/"], None, False)),
                patch.object(app, "fetch_all_comments", return_value=[]) as fetch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run_comments(args), 0)
                self.assertEqual(app.run_comments(args), 0)
                fetch.assert_called_once()
            self.assertTrue((Path(directory) / "comments.html").exists())
            self.assertTrue((Path(directory) / "comments.csv").exists())

    def test_incomplete_all_timeline_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["all", "natgeo", "--output-dir", directory])
            with (
                patch.object(app, "InstagramBrowser"),
                patch.object(app, "preflight_instagram_access"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 100}, [], None, "1")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=(["https://www.instagram.com/p/A/"], None, False)),
                patch.object(app, "scrape_post_payload", return_value={}),
                patch.object(app, "build_post_record", return_value={"shortcode": "A"}),
                patch.object(app, "generate_html"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run(args), 4)
            self.assertFalse(app.load_export_state(Path(directory), args)["completed"])

    def test_video_download_is_opt_in(self):
        item = {"image_versions2": {"candidates": [{"url": "cover.jpg"}]},
                "video_versions": [{"url": "movie.mp4"}]}
        self.assertEqual(app._extract_media_url(item), "cover.jpg")
        self.assertEqual(app._extract_media_url(item, True), "movie.mp4")
        self.assertEqual(app._extract_media_url({"video_versions": [{"url": "movie.mp4"}]}), "")
        self.assertFalse(app.parse_args(["all", "natgeo"]).download_videos)
        self.assertTrue(app.parse_args(["all", "natgeo", "--download-videos"]).download_videos)

    def test_real_date_and_unknown_date_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "download_binary", return_value=Path(directory) / "cover.jpg"):
            payload = {"code": "A", "taken_at": None, "_fallback_timestamp": "2020-01-02T12:00:00Z",
                       "image_versions2": {"candidates": [{"url": "cover.jpg"}]}}
            record = app.build_post_record(MagicMock(), payload, Path(directory), 1, "https://www.instagram.com/p/A/")
            self.assertIn("02.01.2020", record["date_label"])
            payload.pop("_fallback_timestamp")
            record = app.build_post_record(MagicMock(), payload, Path(directory), 1, "https://www.instagram.com/p/A/")
            self.assertEqual(record["date_label"], "Date unknown")

    def test_carousel_has_individual_video_types(self):
        image = {"media_type": 1, "image_versions2": {"candidates": [{"url": "photo.jpg"}]}}
        video = {"media_type": 2, "image_versions2": {"candidates": [{"url": "cover.jpg"}]}, "video_versions": [{"url": "movie.mp4"}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "download_binary", side_effect=lambda session, url, base: base.with_suffix(".mp4" if url.endswith(".mp4") else ".jpg")) as download:
            payload = {"code": "A", "media_type": 8, "carousel_media": [image, video]}
            record = app.build_post_record(MagicMock(), payload, Path(directory), 1, "https://www.instagram.com/p/A/", True)
            self.assertEqual(record["media_types"], ["image", "video"])
            self.assertEqual(download.call_args.args[1], "movie.mp4")
            record = app.build_post_record(MagicMock(), payload, Path(directory), 1, "https://www.instagram.com/p/A/")
            self.assertEqual(record["media_types"], ["image", "image"])
            self.assertEqual(download.call_args.args[1], "cover.jpg")

    def test_empty_comments_are_cached_and_owner_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comments.json"
            app.save_comment_cache(path, "natgeo", [], {"A"})
            self.assertEqual(app.load_comment_cache(path, "natgeo"), ([], {"A"}))
            self.assertEqual(app.load_comment_cache(path, "other"), ([], set()))

    def test_comment_pagination_deduplicates_and_rejects_stuck_cursor(self):
        with patch.object(app, "fetch_comments_page", side_effect=[
            ([{"pk": "1"}], "next", True), ([{"pk": "1"}, {"pk": "2"}], None, False),
        ]):
            self.assertEqual(app.fetch_all_comments(MagicMock(), "A", 0), [{"pk": "1"}, {"pk": "2"}])
        with patch.object(app, "fetch_comments_page", return_value=([{"pk": "1"}], "next", True)):
            with self.assertRaises(app.ExportError):
                app.fetch_all_comments(MagicMock(), "A", 0)

    def test_refresh_replaces_comments_and_filters_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".comments.json"
            app.save_comment_cache(path, "natgeo", [app.build_comment_record({"pk": "old", "text": "old"}, "A"), app.build_comment_record({"pk": "other"}, "B")], {"A", "B"})
            args = app.parse_args(["comments", "natgeo", "--sessionid", "test", "--refresh", "--output-dir", directory])
            with (
                patch.object(app, "_session_from_sessionid", return_value=MagicMock()),
                patch.object(app, "_save_config"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 1}, ["https://www.instagram.com/p/A/"], None, "1")),
                patch.object(app, "collect_post_links", return_value=["https://www.instagram.com/p/A/"]),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=(["https://www.instagram.com/p/A/"], None, False)),
                patch.object(app, "fetch_all_comments", return_value=[{"pk": "new", "text": "fresh"}]) as fetch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run_comments(args), 0)
                fetch.assert_called_once()
            records, completed = app.load_comment_cache(path, "natgeo")
            self.assertEqual({record["comment_id"] for record in records}, {"new", "other"})
            self.assertEqual(completed, {"A", "B"})
            csv = (Path(directory) / "comments.csv").read_text(encoding="utf-8-sig")
            self.assertIn("fresh", csv)
            self.assertNotIn("other", csv)

    def test_partial_export_is_saved_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["all", "natgeo", "--output-dir", directory])
            with (
                patch.object(app, "InstagramBrowser"),
                patch.object(app, "preflight_instagram_access"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 2}, [], None, "1")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=([f"https://www.instagram.com/p/{code}/" for code in "AB"], None, False)),
                patch.object(app, "scrape_post_payload", side_effect=[{}, app.ExportError("failed")]),
                patch.object(app, "build_post_record", return_value={"shortcode": "A"}),
                patch.object(app, "generate_html"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run(args), 4)
            state = app.load_export_state(Path(directory), args)
            self.assertFalse(state["completed"])
            self.assertEqual(state["post_records"], [{"shortcode": "A"}])

    def test_interruption_keeps_completed_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["all", "natgeo", "--output-dir", directory])
            with (
                patch.object(app, "InstagramBrowser"),
                patch.object(app, "preflight_instagram_access"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 2}, [], None, "1")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=([f"https://www.instagram.com/p/{code}/" for code in "AB"], None, False)),
                patch.object(app, "scrape_post_payload", side_effect=[{}, KeyboardInterrupt()]),
                patch.object(app, "build_post_record", return_value={"shortcode": "A"}),
                patch.object(app, "generate_html"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.run(args)
            self.assertEqual(app.load_export_state(Path(directory), args)["post_records"], [{"shortcode": "A"}])

    def test_legacy_cache_continues_past_first_page_count(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100"])
            root = Path(directory)
            app.write_json(app.post_links_path(root), {
                "version": 1, "job": {"username": "natgeo"},
                "links": ["new"], "total": 1, "completed": True, "cursor": "next",
            })
            with patch.object(app, "fetch_authenticated_timeline_page", side_effect=[
                (["middle"], "last", True), (["old"], None, False),
            ]) as fetch:
                links = app.collect_post_links(MagicMock(), "natgeo", 1, root, args, [], None, "123")
            self.assertEqual(links, ["new", "middle", "old"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args_list[0].args[2], "next")
            self.assertTrue(app.load_post_links(root, "natgeo")["completed"])

    def test_unknown_total_follows_cursor_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100"])
            with patch.object(app, "fetch_authenticated_timeline_page", return_value=(["old"], None, False)) as fetch:
                links = app.collect_post_links(MagicMock(), "natgeo", 0, Path(directory), args, ["new"], "next", "123")
            self.assertEqual(links, ["new", "old"])
            fetch.assert_called_once()

    def test_oldest_rejects_incomplete_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100"])
            with self.assertRaises(app.ExportError):
                app.collect_post_links(MagicMock(), "natgeo", 200, Path(directory), args, ["new"], None, "123")

    def test_missing_avatar_does_not_block_export(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "100", "--output-dir", directory])
            with (
                patch.object(app, "InstagramBrowser"),
                patch.object(app, "preflight_instagram_access"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": None, "avatar_url": ""}, [], None, "123")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=(["https://www.instagram.com/p/A/"], None, False)) as fetch,
                patch.object(app, "scrape_post_payload", return_value={}),
                patch.object(app, "build_post_record", return_value={"shortcode": "A"}),
                patch.object(app, "generate_html") as generate,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run(args), 0)
            self.assertEqual(fetch.call_args.kwargs["user_id"], "123")
            self.assertTrue((Path(directory) / "avatar-placeholder.svg").exists())
            self.assertEqual(generate.call_args.kwargs["post_records"], [{"shortcode": "A"}])

    def test_interrupted_download_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "post"
            response = MagicMock()
            response.headers = {"Content-Type": "image/jpeg"}

            def interrupted(*args, **kwargs):
                yield b"partial"
                raise requests.ConnectionError("connection lost")

            response.iter_content.side_effect = interrupted
            session = MagicMock()
            session.get.return_value = response
            with self.assertRaises(app.ExportError):
                app.download_binary(session, "https://example.com/photo.jpg", base)
            self.assertIsNone(app.existing_binary_path(base))
            self.assertEqual(list(Path(directory).iterdir()), [])
            response.iter_content.side_effect = None
            response.iter_content.return_value = [b"complete"]
            downloaded = app.download_binary(session, "https://example.com/photo.jpg", base)
            self.assertEqual(downloaded.read_bytes(), b"complete")

    def test_comments_dry_run_does_not_fetch_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["comments", "natgeo", "--sessionid", "test", "--dry-run", "--output-dir", directory])
            with (
                patch.object(app, "_session_from_sessionid", return_value=MagicMock()),
                patch.object(app, "_save_config"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 1}, ["https://www.instagram.com/p/A/"], None, "1")),
                patch.object(app, "collect_post_links", return_value=["https://www.instagram.com/p/A/"]),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=(["https://www.instagram.com/p/A/"], None, False)),
                patch.object(app, "fetch_all_comments") as fetch,
            ):
                self.assertEqual(app.run_comments(args), 0)
                fetch.assert_not_called()
            self.assertFalse((Path(directory) / "comments.csv").exists())

    def test_resume_keeps_selected_order(self):
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(["oldest", "natgeo", "--limit", "3", "--output-dir", directory])
            media = Path(directory) / "media"
            media.mkdir()
            (media / "C.jpg").write_bytes(b"cached")
            cached = {"shortcode": "C", "local_media_path": "media/C.jpg"}
            (Path(directory) / "avatar.jpg").write_bytes(b"avatar")
            with (
                patch.object(app, "InstagramBrowser"),
                patch.object(app, "preflight_instagram_access"),
                patch.object(app, "fetch_profile_info", return_value=({"username": "natgeo", "mediacount": 3}, [], None, "1")),
                patch.object(app, "fetch_authenticated_timeline_page", return_value=([], None, False)),
                patch.object(app, "collect_post_links", return_value=[f"https://www.instagram.com/p/{code}/" for code in "CBA"]),
                patch.object(app, "load_export_state", return_value={"post_records": [cached], "avatar_src": "avatar.jpg"}),
                patch.object(app, "scrape_post_payload", return_value={}),
                patch.object(app, "build_post_record", side_effect=lambda session, payload, media_dir, index, url, **kwargs: {"shortcode": app.parse_shortcode_from_url(url)}),
                patch.object(app, "generate_html") as generate,
                patch.object(app, "save_export_state") as save,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run(args), 0)
                self.assertEqual([post["shortcode"] for post in generate.call_args.kwargs["post_records"]], list("ABC"))
                self.assertTrue(save.call_args.kwargs["completed"])


if __name__ == "__main__":
    unittest.main()
