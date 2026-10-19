import contextlib
import csv
import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import insta_html_export as app
import igdump_insights as insights
from igdump_csv import csv_cell
from igdump_storage import save_comment_cache, read_json, LikerStore


LINKS = [f"https://www.instagram.com/p/{code}/" for code in "ABC"]


class PassiveDataTests(unittest.TestCase):
    def test_location_dates_group_pins_and_preserve_each_source(self):
        posts = [dict(shortcode=code, source_url=f'https://www.instagram.com/p/{code}/',
                      taken_at=stamp, location={'name': '<Place>', 'lat': lat, 'lng': 37.6})
                 for code, stamp, lat in [('A', 1704067200, 55.7), ('B', 1704240000, 55.7), ('C', None, 50)]]
        rows = insights.location_rows(posts)
        self.assertEqual(rows[0]['published_at'], datetime.fromtimestamp(1704067200, UTC).astimezone().isoformat(timespec='minutes'))
        self.assertIsNone(rows[2]['published_at'])
        html = insights.locations_map(rows)
        self.assertEqual(html.count('class="location-pin"'), 2)
        self.assertIn('(2 posts)', html)
        self.assertIn('Date unknown', html)
        self.assertIn('&lt;Place&gt;', html)
        self.assertNotIn('<Place>', html)
        for row in rows:
            self.assertIn(row['source_url'], html)

    def test_full_defaults_and_independent_flags(self):
        full = app.parse_args(["full", "natgeo"])
        self.assertTrue(all(getattr(full, flag) for flag in insights.REPORTS))
        for mode in ("posts", "comments", "likes"):
            normal = app.parse_args([mode, "natgeo"])
            self.assertFalse(any(getattr(normal, flag) for flag in insights.REPORTS))
            for flag in insights.REPORTS:
                args = app.parse_args([mode, "natgeo", f"--{flag}"])
                self.assertTrue(getattr(args, flag))
        args = app.parse_args(["full", "natgeo", "--no-analytics", "--no-mentions", "--no-contacts", "--no-locations"])
        self.assertFalse(any(getattr(args, flag) for flag in insights.REPORTS))

    def test_mobile_graphql_and_carousel_fields(self):
        raw = {"code": "A", "taken_at_timestamp": 1704067200,
            "edge_media_to_caption": {"edges": [{"node": {"text": "Hello @alice"}}]},
            "edge_sidecar_to_children": {"edges": [{"node": {
                "edge_media_to_tagged_user": {"edges": [{"node": {"user": {"username": "Alice", "id": "1"}}}]}}}]},
            "location": {"id": "9", "name": "Moscow", "latitude": 55.75, "longitude": 37.62}}
        row = insights.publication_details(raw)
        self.assertEqual(row['caption'], 'Hello @alice')
        self.assertEqual(row['taken_at'], 1704067200)
        self.assertEqual(row['tagged_users'][0]['username'], 'alice')
        self.assertEqual(row['location']['lat'], 55.75)
        self.assertNotIn('caption', insights.publication_details({'code': 'B'}))
        self.assertEqual(insights.publication_details({'caption': None})['caption'], '')
        self.assertEqual(insights.publication_details({'location': None})['location'], None)
        self.assertIsNone(insights.coordinate('inf', 90))
        self.assertIsNone(insights.coordinate(91, 90))
        self.assertEqual(insights.coordinate(0, 90), 0)

    def test_cache_merges_sparse_payloads_and_does_not_mix_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = requests.Session()
            app.remember_post_dates(session, [{'code': 'A', 'caption': {'text': 'hello'}, 'taken_at': 1704067200, 'usertags': {'in': []}}])
            app.save_post_metadata(session, root, 'natgeo')
            other = requests.Session()
            app.remember_post_dates(other, [{'code': 'A', 'like_count': 10}])
            app.save_post_metadata(other, root, 'natgeo')
            row = insights.cached_details(root, 'natgeo', LINKS[:1])[0]
            self.assertEqual(row['caption'], 'hello')
            self.assertEqual(row['tagged_users'], [])
            self.assertNotIn('caption', insights.cached_details(root, 'other', LINKS[:1])[0])

    def test_selection_and_legacy_post_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['posts', 'natgeo'])
            app.save_export_state(root, args, {}, None, [
                {'shortcode': 'A', 'caption': 'email a@example.org', 'date_label': '01.01.2024 12:00'},
                {'shortcode': 'B', 'caption': 'unselected'}], True)
            posts = insights.cached_details(root, 'natgeo', LINKS[:1])
            self.assertEqual(len(posts), 1)
            self.assertTrue(posts[0]['legacy_time'])
            self.assertEqual(insights.contact_rows(posts)[0]['value'], 'a@example.org')


