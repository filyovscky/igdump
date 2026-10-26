import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import requests
import insta_html_export as app
import igdump_highlights as highlights
from igdump_summary import summary_data, report_text, generate_summary
from igdump_storage import write_json, read_json, save_comment_cache, LikerStore

URL='https://www.instagram.com/p/A/'


class MetadataAndReportTests(unittest.TestCase):
    def test_empty_highlight_tray_is_scoped_to_numeric_target_id(self):
        for owner in ('123','999'):
            session=requests.Session();session._igdump_browser_profile_metadata={'user_id':'123'}
            response=MagicMock(url=app.IG_BASE_URL+'/api/graphql',status=200)
            response.json.return_value={'data':{'highlights':{'edges':[],'page_info':{'has_next_page':False}}}}
            response.request.all_headers.return_value={'referer':app.IG_BASE_URL+'/'}
            response.request.post_data=app.urlencode({'variables':json.dumps({'user_id':owner})})
            app.capture_browser_profile(session,'target',response)
            self.assertEqual(hasattr(session,'_igdump_highlight_nodes'),owner=='123')

    def test_html_fallback_keeps_requested_username_with_dots(self):
        session=MagicMock();response=session.get.return_value
        response.status_code=200;response.url=app.IG_BASE_URL+'/target.name/'
        response.text='<title>Name (@target.name)</title><script>{"username":"target.name","id":"12345"}</script>'
        profile,_,_,uid=app._scrape_profile_html('target.name',session)
        self.assertEqual(profile['username'],'target.name');self.assertEqual(uid,'12345')
        self.assertIsNone(profile['biography'])

    def test_profile_query_without_username_variable_supplies_full_metadata(self):
        session=requests.Session()
        response=MagicMock(url=app.IG_BASE_URL+'/api/graphql',status=200)
        response.json.return_value={'data':{'user':{'username':'target','id':'123','full_name':'Name','biography':'','follower_count':414,'following_count':244,'media_count':112}}}
        app.capture_browser_profile(session,'target',response)
        profile=session._igdump_browser_profile_metadata
        self.assertEqual([profile[key] for key in ('followers','followees','mediacount')],[414,244,112])
        self.assertEqual(profile['biography'],'')
        self.assertIn('observed_at',profile)

    def test_viewer_metadata_is_not_used_for_target(self):
        session=requests.Session()
        response=MagicMock(url=app.IG_BASE_URL+'/api/graphql',status=200)
        response.json.return_value={'data':{'user':{'username':'viewer','follower_count':999},'viewer':{'username':'target','follower_count':999}}}
        app.capture_browser_profile(session,'target',response)
        self.assertFalse(hasattr(session,'_igdump_browser_profile_metadata'))

    def test_missing_fields_do_not_erase_saved_counts_or_biography(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app.save_profile_metadata(root,'target',{'username':'target','followers':0,'biography':'Hello'})
            result=app.save_profile_metadata(root,'target',{'username':'target','followers':None,'biography':None})
            self.assertEqual(result['followers'],0)
            self.assertEqual(result['biography'],'Hello')

    def test_full_defaults_and_standalone_highlight_options(self):
        full=app.parse_args(['full','target'])
        self.assertTrue(full.highlights and full.text_report)
        self.assertFalse(app.parse_args(['full','target','--no-highlights','--no-text-report']).highlights)
        self.assertEqual(app.parse_args(['highlights','@target','--highlight-limit','1']).highlight_limit,1)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            app.parse_args(['highlights','target','--limit','1'])

    def test_summary_uses_top_ten_but_keeps_all_mentions_and_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            write_json(root/'.profile.json',{'username':'target','profile':{'followers':0,'followees':244,'mediacount':112,'biography':'','observed_at':'2026-10-26T12:00:00+00:00'}})
            write_json(root/'.post-details.json',{'username':'target','posts':{'A':{'caption':' '.join('@user'+str(i) for i in range(15)),'location':{'name':'Place','lat':1,'lng':2}}}})
            comments=[{'post_shortcode':'A','comment_id':str(i),'username':'commenter'+str(i),'full_name':'Name '+str(i)} for i in range(12)]
            save_comment_cache(root/'.comments.json','target',comments,{'A'})
            store=LikerStore(root/'.likers.sqlite','target')
            store.add_page('A',[{'pk':str(i),'username':'liker'+str(i)} for i in range(12)],12,None);store.mark('A','complete',12);store.close()
            data=summary_data(root,'target',[URL]);text=report_text('target',data)
            self.assertEqual(len(data['commenters']),10);self.assertEqual(len(data['likers']),10)
            self.assertEqual(len(data['mentions']),15)
            self.assertIn('имеет 0 подписчиков',text)
            self.assertIn('26.10.2026',text)
            self.assertIn('@user14 (https://www.instagram.com/user14/)',text)
            self.assertIn('Place',text)
            generate_summary(root,'target',[URL])
            self.assertEqual((root/'summary.txt').read_text(encoding='utf-8'),text)

    def test_unknown_counts_are_not_fabricated_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            text=report_text('target',summary_data(Path(directory),'target',[URL]))
            self.assertIn('подписчиков — данные не получены',text)
            self.assertIn('Сбор комментариев неполный',text)


class HighlightsTests(unittest.TestCase):
    def test_posts_offline_reports_missing_highlights_as_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=app.parse_args(['posts','target','--highlights','--offline','--no-open','--output-dir',directory])
            app.save_post_links(root,args,[URL],1,True)
            app.save_export_state(root,args,{'username':'target'},'', [{'shortcode':'A','local_media_path':'photo.jpg'}],True)
            (root/'photo.jpg').write_bytes(b'image')
            with patch.object(app,'generate_html'):
                self.assertEqual(app.run_posts_offline(args,root),4)

    def test_limited_refresh_keeps_unselected_album_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);reel=self.fixture()
            def download(url,base):
                base.parent.mkdir(parents=True,exist_ok=True);file=base.with_suffix('.jpg');file.write_bytes(b'image');return file
            def read(browser,highlight_id,username):return {**reel,'id':highlight_id}
            nodes=[reel,{**reel,'id':'highlight:124'}]
            with patch.object(highlights,'read_reel',side_effect=read):
                highlights.export_highlights(root,'target',nodes,None,download)
                highlights.export_highlights(root,'target',nodes,None,download,limit=1,refresh=True)
            self.assertEqual(len(read_json(root/'.highlights.json')['archive_albums']),2)

    def test_failed_refresh_preserves_previous_media_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);reel=self.fixture()
            def download(url,base):
                base.parent.mkdir(parents=True,exist_ok=True);file=base.with_suffix('.jpg');file.write_bytes(b'image');return file
            with patch.object(highlights,'read_reel',return_value=reel):highlights.export_highlights(root,'target',[reel],None,download)
            with patch.object(highlights,'read_reel',side_effect=RuntimeError('connection closed')):
                self.assertEqual(highlights.export_highlights(root,'target',[reel],None,download,refresh=True),4)
            album=read_json(root/'.highlights.json')['albums'][0]
            self.assertEqual(album['items'][0]['story_id'],'1')
            self.assertEqual(album['state'],'failed')

    def fixture(self):
        return {'id':'highlight:123','title':'Album','user':{'username':'target'},'items':[{'pk':'1','media_type':1,'taken_at':1704067200,'image_versions2':{'candidates':[{'url':'https://cdn.example/photo.jpg'}]}}]}

    def test_embedded_album_is_scoped_to_id_and_owner(self):
        reel=self.fixture()
        html='<script type="application/json">'+json.dumps({'require':[{'data':{'reels':[reel]}}]})+'</script>'
        self.assertEqual(highlights.reel_from_html(html,'highlight:123','target'),reel)
        self.assertIsNone(highlights.reel_from_html(html,'highlight:124','target'))
        self.assertIsNone(highlights.reel_from_html(html,'highlight:123','viewer'))

    def test_export_resume_offline_and_html_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);reel=self.fixture();node={**reel,'title':'<script>alert(1)</script> $content'}
            def download(url,base):
                base.parent.mkdir(parents=True,exist_ok=True);file=base.with_suffix('.jpg');file.write_bytes(b'image');return file
            with patch.object(highlights,'read_reel',return_value=reel) as fetch:
                self.assertEqual(highlights.export_highlights(root,'target',[node],None,download),0)
                self.assertEqual(highlights.export_highlights(root,'target',[node],None,download),0)
                fetch.assert_called_once()
            self.assertEqual(highlights.rebuild_highlights(root,'target'),0)
            html=(root/'highlights.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>alert(1)',html);self.assertIn('&lt;script&gt;',html)
            self.assertIn('$content',html)
            self.assertEqual(len(read_json(root/'.highlights.json')['albums'][0]['items']),1)

    def test_limited_album_cache_is_not_reported_as_all_albums(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);reel=self.fixture()
            def download(url,base):
                base.parent.mkdir(parents=True,exist_ok=True);file=base.with_suffix('.jpg');file.write_bytes(b'image');return file
            with patch.object(highlights,'read_reel',return_value=reel):
                self.assertEqual(highlights.export_highlights(root,'target',[reel,{**reel,'id':'highlight:124'}],None,download,limit=1),0)
            self.assertEqual(highlights.rebuild_highlights(root,'target'),4)
            self.assertEqual(highlights.rebuild_highlights(root,'target',limit=1),0)

    def test_download_failure_is_partial_and_retriable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);reel=self.fixture()
            with patch.object(highlights,'read_reel',return_value=reel):
                self.assertEqual(highlights.export_highlights(root,'target',[reel],None,MagicMock(side_effect=RuntimeError('network failed'))),4)
            self.assertFalse(read_json(root/'.highlights.json')['completed'])
            self.assertEqual(highlights.rebuild_highlights(root,'target'),4)


