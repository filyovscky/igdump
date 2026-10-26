"""Copyable Russian report built exclusively from profile and collection caches."""
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from string import Template
from igdump_storage import read_json, atomic_write, CommentStore, LikerStore
from igdump_insights import cached_details, mention_rows, location_rows


def summary_data(root: Path, username: str, links: list[str]):
    saved = read_json(root/'.profile.json') or {}
    state = read_json(root/'.export-state.json') or {}
    profile = saved.get('profile', {}) if saved.get('username') == username else {}
    if not profile and (state.get('job') or {}).get('username') == username:
        profile = state.get('profile') or {}
    codes = {row['shortcode'] for row in cached_details(root,username,links)}
    comments, completed = [], set()
    if (root/'.comments.sqlite').exists():
        store = CommentStore(root/'.comments.sqlite',username)
        try: comments, completed = store.snapshot()
        finally: store.close()
    else:
        cache = read_json(root/'.comments.json') or {}
        if cache.get('username') == username:
            comments, completed = cache.get('records',[]), set(cache.get('completed_posts',[]))
    people = {}
    for row in comments:
        if row.get('post_shortcode') not in codes: continue
        key = str(row.get('user_id') or row.get('username') or '')
        if not row.get('username'): continue
        person = people.setdefault(key,{'username':row['username'],'full_name':row.get('full_name',''),'count':0})
        person['count'] += 1
    commenters = sorted(people.values(),key=lambda row:(-row['count'],row['username']))[:10]
    likers, likes_complete = [], False
    if (root/'.likers.sqlite').exists():
        store = LikerStore(root/'.likers.sqlite',username)
        try:
            store.select(codes)
            likers = store.stats()['top_commenters'][:10]
            likes_complete = all(store.state(code)['state']=='complete' for code in codes)
        finally: store.close()
    else:
        cache = read_json(root/'.likers.json') or {}
        counts = Counter(row.get('username') for row in cache.get('records',[]) if cache.get('username')==username and row.get('post_shortcode') in codes and row.get('username'))
        likers = [{'username':name,'count':count} for name,count in counts.most_common(10)]
        likes_complete = codes <= set(cache.get('completed_posts',[])) if cache.get('username')==username else False
    posts = cached_details(root,username,links)
    mentions = sorted({row['username'] for row in mention_rows(posts)})
    locations = sorted({row['name'] for row in location_rows(posts) if row.get('name')})
    return {'profile':profile,'commenters':commenters,'likers':likers,'mentions':mentions,'locations':locations,
            'selected_posts':len(codes),'comments_complete':codes <= completed,'likes_complete':likes_complete,
            'captions_received':sum('caption' in row for row in posts),'locations_received':sum('location' in row for row in posts)}


def report_text(username, data):
    profile = data['profile']
    observed = profile.get('observed_at')
    try: date = datetime.fromisoformat(observed.replace('Z','+00:00')).astimezone().strftime('%d.%m.%Y')
    except (ValueError,AttributeError): date = datetime.now().strftime('%d.%m.%Y')
    def number(key):
        value=profile.get(key)
        return str(value) if isinstance(value,int) and not isinstance(value,bool) else 'данные не получены'
    if all(isinstance(profile.get(key),int) and not isinstance(profile[key],bool) for key in ('followers','followees','mediacount')):
        first=f'Профиль @{username} на {date} имеет {number("followers")} подписчиков, подписан на {number("followees")} профилей, {number("mediacount")} публикаций.'
    else:
        first=f'Профиль @{username} на {date}: подписчиков — {number("followers")}; подписок — {number("followees")}; публикаций — {number("mediacount")}.'
    lines=[first, f'Ссылка на профиль: https://www.instagram.com/{username}/']
    if not observed: lines.append('Дата проверки профиля не сохранена; указана дата формирования отчёта.')
    biography=profile.get('biography')
    lines.append('Биография: '+(biography if biography else 'пустая.' if isinstance(biography,str) else 'данные не получены.'))
    def person(row):
        name=row.get('full_name')
        label=f'{name} (@{row["username"]})' if name else '@'+row['username']
        return f'{label} (https://www.instagram.com/{row["username"]}/)'
    lines.append('Среди наиболее частых комментаторов фигурируют следующие профили: '+('; '.join(person(row) for row in data['commenters']) or 'не обнаружены в собранных данных')+'.')
    lines.append('Наиболее активно лайкающие: '+('; '.join(person(row) for row in data['likers']) or 'не обнаружены в собранных данных')+'.')
    lines.append('Упомянутые в публикациях профили: '+('; '.join(f'@{name} (https://www.instagram.com/{name}/)' for name in data['mentions']) or 'не обнаружены в собранных данных')+'.')
    lines.append('В профиле упоминаются следующие локации: '+('; '.join(data['locations']) or 'не обнаружены в полученных геометках')+'.')
    notes=[f'Анализ выполнен по выборке из {data["selected_posts"]} публикаций.']
    if not data['comments_complete']: notes.append('Сбор комментариев неполный или не выполнялся; рейтинг относится только к полученным данным.')
    if not data['likes_complete']: notes.append('Сбор списков лайков неполный или не выполнялся; рейтинг относится только к полученным данным.')
    if data['captions_received']<data['selected_posts']: notes.append('Тексты части публикаций не получены; список упоминаний может быть неполным.')
    if data['locations_received']<data['selected_posts']: notes.append('Геометки части публикаций не получены; отсутствие места в отчёте не доказывает отсутствие геометки.')
    notes.append('Локации перечислены по полученным геометкам. Кешированные наблюдения могут относиться к разным датам.')
    return '\n\n'.join(lines+notes)+'\n'


def generate_summary(root, username, links):
    data=summary_data(root,username,links)
    text=report_text(username,data)
    atomic_write(root/'summary.txt',text)
    template=(Path(__file__).parent/'igdump_assets'/'summary.html').read_text(encoding='utf-8')
    navigation=''.join(f'<a href="{filename}">{label}</a>' for filename,label in [('full.html','Полный экспорт'),('index.html','Публикации'),('highlights.html','Highlights')] if (root/filename).exists())+'<a href="summary.txt" download>Скачать TXT</a>'
    html=Template(template).substitute(title=escape('Текстовый отчёт — @'+username),report=escape(text),navigation=navigation)
    atomic_write(root/'summary.html',html)
    atomic_write(root/'summary.js',(Path(__file__).parent/'igdump_assets'/'summary.js').read_bytes())
    return ('Text report','summary.html',0)