class ExtractionTests(unittest.TestCase):
    def test_email_extraction_does_not_join_caption_mentions(self):
        caption = ('photos by @roberrtt.o\nplayed solo @thefable.la.\n'
                   'night @thefable.la props to @blaire.xyz\n'
                   '@mikedeluccia @blaire.xyz & @brendan.eder\n'
                   'real: sales@example.org person [at] example [dot] net')
        emails = {value for kind, value, evidence in insights.contact_matches(caption) if kind == 'email'}
        self.assertEqual(emails, {'sales@example.org', 'person@example.net'})
        self.assertEqual([item for item in insights.contact_matches('a@host.o a@host.123 name.@example.org') if item[0] == 'email'], [])
        mentions = insights.mention_rows([{'shortcode': 'A', 'source_url': LINKS[0], 'caption': caption}])
        self.assertTrue({'roberrtt.o', 'thefable.la', 'blaire.xyz', 'brendan.eder'} <= {row['username'] for row in mentions})

    def test_contact_normalization_sources_and_false_positives(self):
        caption = ('Email: Sales@Example.org / person [at] example [dot] net\n'
                   'Phone: +7 (999) 123-45-67\nТелефон 8 (999) 123-45-67\n'
                   'https://EXAMPLE.org/contact?q=1, t.me/alice www.example.com example.ru/path\n'
                   'Date 09.10.2026 IP 192.168.100.123 ID 1234567890123 @alice')
        posts = [{'shortcode': 'A', 'source_url': LINKS[0], 'caption': caption}]
        rows = insights.contact_rows(posts)
        found = {(row['type'], row['value']) for row in rows}
        self.assertIn(('email', 'sales@example.org'), found)
        self.assertIn(('email', 'person@example.net'), found)
        self.assertIn(('phone', '+79991234567'), found)
        self.assertEqual(sum(kind == 'phone' for kind, value in found), 1)
        self.assertIn(('url', 'https://example.org/contact?q=1'), found)
        self.assertIn(('url', 'https://t.me/alice'), found)
        self.assertIn(('url', 'https://example.ru/path'), found)
        self.assertNotIn(('url', 'https://example.org'), found)  # email domain
        self.assertTrue(all(row['source_url'] == LINKS[0] and row['context'] == caption for row in rows))
        self.assertEqual(list(insights.contact_matches('price 1000\n1234567890123\n09.10.2026\n192.168.100.123')), [])
        self.assertIn(('phone', '9991234567', '999) 123-45-67'), list(insights.contact_matches('(999) 123-45-67')))

    def test_mentions_do_not_confuse_emails_with_people_and_keep_tag_evidence(self):
        posts = [{'shortcode': 'A', 'source_url': LINKS[0],
            'caption': '@Alice @alice, @bob. email hi@example.org plus+name@host.net',
            'tagged_users': [{'username': 'ALICE', 'user_id': '1', 'full_name': 'Alice'}]}]
        rows = insights.mention_rows(posts)
        self.assertEqual({(r['username'], r['kind']) for r in rows}, {('alice', 'caption'), ('bob', 'caption'), ('alice', 'tag')})
        self.assertEqual(len(rows), 3)

    def test_publication_statistics_account_for_zero_days_and_unknown_times(self):
        times = [datetime(2024, 1, day, 12, tzinfo=UTC).timestamp() for day in (1, 3)]
        posts = [{'taken_at': stamp} for stamp in times] + [{}, {'taken_at': float('nan')}]
        stats = insights.publication_stats(posts)
        self.assertEqual(stats['dated_posts'], 2)
        self.assertEqual(stats['unknown_dates'], 2)
        self.assertEqual(stats['observed_days'], 3)
        self.assertEqual(stats['active_days'], 2)
        self.assertAlmostEqual(stats['posts_per_day'], 0.667)
        self.assertEqual(sum(stats['hours']), 2)
        self.assertEqual(sum(stats['weekdays']), 2)
        hour = datetime.fromtimestamp(times[0], UTC).astimezone().hour
        self.assertEqual(stats['hours'][hour], 2)
        self.assertIsNone(insights.publication_stats([])['posts_per_day'])