@unittest.skipUnless(os.environ.get('IGDUMP_BROWSER_TESTS')=='1','optional browser integration')
class GalleryBrowserTests(unittest.TestCase):
    def test_grid_order_carousel_navigation_and_date(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            records=[{'shortcode':'A','taken_at':1704067200,'date_label':'01.01.2024 12:00','caption':'A','local_media_path':'A1.jpg','media_paths':['A1.jpg','A2.jpg'],'instagram_url':URL,'alt_text':'A'}, {'shortcode':'B','taken_at':1704153600,'date_label':'02.01.2024 12:00','caption':'B','local_media_path':'B.jpg','instagram_url':URL.replace('/A/','/B/'),'alt_text':'B'}]
            app.generate_html({'username':'target'},records,'missing.jpg','Newest first',root,9)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel=os.environ.get('IGDUMP_BROWSER_CHANNEL') or None)
                try:
                    page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto((root/'index.html').as_uri())
                    self.assertEqual(page.locator('.post-card').first.get_attribute('data-shortcode'),'B')
                    page.get_by_role('button',name='Next carousel photo').click()
                    self.assertIn('2 / 2',page.locator('.carousel-count').inner_text())
                    self.assertEqual(page.locator('.carousel').last.evaluate('node=>getComputedStyle(node).overflowX'),'hidden')
                    page.get_by_role('button',name='Photo grid',exact=True).click()
                    self.assertEqual(page.locator('.grid-photo').count(),3)
                    self.assertEqual(page.locator('.grid-photo').evaluate_all("nodes=>nodes.map(node=>node.dataset.shortcode+':'+node.dataset.slide)"),['B:0','A:0','A:1'])
                    page.locator('#archive-order').select_option('oldest')
                    self.assertIn('oldest first',page.locator('#archive-selection-label').inner_text())
                    self.assertEqual(page.locator('.grid-photo').evaluate_all("nodes=>nodes.map(node=>node.dataset.shortcode+':'+node.dataset.slide)"),['A:0','A:1','B:0'])
                    self.assertIn('01.01.2024',page.locator('.grid-photo button').first.get_attribute('title'))
                    page.locator('.grid-photo').first.hover()
                    self.assertEqual(page.locator('.grid-date').first.evaluate('node=>getComputedStyle(node).opacity'),'1')
                    page.locator('.grid-photo button').first.click()
                    self.assertTrue(page.locator('#lightbox').is_visible())
                    page.keyboard.press('Escape')
                    self.assertFalse(page.locator('#lightbox').is_visible())
                    page.set_viewport_size({'width':390,'height':844})
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    self.assertEqual(errors,[])
                finally:browser.close()

    def test_report_copy_uses_editable_plain_text(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);generate_summary(root,'target',[URL])
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel=os.environ.get('IGDUMP_BROWSER_CHANNEL') or None)
                try:
                    context=browser.new_context();page=context.new_page()
                    page.goto((root/'summary.html').as_uri())
                    page.locator('#text-report').fill('Edited report')
                    page.evaluate("navigator.clipboard.writeText=async text=>{window.copiedReport=text}")
                    page.get_by_role('button',name='Скопировать отчёт').click()
                    self.assertEqual(page.evaluate('window.copiedReport'),'Edited report')
                    self.assertIn('скопирован',page.locator('#copy-status').inner_text())
                finally:browser.close()
