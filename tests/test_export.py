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
                patch.object(app, "build_post_record", side_effect=lambda session, payload, media_dir, index, url: {"shortcode": app.parse_shortcode_from_url(url)}),
                patch.object(app, "generate_html") as generate,
                patch.object(app, "save_export_state") as save,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app.run(args), 0)
                self.assertEqual([post["shortcode"] for post in generate.call_args.kwargs["post_records"]], list("ABC"))
                self.assertTrue(save.call_args.kwargs["completed"])


if __name__ == "__main__":
    unittest.main()
