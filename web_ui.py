#!/usr/bin/env python3
"""Local Broken Arrow workflow dashboard with resilient public statistics."""
from __future__ import annotations
import argparse, json, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from ba_tool import LogParser, LogWatcher, MatchAnalysis, PublicStatsClient, CaptchaRequired, match_from_dict, human_report, scan
from analysis_engine import analyze_single_match
from relationships import RelationshipDB

HTML=Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8")

class Cache:
 PREFIX_TTL={'match':604800,'player':21600,'index':21600}
 def __init__(self,path,ttl=21600):
  self.path=Path(path);self.ttl=ttl;self.lock=threading.Lock();self.d={'entries':{},'hits':0,'misses':0,'failures':0}
  try:self.d.update(json.loads(self.path.read_text(encoding='utf8')))
  except (OSError,ValueError):pass
 def get(self,k):
  with self.lock:
   x=self.d['entries'].get(k)
   if not x:self.d['misses']+=1;return None,None
   ttl=self.PREFIX_TTL.get(k.split(':',1)[0],self.ttl)
   fresh=time.time()-x.get('time',0)<=ttl
   self.d['hits' if fresh else 'misses']+=1;return x.get('value'),fresh
 def put(self,k,v):
  with self.lock:
   self.d['entries'][k]={'time':time.time(),'value':v}
   try:self.path.parent.mkdir(parents=True,exist_ok=True);self.path.write_text(json.dumps(self.d,ensure_ascii=False),encoding='utf8')
   except OSError:pass
 def fail(self):
  with self.lock:self.d['failures']+=1
 def summary(self):
  with self.lock:return {'entries':len(self.d['entries']),'hits':self.d['hits'],'misses':self.d['misses'],'failures':self.d['failures']}

class Quota:
 """Rolling 24h request budget, persisted so restarts cannot reset it."""
 def __init__(self,path,limit=300):
  self.path=Path(path);self.limit=limit;self.lock=threading.Lock();self.calls=[]
  try:self.calls=[float(t) for t in json.loads(self.path.read_text(encoding='utf8')) if float(t)>time.time()-86400]
  except (OSError,ValueError,TypeError):pass
 def _save(self):
  try:self.path.write_text(json.dumps(self.calls[-86400:]),encoding='utf8')
  except OSError:pass
 def remaining(self):
  with self.lock:
   self.calls=[t for t in self.calls if t>time.time()-86400]
   return max(0,self.limit-len(self.calls))
 def try_consume(self):
  with self.lock:
   self.calls=[t for t in self.calls if t>time.time()-86400]
   if len(self.calls)>=self.limit:return False
   # One real network attempt costs one slot, whether it succeeds or not.
   self.calls.append(time.time());self._save();return True
 def summary(self):
  with self.lock:
   used=len([t for t in self.calls if t>time.time()-86400])
   return {'used':used,'limit':self.limit,'remaining':max(0,self.limit-used)}