class CsvSafetyTests(unittest.TestCase):
    def test_formula_prefixes_are_protected_and_numbers_unchanged(self):
        for value in ('=1+1', '+123', '-1', '@alice', '  =1', '\tSUM(A1)', '\rhello', '\n=1', '\ufeff=1'):
            self.assertEqual(csv_cell(value), "'" + value)
        for value in (12, -1, 1.5, None, 'alice', 'hello =1', 'https://example.com'):
            self.assertEqual(csv_cell(value), value)

    def test_existing_comment_and_liker_exports_use_safe_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app._save_comments_csv([{'text': '=HYPERLINK("x")', 'username': '@alice', 'like_count': 2}], root)
            app._save_likers_csv([{'username': 'alice', 'full_name': '+danger'}], root)
            app._save_participant_stats({'participants': [{'username': 'alice', 'full_name': '-danger', 'count': 3}]}, root, 'likes')
            for filename, field, expected in (('comments.csv', 'text', "'=HYPERLINK(\"x\")"), ('likes.csv', 'full_name', "'+danger"), ('likes-stats.csv', 'full_name', "'-danger")):
                with (root / filename).open(encoding='utf-8-sig', newline='') as handle:
                    row = next(csv.DictReader(handle))
                    self.assertEqual(row[field], expected)


