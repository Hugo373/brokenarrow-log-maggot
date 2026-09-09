"""Local relationship, name-history, result and ban database."""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path

class RelationshipDB:
    def __init__(self, path: Path):
        self.path=Path(path);self.lock=threading.Lock();self.conn=sqlite3.connect(self.path,check_same_thread=False)
        self.conn.executescript('''CREATE TABLE IF NOT EXISTS matches(fid TEXT PRIMARY KEY, started TEXT, map TEXT); CREATE TABLE IF NOT EXISTS participants(fid TEXT,id TEXT,name TEXT,team TEXT,PRIMARY KEY(fid,id)); CREATE TABLE IF NOT EXISTS blacklist(id TEXT PRIMARY KEY, note TEXT DEFAULT '', added TEXT DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS names(id TEXT,name TEXT,first_seen TEXT,last_seen TEXT,PRIMARY KEY(id,name)); CREATE TABLE IF NOT EXISTS match_results(fid TEXT PRIMARY KEY, won INTEGER); CREATE TABLE IF NOT EXISTS bans(id TEXT PRIMARY KEY, name TEXT, seen_at TEXT DEFAULT CURRENT_TIMESTAMP);'''); self.conn.commit()
    def add_match(self, match: dict):
        fid=str(match.get('fid') or '')
        if not fid:return
        started=match.get('start_time')
        with self.lock:
            self.conn.execute('INSERT OR REPLACE INTO matches VALUES(?,?,?)',(fid,started,match.get('map','')))
            self.conn.execute('DELETE FROM participants WHERE fid=?',(fid,))
            self.conn.executemany('INSERT OR REPLACE INTO participants VALUES(?,?,?,?)',[(fid,str(p.get('id')),p.get('name',''),p.get('team')) for p in match.get('players',[])])
            self.conn.executemany('INSERT INTO names(id,name,first_seen,last_seen) VALUES(?,?,?,?) ON CONFLICT(id,name) DO UPDATE SET last_seen=max(last_seen,excluded.last_seen), first_seen=min(first_seen,excluded.first_seen)',[(str(p.get('id')),p.get('name',''),started,started) for p in match.get('players',[]) if p.get('name')])
            self.conn.commit()
    def record_result(self,fid,won:bool):
        if not fid:return
        with self.lock:self.conn.execute('INSERT OR IGNORE INTO match_results VALUES(?,?)',(str(fid),1 if won else 0));self.conn.commit()
    def _record_pair(self, a: str, b: str, same_team: bool) -> tuple[int,int,int]:
        row=self.conn.execute('SELECT COUNT(DISTINCT a.fid), SUM(CASE WHEN r.won=1 THEN 1 ELSE 0 END), SUM(CASE WHEN r.won=0 THEN 1 ELSE 0 END) FROM participants a JOIN participants b ON a.fid=b.fid LEFT JOIN match_results r ON r.fid=a.fid WHERE a.id=? AND b.id=? AND a.team'+('=b.team' if same_team else '<>b.team'),(a,b)).fetchone()
        n,wins,losses=row if row else (0,None,None)
        # 胜/负一律我方视角：队友局的共同战绩，敌对局的交手战绩
        return n or 0,wins,losses
    def _flags(self,pid:str)->dict:
        banned=self.conn.execute('SELECT 1 FROM bans WHERE id=?',(pid,)).fetchone() is not None
        renamed=self.conn.execute('SELECT COUNT(*) FROM names WHERE id=?',(pid,)).fetchone()[0]>1
        return {'banned':banned,'renamed':renamed}
    def annotate(self, players: list[dict]) -> dict:
        ids=[str(p.get('id')) for p in players]; result={}
        with self.lock:
            for pid in ids:
                row=self.conn.execute('SELECT note FROM blacklist WHERE id=?',(pid,)).fetchone()
                if row:result[pid]={'blacklisted':True,'label':'开黑','note':row[0],**self._flags(pid)};continue
                seen=self.conn.execute('SELECT COUNT(DISTINCT fid) FROM participants WHERE id=?',(pid,)).fetchone()
                result[pid]={'blacklisted':False,'matches_seen':seen[0] if seen else 0,**self._flags(pid)}
        return result
    def annotate_against_local(self, players: list[dict], local_id: str|None) -> dict:
        if not local_id:return self.annotate(players)
        out=self.annotate(players)
        with self.lock:
            for p in players:
                pid=str(p.get('id'))
                tn,tw,tl=self._record_pair(str(local_id),pid,True);on,ow,ol=self._record_pair(str(local_id),pid,False)
                out.setdefault(pid,{}).update({'prior_teammate_matches':tn,'teammate_wins':tw,'teammate_losses':tl,'prior_opponent_matches':on,'opponent_wins':ow,'opponent_losses':ol})
        return out
    def set_blacklist(self, player_id: str, note: str=''):
        with self.lock:self.conn.execute('INSERT OR REPLACE INTO blacklist VALUES(?,?,CURRENT_TIMESTAMP)',(str(player_id),note));self.conn.commit()
    def remove_blacklist(self, player_id: str):
        with self.lock:self.conn.execute('DELETE FROM blacklist WHERE id=?',(str(player_id),));self.conn.commit()
    def list_blacklist(self):
        with self.lock:return [{'id':r[0],'note':r[1]} for r in self.conn.execute('SELECT id,note FROM blacklist ORDER BY added DESC')]
    def party_signal(self, players: list[dict]) -> dict:
        teams={}
        for p in players:teams.setdefault(p.get('team'),[]).append(str(p.get('id')))
        groups=[]
        with self.lock:
            for team,ids in teams.items():
                if len(ids)<2:continue
                pairs=[]
                for i,a in enumerate(ids):
                    for b in ids[i+1:]:
                        n=self.conn.execute('SELECT COUNT(DISTINCT a.fid) FROM participants a JOIN participants b ON a.fid=b.fid WHERE a.id=? AND b.id=? AND a.team=b.team',(a,b)).fetchone()[0]
                        if n>=2:pairs.append({'a':a,'b':b,'matches':n})
                if pairs:groups.append({'team':team,'pairs':pairs,'confidence':'medium' if len(pairs)==1 else 'high'})
        return {'groups':groups,'method':'local co-occurrence heuristic'}
    def apply_ban_snapshot(self, entries) -> list[dict]:
        incoming={str(e.get('id')):str(e.get('name') or '') for e in entries or [] if e.get('id') is not None}
        with self.lock:
            baseline=self.conn.execute('SELECT 1 FROM bans LIMIT 1').fetchone() is None
            prev={r[0] for r in self.conn.execute('SELECT id FROM bans')}
            met={r[0] for r in self.conn.execute('SELECT DISTINCT id FROM participants')}
            alerts=[{'id':i,'name':incoming[i]} for i in (incoming.keys()-prev) if (not baseline and i in met)]
            self.conn.execute('DELETE FROM bans')
            self.conn.executemany('INSERT INTO bans(id,name) VALUES(?,?)',incoming.items())
            self.conn.commit()
        return alerts
    def name_history(self, pid: str) -> list[dict]:
        with self.lock:return [{'name':r[0],'first_seen':r[1],'last_seen':r[2]} for r in self.conn.execute('SELECT name,first_seen,last_seen FROM names WHERE id=? ORDER BY last_seen DESC',(str(pid),))]
    def list_history(self, local_id: str|None) -> list[dict]:
        out=[]
        with self.lock:
            rows=self.conn.execute('SELECT m.fid,m.started,m.map,r.won FROM matches m LEFT JOIN match_results r ON r.fid=m.fid ORDER BY m.started DESC').fetchall()
            for fid,started,mmap,won in rows:
                players=[{'id':r[0],'name':r[1],'team':r[2]} for r in self.conn.execute('SELECT id,name,team FROM participants WHERE fid=? ORDER BY team',(fid,))]
                out.append({'fid':fid,'started':started,'map':mmap,'won':None if won is None else bool(won),'player_count':len(players),'players':players,'with_local':str(local_id) in {p['id'] for p in players} if local_id else False})
        return out
    def investigate(self, pid: str, local_id: str|None) -> dict:
        pid=str(pid);out={'id':pid,'blacklisted':False,'names':[],'first_seen':None,'last_seen':None,'teammate':None,'opponent':None,'recent':[]}
        with self.lock:
            out.update(self._flags(pid))
            row=self.conn.execute('SELECT note FROM blacklist WHERE id=?',(pid,)).fetchone()
            if row:out['blacklisted']=True;out['note']=row[0]
            out['names']=[{'name':r[0],'first_seen':r[1],'last_seen':r[2]} for r in self.conn.execute('SELECT name,first_seen,last_seen FROM names WHERE id=? ORDER BY last_seen DESC',(pid,))]
            span=self.conn.execute('SELECT MIN(m.started), MAX(m.started) FROM participants p JOIN matches m ON m.fid=p.fid WHERE p.id=?',(pid,)).fetchone()
            if span:out['first_seen'],out['last_seen']=span
            if local_id:
                tn,tw,tl=self._record_pair(str(local_id),pid,True);on,ow,ol=self._record_pair(str(local_id),pid,False)
                out['teammate']={'matches':tn,'wins':tw or 0,'losses':tl or 0}
                out['opponent']={'matches':on,'wins':ow or 0,'losses':ol or 0}
                out['recent']=[{'fid':r[0],'map':r[1],'started':r[2],'same_team':bool(r[3]),'won':None if r[4] is None else bool(r[4])} for r in self.conn.execute('SELECT b.fid, m.map, m.started, b.team=a.team, r.won FROM participants a JOIN participants b ON a.fid=b.fid AND b.id=? JOIN matches m ON m.fid=b.fid LEFT JOIN match_results r ON r.fid=b.fid WHERE a.id=? ORDER BY m.started DESC LIMIT 10',(pid,str(local_id)))]
        return out