class ResilientClient(PublicStatsClient):
 def __init__(self,base,cache,quota=None):super().__init__(base,8);self.cache=cache;self.quota=quota;self.lock=threading.Lock();self.last=0;self.open_until=0;self.failures=0;self.last_error=None
 def status(self):
  s={'circuit_open':time.time()<self.open_until,'circuit_until':self.open_until,'consecutive_failures':self.failures,'last_error':self.last_error}
  if self.quota:s['quota']=self.quota.summary()
  return s
 def _cached(self,k,path,params):
  key=k+':'+json.dumps(params,sort_keys=True);stale,fresh=self.cache.get(key)
  if fresh:return stale
  if time.time()<self.open_until:
   if stale is not None:return stale
   raise RuntimeError('circuit_open')
  if self.quota is not None and not self.quota.try_consume():
   self.last_error='quota_exceeded'
   if stale is not None:return stale
   raise RuntimeError('daily quota exhausted / 今日配额已用完')
  for attempt in range(3):
   try:
    with self.lock:
     delay=.35-(time.time()-self.last)
     if delay>0:time.sleep(delay)
     self.last=time.time();v=super()._get(path,params)
    self.cache.put(key,v);self.failures=0;self.last_error=None;return v
   except CaptchaRequired as e:
    self.last_error=str(e);self.open_until=time.time()+600;break
   except HTTPError as e:
    self.last_error=f'HTTP {e.code}'
    if e.code==429:self.open_until=time.time()+30;break
    if e.code in (401,403):self.open_until=time.time()+300;break
    if e.code==404:break
   except (URLError,TimeoutError,OSError,ValueError,json.JSONDecodeError) as e:self.last_error=type(e).__name__
   if attempt<2:time.sleep(.5*(2**attempt))
  self.failures+=1;self.cache.fail()
  if self.failures>=5:self.open_until=time.time()+60
  if stale is not None:return stale
  raise RuntimeError(self.last_error or 'api_unavailable')
 def player_report(self,p):return self._cached('player','/api/analysis/player',{'stbid':p})
 def match_report(self,m):return self._cached('match','/api/analysis/match',{'matchid':m})
 def maggot_index(self,p):
  key='index:'+str(p);stale,fresh=self.cache.get(key)
  if fresh:return stale
  if self.quota is not None and not self.quota.try_consume():
   self.last_error='quota_exceeded';return stale
  try:
   v=super().maggot_index(p);self.cache.put(key,v);return v
  except Exception:self.cache.fail();return stale

class State:
 def __init__(self,directory,client,cache):
  self.lock=threading.Lock();self.file='';self.phase='starting / 启动中';self.last_event='';self.updated=time.time();self.match=None;self.report='';self.review=None;self.health={};self.cache=cache;self.client=client;self.local_name=None;self.relationships=RelationshipDB(Path(__file__).with_name('ba-relationships.sqlite'));self.analysis=MatchAnalysis(client,self.updated_stats)
 def annotate(self,match,party_override=None):
  data=match.jsonable(); players=data.get('players',[]); local_id=next((str(p.get('id')) for p in players if p.get('name')==self.local_name),None); rel=self.relationships.annotate_against_local(players,local_id); party=party_override or self.relationships.party_signal(players)
  for p in players:
   pid=str(p.get('id'));p['relationship']=rel.get(pid,{});p['relationship']['is_self']=(local_id is not None and pid==local_id);p['party_signal']=party
  data['local_id']=local_id;data['local_name']=self.local_name;data['party_signal']=party;return data
 def api_party(self,match):
  groups=[]; teams={}
  for p in match.players: teams.setdefault(p.team,[]).append(p)
  for team,players in teams.items():
   for i,a in enumerate(players):
    for b in players[i+1:]:
     aa={str(x.get('match_id')) for x in (match.player_stats.get(a.id) or {}).get('matches',[]) if isinstance(x,dict)};bb={str(x.get('match_id')) for x in (match.player_stats.get(b.id) or {}).get('matches',[]) if isinstance(x,dict)}; common=aa&bb
     if len(common)>=2:groups.append({'team':team,'players':[a.id,b.id],'shared_matches':len(common)})
  return {'groups':groups,'method':'local database + shared API history'}
 def updated_stats(self,m):
  with self.lock:
   self.match=self.annotate(m,self.api_party(m));human=[p for p in m.players if not p.id.startswith('-')];terminal={'ready','provisional','insufficient_data','api_error'};done=sum((m.player_stats.get(p.id) or {}).get('status') in terminal for p in human);self.phase=('player scores ready / 玩家评分已就绪' if done==len(human) else f'player scoring {done}/{len(human)} / 玩家评分');self.updated=time.time()
 def event(self,k,d):
  if k=='parser_health':
   with self.lock:self.health=d.get('health',{});self.updated=time.time()
   return
  if k=='local_name':
   with self.lock:self.local_name=d.get('name');self.updated=time.time()
   return
  with self.lock:
   self.last_event=k;self.updated=time.time();self.phase={'match_start':'match started / 对局开始','roster':'loading player scores / 获取玩家评分','match_end':'match finished / 对局结束'}.get(k,self.phase)
   if k in ('match_start','roster') and d.get('match'):
    self.match=self.annotate(match_from_dict(d['match']));self.file=d['match'].get('source_file') or self.file
  if k=='roster' and d.get('match'):self.analysis.on_roster(match_from_dict(d['match']))
  if k=='match_end' and d.get('match'):
   m=match_from_dict(d['match']);self.analysis.finish(m);self.relationships.add_match(m.jsonable())
   with self.lock:self.match=self.annotate(m,self.api_party(m));self.report=human_report(m);self.phase='match finished / 对局结束';self.updated=time.time()
   if self.client and m.fid:threading.Thread(target=self.fetch_review,args=(m.fid,),daemon=True).start()
 def fetch_review(self,fid):
  with self.lock:self.review={'status':'loading','match_id':str(fid)};self.updated=time.time()
  for attempt in range(3):
   try:
    r=analyze_single_match(self.client.match_report(fid));r['status']='ready'
    with self.lock:self.review=r;self.updated=time.time()
    return
   except Exception as e:
    if attempt==2:
     r={'status':'unavailable','match_id':str(fid),'reason':type(e).__name__}
     with self.lock:self.review=r;self.updated=time.time()
    else: time.sleep(2 ** attempt)
 def json(self):
  with self.lock:return {'connected':bool(self.file),'file':self.file,'phase':self.phase,'last_event':self.last_event,'updated':self.updated,'match':self.match,'report':self.report,'battle_review':self.review,'stats_enabled':bool(self.client),'api_status':self.client.status() if self.client else {'offline':True},'cache':self.cache.summary(),'parser_health':self.health,'blacklist':self.relationships.list_blacklist()}

