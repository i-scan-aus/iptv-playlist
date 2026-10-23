import json, urllib.request, re
from pathlib import Path
BASE='https://iptv-org.github.io/api/'
def load(name):
    req=urllib.request.Request(BASE+name+'.json',headers={'User-Agent':'Mozilla/5.0 IPTVPlaylistBuilder/1.0'})
    with urllib.request.urlopen(req,timeout=90) as r: return json.load(r)
channels={c['id']:c for c in load('channels') if c.get('country') in ('AU','GB','US') and not c.get('closed') and not c.get('is_nsfw')}
feeds=load('feeds')
english={(f.get('channel'),f.get('id')) for f in feeds if 'eng' in (f.get('languages') or [])}
english_channels={c for c,f in english}
streams=load('streams')
exclude={'religious','shopping','shop','teleshopping'}
pattern=re.compile(r'\b(church|ministr(?:y|ies)|worship|gospel|christian|islamic|quran|bible|god tv|jesus|shopping|shop tv|shop hq|qvc|hsn|jewelry television)\b',re.I)
groups={'AU':'Australia','GB':'UK','US':'USA'}
rows=[]; seen=set()
for s in streams:
    cid=s.get('channel'); c=channels.get(cid)
    if not c: continue
    if exclude.intersection(set(c.get('categories') or [])): continue
    if pattern.search(c.get('name','')): continue
    feed=s.get('feed')
    if feed is not None:
        if (cid,feed) not in english: continue
    elif cid not in english_channels: continue
    url=s.get('url','').strip()
    if not url.startswith(('https://','http://')): continue
    key=(cid,url)
    if key in seen: continue
    seen.add(key)
    rows.append((['AU','GB','US'].index(c['country']),c['name'].casefold(),c['name'],groups[c['country']],cid,url))
rows.sort()
if not rows: raise SystemExit('No streams found; keeping previous published playlist.')
lines=['#EXTM3U']
for _,__,name,group,cid,url in rows:
    name=name.replace('"',"'").replace('\n',' ')
    lines.append(f'#EXTINF:-1 tvg-id="{cid}" group-title="{group}",{name}')
    lines.append(url)
Path('playlist.m3u').write_text('\n'.join(lines)+'\n',encoding='utf-8')
Path('index.html').write_text('<!doctype html><html><head><meta charset="utf-8"><title>IPTV Playlist</title></head><body><h1>IPTV Playlist</h1><a href="playlist.m3u">Download playlist.m3u</a></body></html>\n',encoding='utf-8')
print(f'Created {len(rows)} stream entries')