class ReportIntegrationTests(unittest.TestCase):
    def seed(self, root, args):
        app.save_post_links(root, args, LINKS, 3, True)
        app.save_export_state(root, args, {'username': 'natgeo'}, '', [], True)
        session = requests.Session()
        app.remember_post_dates(session, [
            {'code': 'A', 'taken_at': 1704067200, 'caption': {'text': '@alice a@example.org +7 (999) 123-45-67'},
             'usertags': {'in': [{'user': {'username': 'bob', 'pk': '2'}}]},
             'location': {'name': '<script>alert(1)</script>', 'pk': 1, 'lat': 55.75, 'lng': 37.62}},
            {'code': 'B', 'taken_at': 1704240000, 'caption': {'text': 'New post'}, 'location': None, 'usertags': {'in': []}},
        ])
        app.save_post_metadata(session, root, 'natgeo')
        save_comment_cache(root / '.comments.json', 'natgeo', [], {'A', 'B', 'C'})
        save_comment_cache(root / '.likers.json', 'natgeo', [], {'A', 'B', 'C'})

    def test_full_offline_produces_all_reports_without_network_or_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['full', 'natgeo', '--offline', '--output-dir', directory, '--no-open'])
            self.seed(root, args)
            with patch.object(requests.Session, 'request', side_effect=AssertionError('network')), patch.object(app, 'InstagramBrowser', side_effect=AssertionError('browser')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_full(args), 4)  # Metadata is present; media archive is incomplete.
            for kind in insights.REPORTS:
                for extension in ('html', 'csv', 'json'):
                    self.assertTrue((root / f'{kind}.{extension}').exists())
                self.assertIn(f'{kind}.html', (root / 'full.html').read_text(encoding='utf-8'))
            html = (root / 'locations.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>', html)
            self.assertIn('&lt;script&gt;', html)
            self.assertIn('Coordinate map', html)
            self.assertIn('full.html', html)
            self.assertEqual(read_json(root / 'analytics.json')['unknown_dates'], 1)

    def test_selected_report_only_uses_selected_posts_and_can_rebuild_without_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['posts', 'natgeo', '--offline', '--contacts', '--limit', '1', '--oldest', '--output-dir', directory, '--no-open'])
            self.seed(root, args)
            (root / '.export-state.json').unlink()
            with patch.object(requests.Session, 'request', side_effect=AssertionError('network')):
                self.assertEqual(app.run(args), 0)
            self.assertEqual(read_json(root / 'contacts.json')['records'], [])
            self.assertFalse((root / 'analytics.html').exists())

    def test_offline_date_filters_use_saved_timestamps_before_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['posts', 'natgeo', '--offline', '--analytics', '--after', '02.01.2024', '--before', '04.01.2024', '--limit', '1', '--output-dir', directory, '--no-open'])
            self.seed(root, args)
            # C has no date, so remove it from the saved selection for this test.
            app.save_post_links(root, args, LINKS[:2], 2, True)
            self.assertFalse((root / '.post-dates.json').exists())
            with patch.object(requests.Session, 'request', side_effect=AssertionError('network')):
                self.assertEqual(app.run(args), 4)  # Selected media was not collected in this fixture.
            stats = read_json(root / 'analytics.json')
            self.assertEqual(stats['dated_posts'], 1)
            self.assertEqual(stats['selected_posts'], 1)
            self.assertEqual(stats['first_day'], datetime.fromtimestamp(1704240000, UTC).astimezone().date().isoformat())

    def test_full_offline_keeps_limited_archive_order_and_disabled_report_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['full', 'natgeo', '--offline', '--limit', '1', '--oldest', '--no-contacts', '--output-dir', directory, '--no-open'])
            self.seed(root, args)
            (root / 'media').mkdir()
            for code in 'ABC':
                (root / 'media' / f'{code}.jpg').write_bytes(b'image')
            app.save_export_state(root, args, {'username': 'natgeo'}, '', [
                {'shortcode': code, 'caption': f'caption-{code}', 'date_label': 'Date unknown', 'local_media_path':f'media/{code}.jpg'} for code in 'ABC'], True)
            (root / 'contacts.html').write_text('stale report', encoding='utf-8')
            with patch.object(requests.Session, 'request', side_effect=AssertionError('network')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_full(args), 0)
            html = (root / 'index.html').read_text(encoding='utf-8')
            self.assertIn('caption-C', html)
            self.assertNotIn('caption-A', html)
            self.assertNotIn('contacts.html', html)
            self.assertNotIn('contacts.html', (root / 'full.html').read_text(encoding='utf-8'))

    def test_coverage_csv_protects_server_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['likes', 'natgeo', '--offline', '--output-dir', directory, '--no-open'])
            app.save_post_links(root, args, LINKS[:1], 1, True)
            store = LikerStore(root / '.likers.sqlite', 'natgeo')
            store.mark('A', 'partial', expected=2, error='=untrusted()')
            store.close()
            with patch.object(requests.Session, 'request', side_effect=AssertionError('network')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_comments(args), 4)  # Offline report must retain incomplete coverage.
            with (root / 'likes-coverage.csv').open(encoding='utf-8-sig', newline='') as handle:
                self.assertEqual(next(csv.DictReader(handle))['reason'], "'=untrusted()")

    def test_flags_do_not_add_requests_during_post_scan(self):
        for flags in ([], ['--analytics', '--mentions', '--contacts', '--locations']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = app.parse_args(['posts', 'natgeo', '--no-open', '--output-dir', directory, *flags])
                payload = {'code': 'A', 'caption': {'text': '@alice a@example.org'}, 'taken_at': 1704067200, 'location': {'name': 'Moscow', 'lat': 55.75, 'lng': 37.62}, 'usertags': {'in': []}}
                browser = MagicMock()
                browser.authenticated_session.return_value = requests.Session()
                media = root / 'media' / 'A.jpg'
                media.parent.mkdir()
                media.write_bytes(b'image')
                with patch.object(app, 'InstagramBrowser') as factory, patch.object(app, 'preflight_instagram_access'), patch.object(app, 'fetch_profile_info', return_value=({'username': 'natgeo', 'mediacount': 1}, [], None, '1')) as profile, patch.object(app, 'fetch_authenticated_timeline_page', return_value=(LINKS[:1], None, False)) as timeline, patch.object(app, 'scrape_post_payload', return_value=payload) as scrape, patch.object(app, 'build_post_record', return_value={**insights.publication_details(payload), 'shortcode': 'A', 'caption': '@alice a@example.org', 'local_media_path': 'media/A.jpg'}) as build, patch.object(requests.Session, 'request', side_effect=AssertionError('extra HTTP')), contextlib.redirect_stdout(io.StringIO()):
                    factory.return_value.__enter__.return_value = browser
                    self.assertEqual(app.run(args), 0)
                    self.assertEqual((profile.call_count, timeline.call_count, scrape.call_count, build.call_count), (1, 1, 1, 1))
                self.assertEqual((root / 'analytics.html').exists(), bool(flags))


if __name__ == '__main__':
    unittest.main()
