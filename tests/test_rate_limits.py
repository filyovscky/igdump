import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests
import insta_html_export as app
from igdump_progress import TerminalUI


class RateLimitTests(unittest.TestCase):
    def test_browser_profile_recovers_avatar_and_counts_for_target_only(self):
        session = requests.Session()
        browser = MagicMock()
        page = browser.open_page.return_value
        page.url = app.IG_BASE_URL + '/oddnesse/'
        page.text_content.return_value = 'oddnesse'
        page.content.return_value = '<script>' + json.dumps({'users': [
            {'username': 'viewer', 'id': '999', 'profile_pic_url': 'https://cdn.example/wrong.jpg'},
            {'username': 'oddnesse', 'id': '123', 'profile_pic_url_hd': 'https://cdn.example/right.jpg', 'media_count': 112, 'follower_count': 500, 'full_name': 'Target'}]}) + '</script>'
        with patch.object(app, 'fetch_profile_info', side_effect=app.ProfileHTMLUnavailableError('login')):
            profile, _, _, uid = app.resolve_profile_for_posts(session, browser, 'oddnesse')
        self.assertEqual(uid, '123')
        self.assertEqual(profile['avatar_url'], 'https://cdn.example/right.jpg')
        self.assertEqual(profile['mediacount'], 112)
        self.assertEqual(profile['followers'], 500)

    def test_profile_avatar_from_metadata_handles_attribute_order_and_escaping(self):
        html = '<meta content="https://cdn.example/avatar.jpg?a=1&amp;b=2" property="og:image">'
        self.assertEqual(app.profile_metadata_from_html(html, 'oddnesse')['avatar_url'], 'https://cdn.example/avatar.jpg?a=1&b=2')
        self.assertEqual(app.profile_metadata_from_html('<script>{"username":"viewer","profile_pic_url":"https://cdn.example/wrong.jpg"}</script>', 'oddnesse'), {})

    @staticmethod
    def timeline_payload(errors=None):
        return {'data': {'xdt_api__v1__feed__user_timeline_graphql_connection': {
            'edges': [{'node': {'code': 'A', 'taken_at': 1704067200}}],
            'page_info': {'end_cursor': 'next', 'has_next_page': True}}}, 'errors': errors or []}

    def test_browser_timeline_data_is_used_without_another_request(self):
        session = requests.Session()
        session._igdump_timeline_pages = {None: self.timeline_payload()}
        with patch.object(session, 'post', side_effect=AssertionError('unnecessary request')):
            self.assertEqual(app._try_graphql_timeline(session, 'oddnesse', None),
                             (['https://www.instagram.com/p/A/'], 'next', True))

    def test_pagination_reuses_observed_browser_request(self):
        session = requests.Session()
        page = MagicMock()
        session._igdump_timeline_page = page
        session._igdump_timeline_templates = {'next': {
            'form': {'doc_id': 'observed-id', 'variables': '{}'},
            'variables': {'username': 'oddnesse', 'after': 'old'},
            'headers': {'x-fb-lsd': 'test-token', 'cookie': 'must-not-copy'}}}
        page.evaluate.return_value = {'status': 200, 'body': json.dumps(self.timeline_payload())}
        with patch.object(session, 'post', side_effect=AssertionError('wrong transport')):
            app._try_graphql_timeline(session, 'oddnesse', 'next')
        args = page.evaluate.call_args.args[1]
        self.assertIn('doc_id=observed-id', args['body'])
        self.assertIn('next', args['body'])
        self.assertNotIn('cookie', args['headers'])

    def test_api_graphql_endpoint_is_captured_and_replayed_without_rewriting_url(self):
        session = requests.Session()
        page = MagicMock()
        session._igdump_timeline_page = page
        response = MagicMock(url=app.IG_BASE_URL+'/api/graphql', status=200)
        response.request.post_data = app.urlencode({'doc_id':'observed', 'variables':json.dumps({'username':'oddnesse','after':'cursor'})})
        response.request.all_headers.return_value = {'x-fb-lsd':'dummy'}
        response.json.return_value = self.timeline_payload()
        app.capture_browser_timeline(session, 'oddnesse', response)
        self.assertIn('cursor', session._igdump_timeline_pages)
        page.evaluate.return_value = {'status':200,'body':json.dumps(self.timeline_payload())}
        with patch.object(session,'post',side_effect=AssertionError('wrong transport')):
            app._try_graphql_timeline(session,'oddnesse','another-cursor')
        self.assertEqual(page.evaluate.call_args.args[1]['url'],app.IG_BASE_URL+'/api/graphql')

    def test_profile_waits_for_delayed_api_graphql_response_before_closing_page(self):
        session = requests.Session()
        browser = MagicMock()
        page = browser.open_page.return_value
        page.url = app.IG_BASE_URL+'/oddnesse/'
        page.text_content.return_value = 'oddnesse'
        page.content.return_value = '<script>{"username":"oddnesse","id":"123"}</script>'
        response = MagicMock(url=app.IG_BASE_URL+'/api/graphql', status=200)
        response.request.post_data = app.urlencode({'doc_id':'observed','variables':json.dumps({'username':'oddnesse'})})
        response.request.all_headers.return_value = {}
        response.json.return_value = self.timeline_payload()
        def delayed(milliseconds):
            if milliseconds == 500:
                browser.require_context().on.call_args.args[1](response)
        page.wait_for_timeout.side_effect = delayed
        with patch.object(app,'fetch_profile_info',side_effect=app.ProfileHTMLUnavailableError('login')):
            app.resolve_profile_for_posts(session,browser,'oddnesse')
        page.close.assert_not_called()
        with patch.object(session,'post',side_effect=AssertionError('cached browser page was ignored')):
            self.assertEqual(app._try_graphql_timeline(session,'oddnesse',None)[0],['https://www.instagram.com/p/A/'])

    def test_profile_api_success_still_observes_chrome_timeline_before_using_static_query(self):
        session = requests.Session()
        browser = MagicMock()
        with patch.object(app,'fetch_profile_info',return_value=({'username':'oddnesse'},[],None,'123')):
            app.resolve_profile_for_posts(session,browser,'oddnesse')
        def bootstrap(*args, **kwargs):
            self.assertTrue(kwargs['force_browser'])
            session._igdump_timeline_pages = {None:self.timeline_payload()}
        with patch.object(app,'resolve_profile_for_posts',side_effect=bootstrap) as observe, patch.object(session,'post',side_effect=AssertionError('stale guessed query')):
            self.assertEqual(app._try_graphql_timeline(session,'oddnesse',None)[0],['https://www.instagram.com/p/A/'])
        observe.assert_called_once()

    def test_missing_pagination_template_is_observed_before_any_guessed_request(self):
        session = requests.Session()
        session._igdump_timeline_page = MagicMock()
        session._igdump_timeline_templates = {'first': {'form': {'doc_id': 'first-only'}}}
        expected = (['https://www.instagram.com/p/B/'], 'next', True)
        with patch.object(app, 'observe_browser_pagination', return_value=expected) as observe, patch.object(session, 'post', side_effect=AssertionError('guessed query')):
            self.assertEqual(app._try_graphql_timeline(session, 'oddnesse', 'first-cursor'), expected)
        observe.assert_called_once_with(session, 'oddnesse', 'first-cursor')

    def test_browser_scroll_captures_real_next_page_and_removes_listener(self):
        session = requests.Session()
        page = MagicMock()
        session._igdump_timeline_page = page
        response = MagicMock(url=app.IG_BASE_URL + '/graphql/query/', status=200)
        response.request.post_data = app.urlencode({'doc_id': 'next-only', 'variables': json.dumps({'username': 'oddnesse', 'after': 'first-cursor'})})
        response.json.return_value = self.timeline_payload()
        response.request.all_headers.return_value = {'cookie': 'private', 'x-fb-lsd': 'token'}
        def respond(_):
            callback = page.context.on.call_args.args[1]
            callback(response)
        page.wait_for_timeout.side_effect = respond
        self.assertEqual(app.observe_browser_pagination(session, 'oddnesse', 'first-cursor')[0], ['https://www.instagram.com/p/A/'])
        self.assertEqual(session._igdump_timeline_templates['next']['form']['doc_id'], 'next-only')
        self.assertNotIn('cookie', session._igdump_timeline_templates['next']['headers'])
        page.context.remove_listener.assert_called_once()

    def test_rest_fallback_starts_fresh_and_resumes_with_its_own_cursor(self):
        session = requests.Session()
        error = requests.HTTPError('400')
        with patch.object(app, '_try_graphql_timeline', side_effect=error) as graphql, patch.object(app, 'fetch_user_timeline_rest', side_effect=[(['A'], 'rest-next', True), (['B'], None, False)]) as rest:
            first = app.fetch_authenticated_timeline_page(session, 'oddnesse', 'graphql-cursor', '123')
            self.assertEqual(first, (['A'], 'igdump-rest:rest-next', True))
            rest.assert_called_once_with(session, '123', None)
            self.assertEqual(app.fetch_authenticated_timeline_page(session, 'oddnesse', first[1], '123'), (['B'], None, False))
            self.assertEqual(rest.call_args.args, (session, '123', 'rest-next'))
            graphql.assert_called_once()

    def test_browser_pagination_429_never_falls_back_to_rest(self):
        session = requests.Session()
        session._igdump_timeline_page = MagicMock()
        session._igdump_timeline_templates = {'first': {}}
        session._igdump_browser_timeline_limited = True
        with patch.object(app, 'fetch_user_timeline_rest') as rest:
            with self.assertRaises(app.InstagramRateLimitError):
                app.fetch_authenticated_timeline_page(session, 'oddnesse', 'next', '123')
        rest.assert_not_called()

    def test_location_picture_errors_do_not_discard_valid_timeline(self):
        payload = self.timeline_payload([{'path': ['xdt_api__v1__feed__user_timeline_graphql_connection', 'edges', 0, 'node', 'location', 'profile_pic_url']}])
        self.assertEqual(app._parse_timeline_payload(requests.Session(), 'oddnesse', payload)[0], ['https://www.instagram.com/p/A/'])
        payload['errors'][0]['path'][-1] = 'code'
        with self.assertRaises(app.ExportError):
            app._parse_timeline_payload(requests.Session(), 'oddnesse', payload)

    def test_public_login_redirect_is_not_a_resolved_profile(self):
        session = MagicMock()
        response = session.get.return_value
        response.status_code = 200
        response.url = 'https://www.instagram.com/accounts/login/?next=/oddnesse/'
        with patch.object(app, '_resolve_user_id_via_search') as search:
            with self.assertRaises(app.ProfileHTMLUnavailableError):
                app._scrape_profile_html('oddnesse', session=session)
            search.assert_not_called()

    def test_login_redirect_uses_authenticated_page_and_supplies_rest_id(self):
        browser = MagicMock()
        page = browser.open_page.return_value
        page.url = 'https://www.instagram.com/oddnesse/'
        page.text_content.return_value = 'oddnesse'
        page.content.return_value = '<script>requireLazy([],function(){var data={"username":"oddnesse","id":"12345"};});</script>'
        with patch.object(app, 'fetch_profile_info', side_effect=app.ProfileHTMLUnavailableError('login redirect')), patch.object(app, '_resolve_user_id_via_search') as search:
            result = app.resolve_profile_for_posts(MagicMock(), browser, 'oddnesse')
            self.assertEqual(result[3], '12345')
            search.assert_not_called()
            page.close.assert_called_once()

    def test_authenticated_profile_failure_is_explicit(self):
        browser = MagicMock()
        page = browser.open_page.return_value
        page.url = 'https://www.instagram.com/oddnesse/'
        page.text_content.return_value = 'oddnesse'
        page.content.return_value = '<html>oddnesse</html>'
        with patch.object(app, 'fetch_profile_info', return_value=({'username': 'oddnesse'}, [], None, '')), patch.object(app, '_resolve_user_id_via_search', return_value=''):
            with self.assertRaisesRegex(app.ExportError, 'supplied no profile ID'):
                app.resolve_profile_for_posts(MagicMock(), browser, 'oddnesse')
            page.close.assert_called_once()

    def test_api_429_then_public_html_disconnect_still_uses_chrome(self):
        session = requests.Session()
        response = requests.Response()
        response.status_code = 429
        response._content = b'{}'
        response._content_consumed = True
        browser = MagicMock()
        page = browser.open_page.return_value
        page.url = 'https://www.instagram.com/oddnesse/'
        page.text_content.return_value = 'oddnesse'
        page.content.return_value = '<script type="application/json">{"username":"oddnesse","pk":"12345"}</script>'
        with patch.object(session, 'get', return_value=response) as api, patch.object(app, '_scrape_profile_html', side_effect=requests.ConnectionError('RemoteDisconnected: Remote end closed connection without response')) as html:
            self.assertEqual(app.resolve_profile_for_posts(session, browser, 'oddnesse')[3], '12345')
            api.assert_called_once()
            html.assert_called_once_with('oddnesse')
            browser.open_page.assert_called_once_with(page.url)

    def test_profile_timeouts_use_chrome_but_rate_limits_stop(self):
        for error in (requests.Timeout('timeout'), requests.ConnectionError('connection aborted'), requests.exceptions.SSLError('SSLEOFError')):
            session = requests.Session()
            browser = MagicMock()
            page = browser.open_page.return_value
            page.url = 'https://www.instagram.com/oddnesse/'
            page.text_content.return_value = 'oddnesse'
            page.content.return_value = '<script type="application/json">{"username":"oddnesse","id":"12345"}</script>'
            with self.subTest(error=error), patch.object(app, 'fetch_profile_info', side_effect=error):
                self.assertEqual(app.resolve_profile_for_posts(session, browser, 'oddnesse')[3], '12345')
        with patch.object(app, 'fetch_profile_info', side_effect=app.InstagramRateLimitError('HTML 429')):
            browser = MagicMock()
            with self.assertRaises(app.InstagramRateLimitError):
                app.resolve_profile_for_posts(requests.Session(), browser, 'oddnesse')
            browser.open_page.assert_not_called()

    def test_profile_ssl_eof_uses_authorized_transport_once(self):
        session = requests.Session()
        transport = MagicMock()
        session._igdump_profile_transport = transport
        native = transport.get.return_value
        native.status = 200
        native.url = 'https://www.instagram.com/api/v1/users/web_profile_info/'
        native.headers = {'content-type': 'application/json'}
        native.body.return_value = b'{"data":{"user":{"username":"oddnesse","pk":"12345"}}}'
        with patch.object(session, 'get', side_effect=requests.exceptions.SSLError('UNEXPECTED_EOF_WHILE_READING')) as get:
            profile, _, _, uid = app.fetch_profile_info(session, 'oddnesse')
            self.assertEqual(profile['username'], 'oddnesse')
            self.assertEqual(uid, '12345')
            get.assert_called_once()
            transport.get.assert_called_once()
            native.dispose.assert_called_once()

    def test_profile_tls_fallback_429_uses_html(self):
        session = requests.Session()
        transport = MagicMock()
        session._igdump_profile_transport = transport
        native = transport.get.return_value
        native.status = 429
        native.url = 'https://www.instagram.com/api/v1/users/web_profile_info/'
        native.headers = {}
        native.body.return_value = b'{}'
        with patch.object(session, 'get', side_effect=requests.exceptions.SSLError('SSLEOFError')), patch.object(app, '_scrape_profile_html') as html:
            self.assertIs(app.fetch_profile_info(session, 'oddnesse'), html.return_value)
            transport.get.assert_called_once()
            html.assert_called_once_with('oddnesse')

    def test_certificate_errors_do_not_trigger_transport_fallback(self):
        session = requests.Session()
        session._igdump_profile_transport = MagicMock()
        with patch.object(session, 'get', side_effect=requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED')):
            with self.assertRaises(requests.exceptions.SSLError):
                app.fetch_profile_info(session, 'oddnesse')
        session._igdump_profile_transport.get.assert_not_called()

    def test_terminal_stops_preparing_after_failed_run(self):
        ui = TerminalUI(quiet=True)
        ui.finish(4)
        task = ui.progress.tasks[0]
        self.assertIn('Stopped', task.description)
        self.assertIsNotNone(task.stop_time)

    def test_profile_429_uses_html_once(self):
        session = MagicMock()
        session.get.return_value.status_code = 429
        with patch.object(app, '_scrape_profile_html') as html, patch.object(app, '_resolve_user_id_via_search') as search:
            self.assertIs(app.fetch_profile_info(session, 'oddnesse'), html.return_value)
            html.assert_called_once_with('oddnesse')
            search.assert_not_called()
        self.assertEqual(session.get.call_count, 1)

    def test_logging_wrapper_allows_profile_429_to_reach_html_fallback(self):
        session = requests.Session()
        response = requests.Response()
        response.status_code = 429
        response._content = b'{}'
        response._content_consumed = True
        with patch.object(session, 'send', return_value=response) as send, patch.object(app, '_scrape_profile_html', return_value=({'username': 'oddnesse'}, [], None, '12345')) as html:
            app._patch_session_for_logging(session)
            self.assertEqual(app.fetch_profile_info(session, 'oddnesse')[3], '12345')
            send.assert_called_once()
            html.assert_called_once_with('oddnesse')

    def test_profile_api_and_html_429_do_not_loop(self):
        session = requests.Session()
        response = requests.Response()
        response.status_code = 429
        response._content = b'{}'
        response._content_consumed = True
        with patch.object(session, 'get', return_value=response) as api, patch.object(app, '_scrape_profile_html', side_effect=app.InstagramRateLimitError('HTML 429')) as html:
            with self.assertRaises(app.InstagramRateLimitError):
                app.fetch_profile_info(session, 'oddnesse')
            api.assert_called_once()
            html.assert_called_once_with('oddnesse')

    def test_full_failed_timeline_does_not_repeat_for_interactions_or_open(self):
        for error in (app.InstagramRateLimitError('429'), app.ExportError('timeline unavailable')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                args = app.parse_args(['full', 'oddnesse', '--output-dir', folder])
                with patch.object(app, 'run', side_effect=error) as posts, patch.object(app, 'run_comments') as interactions, patch.object(app, 'open_export') as opener, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(app.run_full(args), 4)
                    posts.assert_called_once()
                    interactions.assert_not_called()
                    opener.assert_not_called()

    def test_full_keeps_state_on_rate_limit_and_does_not_start_interactions(self):
        with tempfile.TemporaryDirectory() as folder:
            args = app.parse_args(['full', 'oddnesse', '--output-dir', folder, '--no-open'])
            session = MagicMock()
            def fail(phase):
                phase._shared_session = session
                phase._selected_links = ['https://www.instagram.com/p/A/']
                raise app.InstagramRateLimitError('429')
            with patch.object(app, 'run', side_effect=fail), patch.object(app, 'run_comments') as interactions, patch.object(app, 'passive_reports', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.run_full(args), 4)
                interactions.assert_not_called()
                self.assertIs(args._shared_session, session)
                self.assertEqual(len(args._selected_links), 1)

    def test_graphql_400_falls_back_once_without_sleep(self):
        response = requests.Response()
        response.status_code = 400
        error = requests.HTTPError('400', response=response)
        with patch.object(app, '_try_graphql_timeline', side_effect=error) as graphql, patch.object(app, 'fetch_user_timeline_rest', return_value=([], None, False)) as rest, patch.object(app, '_safe_sleep') as sleep:
            self.assertEqual(app.fetch_authenticated_timeline_page(MagicMock(), 'oddnesse', None, '123'), ([], None, False))
            graphql.assert_called_once()
            rest.assert_called_once()
            sleep.assert_not_called()

    def test_rest_and_search_429_stop_without_retry(self):
        for func, args in ((app.fetch_user_timeline_rest, ('123', None)), (app._resolve_user_id_via_search, ('oddnesse',))):
            session = MagicMock()
            session.get.return_value.status_code = 429
            with self.subTest(func=func), patch.object(app, '_safe_sleep') as sleep:
                with self.assertRaises(app.InstagramRateLimitError):
                    if func is app._resolve_user_id_via_search:
                        func(*args, session=session)
                    else:
                        func(session, *args)
                self.assertEqual(session.get.call_count, 1)
                sleep.assert_not_called()

    def test_embedded_profile_id_does_not_pick_viewer_or_media(self):
        text = '<script type="application/json">{"viewer":{"username":"me","id":"99999"},"media":{"id":"88888"},"profile":{"username":"oddnesse","pk":12345}}</script>'
        self.assertEqual(app._extract_user_id_from_html(text, 'oddnesse'), '12345')
        self.assertEqual(app._extract_user_id_from_html(text, 'missing'), '')
        self.assertEqual(app._extract_user_id_from_html('<script type="application/ld+json">{"identifier":{"@type":"PropertyValue","value":"12345"}}</script>', 'oddnesse'), '12345')
