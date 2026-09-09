#!/usr/bin/env python3
"""Local Broken Arrow workflow dashboard with resilient public statistics."""
from __future__ import annotations
import argparse, hashlib, json, os, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from ba_tool import LogParser, LogWatcher, MatchAnalysis, match_from_dict, human_report, scan, find_gamelogs, DEFAULT_GAMELOGS, MATCH_RETRY_DELAYS, Cache, Quota, ResilientClient
from analysis_engine import analyze_single_match
from relationships import RelationshipDB

HTML=Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8")
try:BUILD=os.environ.get('BA_BUILD_ID') or hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:8]
except OSError:BUILD='dev'

class State:
 def __init__(self,directory,client,cache,rel_db=None):
  self.lock=threading.Lock();self.file='';self.phase='starting / 启动中';self.last_event='';self.updated=time.time();self.match=None;self.report='';self.review=None;self.health={};self.cache=cache;self.client=client;self.local_name=None;self.local_id=None;self.ban_alerts=[];self.relationships=RelationshipDB(rel_db or Path(__file__).with_name('ba-relationships.sqlite'));self.analysis=MatchAnalysis(client,self.updated_stats)
 def annotate(self,match,party_override=None):
  data=match.jsonable(); players=data.get('players',[]); local_id=next((str(p.get('id')) for p in players if p.get('name')==self.local_name),None)
  if local_id:self.local_id=local_id
  rel=self.relationships.annotate_against_local(players,local_id); party=party_override or self.relationships.party_signal(players)
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
   if self.client and m.fid:
    lid=next((p.id for p in m.players if p.name==self.local_name),None)
    dlt=((m.player_stats or {}).get(lid) or {}).get('elo_delta')
    if isinstance(dlt,(int,float)) and abs(dlt)>=0.01:self.relationships.record_result(str(m.fid),dlt>0)
   with self.lock:self.match=self.annotate(m,self.api_party(m));self.report=human_report(m);self.phase='match finished / 对局结束';self.updated=time.time()
   if self.client and m.fid:threading.Thread(target=self.fetch_review,args=(m.fid,),daemon=True).start()
 def fetch_review(self,fid,retries=0):
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
     if self.client and retries<6:
      delay=max(MATCH_RETRY_DELAYS[min(retries,len(MATCH_RETRY_DELAYS)-1)],float(getattr(self.client,'open_until',0))-time.time()+5.0,0.0)
      timer=threading.Timer(delay,self.fetch_review,args=(fid,retries+1));timer.daemon=True;timer.start()
    else: time.sleep(2 ** attempt)
 def check_bans(self):
  raw=self.client._get('/api/leaderboard/ban',{'limit':1000})
  alerts=self.relationships.apply_ban_snapshot((raw or {}).get('leaderboard') or [])
  if alerts:
   with self.lock:self.ban_alerts=alerts;self.updated=time.time()
 def ban_loop(self):
  if not self.client:return
  time.sleep(10)
  while True:
   try:self.check_bans()
   except (HTTPError,URLError,TimeoutError,OSError,ValueError,RuntimeError,TypeError,KeyError):pass
   time.sleep(3600)
 def json(self):
  with self.lock:return {'connected':bool(self.file),'file':self.file,'phase':self.phase,'last_event':self.last_event,'updated':self.updated,'match':self.match,'report':self.report,'battle_review':self.review,'stats_enabled':bool(self.client),'api_status':self.client.status() if self.client else {'offline':True},'cache':self.cache.summary(),'parser_health':self.health,'blacklist':self.relationships.list_blacklist(),'ban_alerts':self.ban_alerts,'history_rev':self.relationships.history_rev(),'build':BUILD}

def _urlfile() -> Path:
 return Path(os.environ.get('BA_URL_FILE') or Path(__file__).with_name('ba-webui.url'))

class Server(ThreadingHTTPServer):
 # Windows SO_REUSEADDR allows a second process to bind a taken port without error;
 # disabling it is what makes the port-fallback loop actually observe conflicts.
 allow_reuse_address = False

