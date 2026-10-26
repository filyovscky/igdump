"""Highlights from the same authenticated pages a user can open in Chrome."""
import json
import logging
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlparse
from igdump_storage import read_json, write_json, atomic_write
from igdump_csv import write_csv

logger=logging.getLogger('igdump')


def find_reel(payload, highlight_id, username):
    stack=[payload]
    while stack:
        node=stack.pop()
        if isinstance(node,dict):
            if str(node.get('id'))==highlight_id and isinstance(node.get('items'),list) and str((node.get('user') or {}).get('username','')).casefold()==username.casefold():
                return node
            stack.extend(node.values())
        elif isinstance(node,list):stack.extend(node)
        elif isinstance(node,str) and node.lstrip().startswith(('{','[')):
            try:stack.append(json.loads(node))
            except (ValueError,RecursionError):pass
    return None


def reel_from_html(html, highlight_id, username):
    for script in re.findall(r'<script[^>]*>(.*?)</script>',html,re.S):
        try:payload=json.loads(script)
        except (ValueError,RecursionError):continue
        reel=find_reel(payload,highlight_id,username)
        if reel is not None:return reel
    return None


def read_reel(browser, highlight_id, username):
    number=highlight_id.removeprefix('highlight:')
    if not number.isdecimal():raise ValueError('Invalid highlight ID')
    page=browser.require_context().new_page()
    found=[]
    limited=[]
    def capture(response):
        if urlparse(response.url).hostname not in ('www.instagram.com','instagram.com'):return
        if 'graphql' not in response.url and '/reels' not in response.url:return
        if response.status==429:limited.append(True);return
        if response.status!=200:return
        try:
            reel=find_reel(response.json(),highlight_id,username)
            if reel is not None:found.append(reel)
        except Exception:pass
    page.on('response',capture)
    try:
        page.goto(f'https://www.instagram.com/stories/highlights/{number}/',wait_until='domcontentloaded',timeout=30000)
        for _ in range(8):
            if limited:raise RuntimeError('Instagram rate limited highlight access (429).')
            if any(part in page.url for part in ('/accounts/login','/auth_platform/','/challenge/')):
                raise RuntimeError('Instagram denied access to highlights; login or verification is required.')
            if found:return found[-1]
            reel=reel_from_html(page.content(),highlight_id,username)
            if reel is not None:return reel
            page.wait_for_timeout(500)
        raise RuntimeError('Instagram did not return the requested highlight items. The album remains unfinished.')
    finally:
        page.close()


def story_date(stamp):
    try:return datetime.fromtimestamp(stamp,UTC).astimezone().isoformat(timespec='minutes')
    except (TypeError,ValueError,OverflowError,OSError):return None


def media_url(item, videos):
    versions=item.get('video_versions') if videos else None
    versions=versions or (item.get('image_versions2') or {}).get('candidates') or []
    return versions[0].get('url') if versions else None


def render_highlights(root, cache):
    albums=cache.get('albums',[])
    nav=''.join(f'<a href="#album-{escape(album["id"].split(":")[-1])}">{escape(album.get("title") or "Untitled")}</a>' for album in albums)
    sections=[];csv_rows=[]
    for album in albums:
        items=[]
        for item in album.get('items',[]):
            csv_rows.append({'album':album.get('title',''),'album_id':album['id'],**item})
            local=item.get('local_media_path');date=item.get('published_at') or 'Date unknown'
            if local and (root/local).is_file():
                src=escape(local,quote=True)
                media=f'<video controls preload="none" src="{src}" playsinline></video>' if item.get('downloaded_type')=='video' else f'<a href="{src}"><img src="{src}" loading="lazy" alt="{escape(date,quote=True)}"></a>'
            else:media='<p>Media not saved</p>'
            note=' · Video cover' if item.get('media_type')==2 and item.get('downloaded_type')!='video' else ''
            items.append(f'<figure>{media}<figcaption>{escape(date)}{note}</figcaption></figure>')
        cover=album.get('cover_src');cover_html=f'<img class="cover" src="{escape(cover,quote=True)}" alt="">' if cover and (root/cover).is_file() else ''
        errors=''.join(f'<li>{escape(str(error))}</li>' for error in album.get('errors',[]))
        sections.append(f'<section id="album-{escape(album["id"].split(":")[-1])}"><h2>{cover_html}{escape(album.get("title") or "Untitled")}</h2><p>{len(album.get("items",[]))} items · {escape(album.get("state","unfinished"))} · <a href="{escape(album["source_url"],quote=True)}">Instagram</a></p><ul>{errors}</ul><div class="stories">{"".join(items)}</div></section>')
    reason=cache.get('reason') or ''
    content=('<p>'+escape(reason)+'</p>' if reason else '')+(''.join(sections) or '<p>No saved highlights.</p>')
    template=(Path(__file__).parent/'igdump_assets'/'highlights.html').read_text(encoding='utf-8')
    status=('Selected albums complete' if cache.get('completed') else 'Partial or not collected')+' · '+cache.get('scope','saved albums')
    reports=''.join(f'<a href="{filename}">{label}</a>' for filename,label in [('index.html','Posts'),('full.html','Full export'),('summary.html','Text report')] if (root/filename).exists())+'<a href="highlights.csv">CSV</a>'
    html=Template(template).substitute(title=escape('Highlights — @'+cache['username']),status=escape(status),navigation=nav,content=content,reports=reports)
    atomic_write(root/'highlights.html',html)
    write_csv(root/'highlights.csv',['album','album_id','story_id','published_at','media_type','downloaded_type','local_media_path','source_url'],csv_rows)


