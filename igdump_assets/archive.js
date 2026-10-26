(() => {
  'use strict';
  const payload = JSON.parse(document.getElementById('post-data').textContent);
  const allPosts = (payload.posts || []).map((post, index) => ({...post, originalIndex:index}));
  const feed = document.getElementById('feed'), shell = document.querySelector('.feed-shell');
  const sentinel = document.getElementById('sentinel'), loader = document.getElementById('loader');
  const progress = document.getElementById('render-progress'), empty = document.getElementById('empty');
  const order = document.getElementById('archive-order');
  const viewButtons = [...document.querySelectorAll('[data-archive-view]')];
  const storageKey = `igdump:${payload.profile.username}:archive`;
  let preference = {};
  try { preference = JSON.parse(localStorage.getItem(storageKey)) || {}; } catch (_) {}
  let view = preference.view === 'grid' ? 'grid' : 'feed';
  order.value = ['oldest','newest'].includes(preference.order) ? preference.order : payload.initial_order || 'newest';
  let posts = [], photos = [], entries = [], cursor = 0, photoIndices = new Map();
  const stamp = post => {
    if (Number.isFinite(post.taken_at)) return post.taken_at;
    const match = /^(\d{2})\.(\d{2})\.(\d{4})(?: (\d{2}):(\d{2}))?$/.exec(post.date_label || '');
    return match ? new Date(+match[3],+match[2]-1,+match[1],+(match[4]||0),+(match[5]||0)).getTime()/1000 : null;
  };
  const dateText = post => post.date_label && post.date_label !== 'Date unknown' ? post.date_label : stamp(post) !== null ? new Date(stamp(post)*1000).toLocaleString() : 'Date unknown';
  const make = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const mediaFor = post => (post.media_paths || [post.local_media_path]).filter(Boolean);
  const lightbox = document.getElementById('lightbox'), lightboxImg = document.getElementById('lightbox-img');
  const lightboxVideo = document.getElementById('lightbox-video'), caption = document.getElementById('lightbox-caption');
  let lightboxIndex = 0, returnFocus;
  const showLightboxItem = () => {
    const item = photos[lightboxIndex];
    if (!item) return;
    const video = item.type === 'video';
    lightboxImg.hidden = video; lightboxVideo.hidden = !video;
    lightboxVideo.pause(); lightboxVideo.removeAttribute('src');
    if (video) lightboxVideo.src = item.path;
    else { lightboxImg.src = item.path; lightboxImg.alt = item.post.alt_text || item.post.caption || ''; }
    caption.textContent = `${dateText(item.post)} · ${item.post.shortcode} · ${item.slide+1}/${mediaFor(item.post).length} · ${lightboxIndex+1}/${photos.length}`;
    document.getElementById('lightbox-prev').disabled = lightboxIndex === 0;
    document.getElementById('lightbox-next').disabled = lightboxIndex === photos.length-1;
  };
  const openLightbox = index => {
    returnFocus = document.activeElement; lightboxIndex = index;
    showLightboxItem(); lightbox.hidden=false;lightbox.classList.add('open'); lightbox.setAttribute('aria-modal','true');
    document.body.style.overflow = 'hidden'; document.getElementById('lightbox-close').focus();
  };
  const closeLightbox = () => {
    lightbox.classList.remove('open'); lightbox.hidden=true;lightbox.removeAttribute('aria-modal');
    lightboxImg.removeAttribute('src'); lightboxVideo.pause(); lightboxVideo.removeAttribute('src');
    document.body.style.overflow = ''; returnFocus?.focus();
  };
  const moveLightbox = delta => {lightboxIndex = Math.min(photos.length-1,Math.max(0,lightboxIndex+delta));showLightboxItem();};
  document.getElementById('lightbox-close').addEventListener('click',closeLightbox);
  document.getElementById('lightbox-prev').addEventListener('click',()=>moveLightbox(-1));
  document.getElementById('lightbox-next').addEventListener('click',()=>moveLightbox(1));
  lightbox.addEventListener('click',event=>{if(event.target===lightbox)closeLightbox();});
  document.addEventListener('keydown',event=>{
    if(!lightbox.classList.contains('open'))return;
    if(event.key==='Escape')closeLightbox();
    else if(event.key==='ArrowLeft')moveLightbox(-1);
    else if(event.key==='ArrowRight')moveLightbox(1);
    else if(event.key==='Tab') {
      const controls=[...lightbox.querySelectorAll('button:not(:disabled),video:not([hidden])')];
      if(event.shiftKey && document.activeElement===controls[0]) {controls.at(-1).focus();event.preventDefault();}
      else if(!event.shiftKey && document.activeElement===controls.at(-1)) {controls[0].focus();event.preventDefault();}
    }
  });
  const mediaElement = item => {
    const media = make(item.type==='video'?'video':'img',undefined,'post-media');
    media.src = item.path;
    if(item.type==='video'){media.controls=true;media.preload='metadata';media.playsInline=true;}
    else {media.alt=item.post.alt_text||item.post.caption||`Post ${item.post.shortcode}`;media.loading='lazy';}
    return media;
  };
  const itemIndex = (post, slide) => photoIndices.get(`${post.shortcode}/${slide}`);
  const cardFor = post => {
    const card=make('article',undefined,'post-card');card.dataset.shortcode=post.shortcode;
    const head=make('div',undefined,'post-head'), avatar=make('img',undefined,'avatar');
    avatar.src=payload.profile.avatar_src;avatar.alt='';
    const owner=make('div',undefined,'post-owner');owner.append(make('strong',`@${payload.profile.username}`),make('span',dateText(post)));
    const source=make('a','Instagram','post-link');source.href=post.instagram_url||`https://www.instagram.com/p/${post.shortcode}/`;source.target='_blank';source.rel='noreferrer';
    head.append(avatar,owner,source);card.append(head);
    const paths=mediaFor(post),carousel=make('div',undefined,'carousel');
    carousel.setAttribute('role','group');carousel.setAttribute('aria-label',`Post ${post.shortcode}, ${paths.length} media`);carousel.tabIndex=0;
    paths.forEach((path,slide)=>{
      const item=photos[itemIndex(post,slide)],wrap=make('div',undefined,'carousel-slide');wrap.hidden=slide!==0;
      const media=mediaElement(item);
      if(item.type!=='video'){const button=make('button',undefined,'media-wrap');button.type='button';button.style.cssText='padding:0;border:0;display:block;width:100%;background:transparent';button.setAttribute('aria-label',`Open photo ${slide+1} of ${paths.length}`);button.append(media);button.addEventListener('click',()=>openLightbox(itemIndex(post,slide)));wrap.append(button);}
      else wrap.append(media);
      carousel.append(wrap);
    });
    card.append(carousel);
    if(paths.length>1){
      const controls=make('div',undefined,'carousel-controls'),prev=make('button','←'),next=make('button','→'),count=make('span',undefined,'carousel-count');
      prev.type=next.type='button';prev.setAttribute('aria-label','Previous carousel photo');next.setAttribute('aria-label','Next carousel photo');count.setAttribute('aria-live','polite');
      let active=0;
      const show=delta=>{carousel.children[active].querySelector('video')?.pause();active=Math.min(paths.length-1,Math.max(0,active+delta));[...carousel.children].forEach((child,i)=>child.hidden=i!==active);count.textContent=`${active+1} / ${paths.length} · carousel`;prev.disabled=active===0;next.disabled=active===paths.length-1;};
      prev.addEventListener('click',()=>show(-1));next.addEventListener('click',()=>show(1));
      carousel.addEventListener('keydown',event=>{if(event.target.tagName==='VIDEO')return;if(event.key==='ArrowLeft'){show(-1);event.preventDefault();}if(event.key==='ArrowRight'){show(1);event.preventDefault();}});
      let start,suppressClick=false;carousel.addEventListener('pointerdown',event=>{suppressClick=false;start={x:event.clientX,y:event.clientY};});
      carousel.addEventListener('pointerup',event=>{if(start&&Math.abs(event.clientX-start.x)>50&&Math.abs(event.clientY-start.y)<40){show(event.clientX<start.x?1:-1);suppressClick=true;}start=null;});
      carousel.addEventListener('click',event=>{if(suppressClick){event.preventDefault();event.stopPropagation();suppressClick=false;}},true);
      controls.append(prev,count,next);card.append(controls);show(0);
    }
    const body=make('div',undefined,'post-body'),stats=make('div',undefined,'post-stats');
    [post.likes_label,post.comments_label,post.kind_label].filter(Boolean).forEach(text=>stats.append(make('span',text)));
    body.append(stats,make('div',post.caption||'No caption',post.caption?'caption':'caption caption-empty'));card.append(body);return card;
  };
  const gridFor = item => {
    const figure=make('figure',undefined,'grid-photo');figure.dataset.shortcode=item.post.shortcode;figure.dataset.slide=String(item.slide);
    const button=make('button');button.type='button';button.title=`${dateText(item.post)} · ${item.post.shortcode} · ${item.slide+1}/${mediaFor(item.post).length}`;button.setAttribute('aria-label',button.title);
    const media=mediaElement(item);if(item.type==='video'){media.controls=false;media.muted=true;}button.append(media);button.addEventListener('click',()=>openLightbox(itemIndex(item.post,item.slide)));
    figure.append(button,make('figcaption',dateText(item.post),'grid-date'));
    const total=mediaFor(item.post).length;
    if(total>1||item.type==='video')figure.append(make('span',`${item.type==='video'?'Video · ':''}${item.slide+1}/${total}`,'grid-position'));
    return figure;
  };
  const renderMore = () => {
    const size=view==='grid'?Math.max(36,payload.batch_size||9):Math.max(1,payload.batch_size||9);
    entries.slice(cursor,cursor+size).forEach(entry=>feed.append(view==='grid'?gridFor(entry):cardFor(entry)));
    cursor=Math.min(entries.length,cursor+size);progress.textContent=entries.length?`Showing ${cursor} of ${entries.length} ${view==='grid'?'media':'posts'}`:'';
    empty.hidden=entries.length>0;loader.classList.toggle('hidden',cursor>=entries.length);
    if(cursor>=entries.length)observer.disconnect();
  };
  const observer=new IntersectionObserver(items=>{if(items.some(item=>item.isIntersecting))renderMore();},{rootMargin:'220px 0px'});
  const reset = () => {
    posts=[...allPosts].sort((a,b)=>{const x=stamp(a),y=stamp(b);if(x===null&&y===null)return a.originalIndex-b.originalIndex;if(x===null)return 1;if(y===null)return -1;return (order.value==='oldest'?x-y:y-x)||a.originalIndex-b.originalIndex;});
    photos=posts.flatMap(post=>mediaFor(post).map((path,slide)=>({post,path,slide,type:post.media_types?.[slide]||'image'})));
    photoIndices=new Map(photos.map((item,index)=>[`${item.post.shortcode}/${item.slide}`,index]));
    entries=view==='grid'?photos:posts;cursor=0;observer.disconnect();feed.replaceChildren();feed.classList.toggle('photo-grid',view==='grid');shell.classList.toggle('grid-view',view==='grid');
    const selection=document.getElementById('archive-selection-label');
    if(selection)selection.textContent=(payload.selection_label||selection.textContent).replace(/(?:oldest|newest) first/gi,`${order.value} first`);
    viewButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.archiveView===view)));
    renderMore();if(cursor<entries.length)observer.observe(sentinel);
    try{localStorage.setItem(storageKey,JSON.stringify({view,order:order.value}));}catch(_){}
  };
  viewButtons.forEach(button=>button.addEventListener('click',()=>{view=button.dataset.archiveView;reset();}));order.addEventListener('change',reset);reset();
})();