class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  try:
   u=urlparse(self.path);p=u.path
   if p=='/':body=HTML.encode()
   elif p=='/api/state':body=json.dumps(self.server.state.json(),ensure_ascii=False).encode()
   elif p=='/api/investigate':
    pid=(parse_qs(u.query).get('id') or [None])[0]
    if pid is None:self.send_error(400,'missing id');return
    st=self.server.state;body=json.dumps(st.relationships.investigate(pid,st.local_id),ensure_ascii=False).encode()
   elif p=='/api/history':body=json.dumps(self.server.state.relationships.list_history(self.server.state.local_id),ensure_ascii=False).encode()
   else:body=None
   if body is None:self.send_error(404);return
   self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8' if p=='/' else 'application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):pass
  except OSError as e:
   if getattr(e,'winerror',None)!=10053:raise
 def do_POST(self):
  if (self.headers.get('Content-Type') or '').split(';')[0].strip()!='application/json':self.send_error(403);return
  p=urlparse(self.path).path
  if p=='/api/shutdown':
   body=b'{"ok":true}';self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
   threading.Thread(target=self.server.shutdown,daemon=True).start();return
  if p!='/api/blacklist':self.send_error(404);return
  try:
   data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));rel=self.server.state.relationships
   if data.get('op')=='remove':rel.remove_blacklist(str(data.get('id')))
   else:rel.set_blacklist(str(data.get('id')),str(data.get('note','')))
   body=b'{"ok":true}';self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  except Exception as e:self.send_error(400,str(e))
 def log_message(self,*args):pass

def main():
 try:sys.stdout.reconfigure(encoding='utf-8',errors='replace')
 except Exception:pass
 ap=argparse.ArgumentParser();ap.add_argument('--dir',type=Path,default=None);ap.add_argument('--port',type=int,default=8765);ap.add_argument('--no-stats',action='store_true');ap.add_argument('--no-browser',action='store_true');ap.add_argument('--daily-limit',type=int,default=300);ap.add_argument('--rel-db',type=Path,default=None);ap.add_argument('--cache',type=Path,default=Path('ba-api-cache.json'));a=ap.parse_args()
 # 接管语义：同指纹已在运行则静默退出；不同指纹则关闭旧实例后接管
 urlfile=_urlfile();old_url=None
 try:old_url=urlfile.read_text(encoding='utf8').split('|')[0].strip()
 except OSError:pass
 if old_url:
  try:
   st=json.loads(urlopen(old_url+'/api/state',timeout=2).read())
  except OSError:
   st=None  # 拒连/超时：旧实例已死或挂死，按正常启动走
  except (ValueError,RuntimeError):
   st={'build':None}  # 有服务但应答异常：视为未知旧实例，接管替换
  if st is not None:
   if st.get('build')==BUILD:
    print(f'already running: {old_url} / 已有同版本实例在运行',flush=True);return
   try:
    urlopen(Request(old_url+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json'}),timeout=3).read()
    deadline=time.time()+4
    while time.time()<deadline:
     try:urlopen(old_url+'/api/state',timeout=.5)
     except Exception:break
     time.sleep(.3)
    print(f'took over previous instance at {old_url} / 已接管旧实例',flush=True)
   except OSError:pass
 if a.dir is None:a.dir=find_gamelogs() or DEFAULT_GAMELOGS
 cache=Cache(a.cache);quota=Quota(a.cache.with_name('ba-api-quota.json'),a.daily_limit);client=None if a.no_stats else ResilientClient('https://app.batrace.top',cache,quota);state=State(a.dir,client,cache,a.rel_db)
 if not a.dir.is_dir():state.phase=f'未找到日志目录 / GameLogs not found: {a.dir} —— 请确认游戏已安装并进入过一次对局，或用 --dir 指定路径'
 parser=LogParser(state.event);watcher=LogWatcher(a.dir,parser);threading.Thread(target=lambda:(setattr(state,'file','starting'),watcher.run()),daemon=True).start();threading.Thread(target=state.ban_loop,daemon=True).start()
 server=None
 for port in range(a.port,a.port+20):
  try:server=Server(('127.0.0.1',port),Handler);break
  except OSError as e:
   if getattr(e,'winerror',None)==10048 or e.errno in (48,98):continue
   raise
 if server is None:raise SystemExit(f'ports {a.port}-{a.port+19} all busy / 端口全部被占用')
 server.state=state;url=f'http://127.0.0.1:{port}';print(f'Web UI: {url}',flush=True)
 try:_urlfile().write_text(f'{url}|{int(time.time())}',encoding='utf8')
 except OSError:pass
 if not a.no_browser:threading.Timer(0.8,lambda:webbrowser.open(url)).start()
 try:server.serve_forever()
 finally:cache.flush();quota._save()
if __name__=='__main__':main()