class Server(ThreadingHTTPServer):
 # Windows SO_REUSEADDR allows a second process to bind a taken port without error;
 # disabling it is what makes the port-fallback loop actually observe conflicts.
 allow_reuse_address = False

class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  try:
   p=urlparse(self.path).path;body=HTML.encode() if p=='/' else json.dumps(self.server.state.json(),ensure_ascii=False).encode() if p=='/api/state' else None
   if body is None:self.send_error(404);return
   self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8' if p=='/' else 'application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):pass
  except OSError as e:
   if getattr(e,'winerror',None)!=10053:raise
 def do_POST(self):
  if urlparse(self.path).path!='/api/blacklist':self.send_error(404);return
  try:
   data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));rel=self.server.state.relationships
   if data.get('op')=='remove':rel.remove_blacklist(str(data.get('id')))
   else:rel.set_blacklist(str(data.get('id')),str(data.get('note','')))
   body=b'{"ok":true}';self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  except Exception as e:self.send_error(400,str(e))
 def log_message(self,*args):pass

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--dir',type=Path,default=Path(r'D:\SteamLibrary\steamapps\common\broken_arrow\GameLogs'));ap.add_argument('--port',type=int,default=8765);ap.add_argument('--no-stats',action='store_true');ap.add_argument('--no-browser',action='store_true');ap.add_argument('--daily-limit',type=int,default=300);ap.add_argument('--cache',type=Path,default=Path('ba-api-cache.json'));a=ap.parse_args();cache=Cache(a.cache);quota=Quota(a.cache.with_name('ba-api-quota.json'),a.daily_limit);client=None if a.no_stats else ResilientClient('https://app.batrace.top',cache,quota);state=State(a.dir,client,cache);parser=LogParser(state.event);watcher=LogWatcher(a.dir,parser);threading.Thread(target=lambda:(setattr(state,'file','starting'),watcher.run()),daemon=True).start()
 server=None
 for port in range(a.port,a.port+20):
  try:server=Server(('127.0.0.1',port),Handler);break
  except OSError as e:
   if getattr(e,'winerror',None)==10048 or e.errno in (48,98):continue
   raise
 if server is None:raise SystemExit(f'ports {a.port}-{a.port+19} all busy / 端口全部被占用')
 server.state=state;url=f'http://127.0.0.1:{port}';print(f'Web UI: {url}',flush=True)
 if not a.no_browser:threading.Timer(0.8,lambda:webbrowser.open(url)).start()
 server.serve_forever()
if __name__=='__main__':main()