def export_highlights(root, username, nodes, browser, download, videos=False, refresh=False, limit=0, tray_complete=True):
    old=read_json(root/'.highlights.json') or {}
    if old.get('username')!=username:old={}
    previous={album['id']:album for album in [*old.get('archive_albums',[]),*old.get('albums',[])]}
    selected=nodes[:limit] if limit else nodes
    cache={'version':1,'username':username,'observed_at':datetime.now(UTC).isoformat(),'completed':False,'albums':[],'limit':limit,'available_albums':len(nodes),'tray_complete':tray_complete,'scope':f'first {limit} of {len(nodes)} received albums' if limit else 'all received albums'}
    def checkpoint():
        archive={**previous,**{album['id']:album for album in cache['albums']}}
        cache['archive_albums']=list(archive.values())
        write_json(root/'.highlights.json',cache)
    failure=False
    for index,node in enumerate(selected,1):
        highlight_id=node.get('id','')
        if not re.fullmatch(r'highlight:\d+',highlight_id):raise ValueError('Instagram returned an invalid highlight ID')
        number=highlight_id.split(':')[1]
        album=previous.get(highlight_id)
        if album and not refresh and album.get('state')=='complete' and album.get('download_videos')==videos and all(item.get('local_media_path') and (root/item['local_media_path']).is_file() for item in album.get('items',[])):
            cache['albums'].append({**album,'title':node.get('title') or album.get('title','')})
            checkpoint()
            continue
        album={'id':highlight_id,'title':node.get('title') or '', 'source_url':f'https://www.instagram.com/stories/highlights/{number}/','items':[],'errors':[],'state':'collecting','download_videos':videos}
        cache['albums'].append(album)
        logger.info('Highlights %s/%s: %s',index,len(selected),album['title'])
        checkpoint()
        try:
            reel=read_reel(browser,highlight_id,username)
            if not reel['items']:raise RuntimeError('Instagram returned no accessible items for this album.')
            cover_url=(((node.get('cover_media') or {}).get('cropped_image_version') or {}).get('url'))
            if cover_url:
                try:
                    file=download(cover_url,root/'highlights'/'covers'/number)
                    album['cover_src']=file.relative_to(root).as_posix()
                except Exception as error:
                    if '429' in str(error):raise
                    logger.warning('Highlight cover unavailable: %s',error)
            for position,item in enumerate(reel['items'],1):
                identity=str(item.get('pk') or item.get('id') or '')
                if not identity:
                    album['errors'].append(f'Item {position}: no story ID');failure=True;continue
                path_id=re.sub(r'[^A-Za-z0-9_-]','_',identity)
                url=media_url(item,videos)
                row={'story_id':identity,'published_at':story_date(item.get('taken_at')),'media_type':item.get('media_type'), 'source_url':album['source_url'],'local_media_path':None,'downloaded_type':None}
                album['items'].append(row)
                try:
                    if videos and item.get('media_type')==2 and not item.get('video_versions'):
                        failure=True;album['errors'].append(f'{identity}: video URL unavailable; only cover can be saved')
                    if not url:raise RuntimeError('No media URL returned')
                    video=bool(videos and item.get('video_versions'))
                    file=download(url,root/'highlights'/'media'/(path_id+('-video' if video else '-cover')))
                    row['local_media_path']=file.relative_to(root).as_posix()
                    row['downloaded_type']='video' if video else 'image'
                except Exception as error:
                    failure=True;album['errors'].append(f'{identity}: {error}')
                    if '429' in str(error) or 'denied access' in str(error):raise
                if position%10==0:checkpoint()
            album['state']='partial' if album['errors'] else 'complete'
        except Exception as error:
            failure=True;album['state']='failed';album['errors'].append(str(error))
            collected={item['story_id'] for item in album['items']}
            album['items'].extend(item for item in previous.get(highlight_id,{}).get('items',[]) if item['story_id'] not in collected)
            checkpoint();render_highlights(root,cache)
            if '429' in str(error) or 'denied access' in str(error):raise
        checkpoint();render_highlights(root,cache)
    cache['completed']=bool(not failure and (tray_complete or limit and len(selected)>=limit))
    checkpoint();render_highlights(root,cache)
    return 0 if cache['completed'] else 4


def rebuild_highlights(root,username,limit=0):
    cache=read_json(root/'.highlights.json') or {}
    if cache.get('username')!=username:
        cache={'username':username,'completed':False,'albums':[],'reason':'First collect online with --highlights or the highlights command.'}
    else:
        if limit:
            cache['albums']=cache.get('albums',[])[:limit]
            cache['scope']=f'first {limit} albums from cache'
        if not limit and cache.get('limit',0) and len(cache.get('albums',[]))<cache.get('available_albums',len(cache.get('albums',[]))):
            cache['completed']=False
        if any(not item.get('local_media_path') or not (root/item['local_media_path']).is_file() for album in cache.get('albums',[]) for item in album.get('items',[])):
            cache['completed']=False
    render_highlights(root,cache)
    return 0 if cache.get('completed') else 4
