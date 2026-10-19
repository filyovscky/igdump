import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import insta_html_export as app
from igdump_storage import LikerStore, PostStore, read_json, write_json, export_lock
from igdump_csv import write_csv


URL = 'https://www.instagram.com/p/A/'


def response(payload, url='https://www.instagram.com/api/v1/test/'):
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result._content = json.dumps(payload).encode()
    return result


class AuditFixTests(unittest.TestCase):
    def test_timeline_payload_cache_is_bounded(self):
        session=requests.Session()
        app.remember_post_dates(session,[{'code':f'post{i}','media_type':1,'image_versions2':{'candidates':[{'url':'https://cdn.example/photo.jpg'}]}} for i in range(300)])
        self.assertEqual(len(session._igdump_media_payloads),app.TIMELINE_MEDIA_CACHE_LIMIT)
        self.assertIn('post0',session._igdump_media_payloads)

    def test_media_does_not_accept_login_html_as_an_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data=response({},'https://cdn.example/avatar.jpg')
            data.headers['Content-Type']='text/html; charset=utf-8'
            data._content_consumed=True
            with patch.object(requests.Session,'get',return_value=data):
                with self.assertRaisesRegex(app.ExportError,'instead of media'):
                    app.download_binary(requests.Session(),'https://cdn.example/avatar.jpg',root/'avatar')
            self.assertEqual(list(root.iterdir()),[])

    def test_offline_archive_with_missing_media_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['posts','audit','--offline','--output-dir',directory,'--no-open'])
            app.save_post_links(root,args,[URL],1,True)
            app.save_export_state(root,args,{'username':'audit'},'', [{'shortcode':'A','local_media_path':'media/A.jpg'}],True)
            with patch.object(app,'generate_html'):
                self.assertEqual(app.run_posts_offline(args,root),4)
                (root/'media').mkdir()
                (root/'media'/'A.jpg').write_bytes(b'image')
                self.assertEqual(app.run_posts_offline(args,root),0)

    def test_run_reuses_timeline_media_and_renders_only_initial_and_final(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['posts','audit','--output-dir',directory,'--no-open'])
            session=requests.Session()
            nodes=[{'code':code,'taken_at':1704070800-i,'media_type':1,'image_versions2':{'candidates':[{'url':'https://cdn.example/photo.jpg'}]}} for i,code in enumerate('AB')]
            app.remember_post_dates(session,nodes)
            links=[URL,URL.replace('/A/','/B/')]
            browser=MagicMock()
            browser.authenticated_session.return_value=session
            with patch.object(app,'InstagramBrowser') as factory, patch.object(app,'preflight_instagram_access'), patch.object(app,'fetch_profile_info',return_value=({'username':'audit','mediacount':2},[],None,'123')), patch.object(app,'fetch_authenticated_timeline_page',return_value=(links,None,False)), patch.object(app,'scrape_post_payload',side_effect=AssertionError('unnecessary post navigation')), patch.object(app,'build_post_record',side_effect=lambda s,p,*a,**k:{'shortcode':p['code']}), patch.object(app,'generate_html') as render, patch.object(app.time,'monotonic',return_value=100), contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.__enter__.return_value=browser
                # The renderer is mocked, so create its target for checkpoint scheduling.
                render.side_effect=lambda **kwargs:(root/'index.html').write_text('report',encoding='utf-8')
                self.assertEqual(app.run(args),0)
                self.assertEqual(render.call_count,2)
            with PostStore(root/'.posts.sqlite','audit') as store:
                self.assertEqual(len(store.records()),2)

    def test_interrupt_between_json_checkpoints_recovers_post_from_sqlite(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['posts','audit','--output-dir',directory,'--no-open'])
            links=[URL,URL.replace('/A/','/B/')]
            with patch.object(app,'InstagramBrowser'), patch.object(app,'preflight_instagram_access'), patch.object(app,'fetch_profile_info',return_value=({'username':'audit','mediacount':2},[],None,'123')), patch.object(app,'fetch_authenticated_timeline_page',return_value=(links,None,False)), patch.object(app,'scrape_post_payload',return_value={}), patch.object(app,'build_post_record',side_effect=[{'shortcode':'A'},KeyboardInterrupt()]), patch.object(app,'generate_html') as render, patch.object(app.time,'monotonic',return_value=100):
                render.side_effect=lambda **kwargs:(root/'index.html').write_text('report',encoding='utf-8')
                with self.assertRaises(KeyboardInterrupt):
                    app.run(args)
            self.assertEqual(read_json(root/'.export-state.json')['post_records'],[])
            offline=app.parse_args(['posts','audit','--offline','--output-dir',directory,'--no-open'])
            with patch.object(requests.Session,'request',side_effect=AssertionError('network')), patch.object(app,'generate_html') as render:
                self.assertEqual(app.run_posts_offline(offline,root),4)
            self.assertEqual(render.call_args.args[1],[{'shortcode':'A'}])

    def test_latest_limit_stops_only_with_known_pin_status_and_dated_order(self):
        for known in [False, True]:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                args=app.parse_args(['posts','audit','--limit','1'])
                session=requests.Session()
                nodes=[{'code':'A','taken_at':1704067200,'is_pinned':True}, {'code':'B','taken_at':1704070800}]
                if known:
                    nodes[1]['is_pinned']=False
                app.remember_post_dates(session,nodes)
                seed=[URL,URL.replace('/A/','/B/')]
                with patch.object(app,'fetch_authenticated_timeline_page',return_value=([],None,False)) as fetch:
                    links=app.collect_post_links(session,'audit',100,root,args,seed,'next','123')
                self.assertEqual(fetch.call_count,0 if known else 1)
                if known:
                    self.assertFalse(app.load_post_links(root,'audit')['completed'])
                    self.assertTrue(app.timeline_satisfies_selection(root,args))
                    ordered=app.filter_links_by_date(links,args,session,root)
                    self.assertEqual(app.select_post_links(ordered,args),[seed[1]])
                    args.limit=10
                    self.assertFalse(app.timeline_satisfies_selection(root,args))

    def test_posts_survive_without_json_checkpoint_and_enrich_passive_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with PostStore(root/'.posts.sqlite','audit') as store:
                store.add({'shortcode':'A','taken_at':1704067200,'caption':'saved before interruption'})
            details=app.cached_details(root,'audit',[URL])
            self.assertEqual(details[0]['caption'],'saved before interruption')
            with PostStore(root/'.posts.sqlite','audit') as store:
                self.assertEqual(len(store.records()),1)

    def test_disabling_immediate_like_recheck_keeps_partial_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['likes','audit','--no-recheck-likes','--no-open'])
            app.save_post_links(root,args,[URL],1,True)
            with patch.object(requests.Session,'get',return_value=response({'users':[{'pk':'1','username':'alice'}],'user_count':2})) as get, patch.object(app,'_safe_sleep'), patch.object(app,'write_interaction_report'):
                self.assertEqual(app.export_likers(args,requests.Session(),root,'audit',[URL]),4)
                self.assertEqual(get.call_count,1)

    def test_browser_transport_remains_open_for_standalone_interactions(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            args=app.parse_args(['likes','audit','--browser-profile-dir',directory,'--output-dir',directory,'--no-open'])
            browser=MagicMock()
            browser.authenticated_session.return_value=requests.Session()
            with patch.object(app,'InstagramBrowser') as factory, patch.object(app,'resolve_profile_for_posts',return_value=({'username':'audit','mediacount':1},[],None,'123')) as resolve, patch.object(app,'fetch_authenticated_timeline_page',return_value=([URL],None,False)), patch.object(app,'export_likers',return_value=0) as export:
                factory.return_value.__enter__.return_value=browser
                export.side_effect=lambda *a: (factory.return_value.__exit__.assert_not_called() or 0)
                self.assertEqual(app.run_comments(args),0)
                resolve.assert_called_once()

    def test_comment_refresh_failure_keeps_previous_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['comments','audit','--refresh','--no-open'])
            app.save_post_links(root,args,[URL],1,True)
            app.save_comment_cache(root/'.comments.json','audit',[{'post_shortcode':'A','comment_id':'1','username':'alice','full_name':'','profile_pic_url':'','text':'old','created_at':0,'like_count':0,'child_comment_count':0}],{'A'})
            with patch.object(app,'fetch_all_comments',side_effect=requests.ConnectionError('closed')), patch.object(app,'write_interaction_report'):
                self.assertEqual(app.export_comments(args,requests.Session(),root,'audit',[URL]),4)
            records,complete=app.load_comment_cache(root/'.comments.json','audit')
            self.assertEqual(records[0]['text'],'old')
            self.assertEqual(complete,set())

    def test_incomplete_timeline_nodes_do_not_replace_browser_post_fetch(self):
        self.assertFalse(app.media_payload_complete({'media_type':8,'carousel_media':[{'media_type':2,'image_versions2':{'candidates':[{'url':'https://cdn.example/cover.jpg'}]}}]},True))
        self.assertTrue(app.media_payload_complete({'media_type':1,'image_versions2':{'candidates':[{'url':'https://cdn.example/photo.jpg'}]}},False))

    def test_same_user_id_is_aggregated_across_renamed_accounts(self):
        comments=[app.build_comment_record({'pk':str(i),'user':{'pk':'100','username':name}},'A') for i,name in enumerate(['old_name','new_name'])]
        stats=app.aggregate_comment_stats(comments,1)
        self.assertEqual(stats['unique_commenters'],1)
        self.assertEqual(stats['participants'][0]['count'],2)

    def test_rest_and_comments_reject_http_200_errors_and_unknown_shapes(self):
        for payload in [{'status':'fail','message':'login_required'}, {}, [], {'items':None,'comments':None}]:
            for func,args in [(app.fetch_user_timeline_rest,('123',None)),(app.fetch_comments_page,('123',None,0))]:
                with self.subTest(payload=payload,func=func.__name__), patch.object(requests.Session,'get',return_value=response(payload)), patch.object(app,'_safe_sleep'):
                    with self.assertRaises(app.ExportError):
                        func(requests.Session(),*args)

    def test_failed_api_response_is_not_cached_as_completed_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['comments','audit','--no-open'])
            app.save_post_links(root,args,[URL],1,True)
            with patch.object(requests.Session,'get',return_value=response({'status':'fail','message':'login_required'})), patch.object(app,'_safe_sleep'), patch.object(app,'write_interaction_report'):
                self.assertEqual(app.export_comments(args,requests.Session(),root,'audit',[URL]),4)
            records,completed = app.load_comment_cache(root/'.comments.json','audit')
            self.assertEqual(completed,set())
            self.assertEqual(records,[])

    def test_comment_pages_resume_after_failure_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['comments','audit','--no-open'])
            app.save_post_links(root,args,[URL],1,True)
            first = {'pk':'1','user':{'pk':'100','username':'alice'},'text':'first'}
            second = {'pk':'2','user':{'pk':'100','username':'alice'},'text':'second'}
            with patch.object(app,'fetch_comments_page',side_effect=[([first],'next',True),requests.ConnectionError('closed')]), patch.object(app,'write_interaction_report'):
                self.assertEqual(app.export_comments(args,requests.Session(),root,'audit',[URL]),4)
            records,completed = app.load_comment_cache(root/'.comments.json','audit')
            self.assertEqual(len(records),1)
            self.assertEqual(completed,set())
            with patch.object(app,'fetch_comments_page',return_value=([first,second],None,False)) as fetch, patch.object(app,'write_interaction_report'):
                self.assertEqual(app.export_comments(args,requests.Session(),root,'audit',[URL]),0)
                self.assertEqual(fetch.call_args.args[2],'next')
            records,completed = app.load_comment_cache(root/'.comments.json','audit')
            self.assertEqual({row['comment_id'] for row in records},{'1','2'})
            self.assertEqual(completed,{'A'})

    def test_fresh_liker_counter_can_decrease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = app.parse_args(['likes','audit','--no-open'])
            app.save_post_links(root,args,[URL],1,True)
            write_json(root/'.post-likes.json',{'username':'audit','counts':{'A':5}})
            payload = {'status':'ok','users':[{'pk':str(i),'username':f'user{i}'} for i in range(3)],'user_count':3}
            with patch.object(requests.Session,'get',return_value=response(payload)) as get, patch.object(app,'_safe_sleep'), patch.object(app,'write_interaction_report'):
                self.assertEqual(app.export_likers(args,requests.Session(),root,'audit',[URL]),0)
                self.assertEqual(get.call_count,1)
            db = LikerStore(root/'.likers.sqlite','audit')
            try:
                self.assertEqual(db.state('A')['state'],'complete')
                self.assertEqual(db.state('A')['expected'],3)
            finally:
                db.close()

    def test_pins_and_same_day_posts_are_ordered_by_exact_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            links=[URL,URL.replace('/A/','/B/'),URL.replace('/A/','/C/')]
            session=requests.Session()
            app.remember_post_dates(session,[{'code':'A','taken_at':1609459200},{'code':'B','taken_at':1704067200},{'code':'C','taken_at':1704070800}])
            for flags in [[],['--after','01.01.2024']]:
                args=app.parse_args(['posts','audit','--limit','1',*flags])
                selected=app.filter_links_by_date(links,args,session,root)
                self.assertEqual(app.select_post_links(selected,args),[links[2]])

    def test_completed_history_reconnects_to_fresh_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['posts','audit'])
            app.save_post_links(root,args,[URL],1,True)
            newer=URL.replace('/A/','/B/')
            with patch.object(app,'fetch_authenticated_timeline_page',return_value=([URL],None,False)) as fetch:
                links=app.collect_post_links(requests.Session(),'audit',2,root,args,[newer],'fresh-cursor','123')
            self.assertEqual(set(links),{URL,newer})
            fetch.assert_called_once()
            self.assertTrue(app.load_post_links(root,'audit')['completed'])

    def test_changed_selection_keeps_all_archive_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['posts','audit'])
            app.save_export_state(root,args,{'username':'audit'},'', [{'shortcode':'A'},{'shortcode':'B'}],True)
            args.limit=1
            app.save_export_state(root,args,{'username':'audit'},'', [{'shortcode':'A'}],True)
            state=read_json(root/'.export-state.json')
            self.assertEqual(len(state['post_records']),1)
            self.assertEqual({row['shortcode'] for row in state['archive_records']},{'A','B'})

    def test_media_session_never_uses_instagram_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            session=requests.Session()
            session.headers['X-CSRFToken']='dummy'
            session.headers['Authorization']='Bearer dummy'
            session.cookies.set('sessionid','dummy',domain='.instagram.com')
            picture=response({},'https://cdn.example/picture.jpg')
            picture.headers['Content-Type']='image/jpeg'
            picture._content=b'photo'
            picture._content_consumed=True
            with patch.object(requests.Session,'get',return_value=picture):
                app.download_binary(session,'https://cdn.example/picture.jpg',root/'avatar')
            media=session._igdump_media_session
            request=media.prepare_request(requests.Request('GET','https://cdn.example/picture.jpg'))
            self.assertNotIn('X-CSRFToken',request.headers)
            self.assertNotIn('Authorization',request.headers)
            self.assertNotIn('Cookie',request.headers)

    def test_corrupt_json_is_reported_without_overwriting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cache.json'
            path.write_text('{broken',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Invalid JSON cache'):
                read_json(path)
            self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_export_lock_is_released_and_blocks_second_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with export_lock(root):
                with self.assertRaisesRegex(ValueError,'Another igdump process'):
                    with export_lock(root):
                        self.fail('lock was bypassed')
            with export_lock(root):
                pass

    def test_failed_csv_generation_keeps_previous_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'report.csv'
            path.write_text('previous',encoding='utf-8')
            def rows():
                yield {'name':'first'}
                raise OSError('interrupted')
            with self.assertRaises(OSError):
                write_csv(path,['name'],rows())
            self.assertEqual(path.read_text(encoding='utf-8'),'previous')
            self.assertEqual(list(path.parent.glob('*.tmp')),[])


@unittest.skipUnless(os.environ.get('IGDUMP_BROWSER_TESTS')=='1','optional browser integration')
class BrowserRegressionTests(unittest.TestCase):
    def test_html_comment_and_script_caption_does_not_break_archive(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            caption='<!--<script></script><img src=x onerror=alert(1)>'
            record={'shortcode':'A','caption':caption,'local_media_path':'missing.jpg','alt_text':'photo','date_label':'unknown','instagram_url':URL,'likes_label':'1','comments_label':'0','kind_label':'image'}
            app.generate_html({'username':'audit'},[record],'missing.jpg','test',root,9)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel=os.environ.get('IGDUMP_BROWSER_CHANNEL') or None)
                try:
                    page=browser.new_page()
                    page.goto((root/'index.html').as_uri())
                    self.assertEqual(page.locator('.post-card').count(),1)
                    self.assertEqual(page.locator('.caption').inner_text(),caption)
                finally:
                    browser.close()
