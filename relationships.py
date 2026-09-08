"""Local relationship and party heuristics database."""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path

class RelationshipDB:
    def __init__(self, path: Path):
        self.path=Path(path); self.lock=threading.Lock(); self.conn=sqlite3.connect(self.path,check_same_thread=False)
        self.conn.executescript('''CREATE TABLE IF NOT EXISTS matches(fid TEXT PRIMARY KEY, started TEXT, map TEXT); CREATE TABLE IF NOT EXISTS participants(fid TEXT,id TEXT,name TEXT,team TEXT,PRIMARY KEY(fid,id)); CREATE TABLE IF NOT EXISTS blacklist(id TEXT PRIMARY KEY, note TEXT DEFAULT '', added TEXT DEFAULT CURRENT_TIMESTAMP);'''); self.conn.commit()
    def add_match(self, match: dict):
        fid=str(match.get('fid') or '')
        if not fid:return
        with self.lock:
            self.conn.execute('INSERT OR REPLACE INTO matches VALUES(?,?,?)',(fid,match.get('start_time'),match.get('map','')))
            self.conn.execute('DELETE FROM participants WHERE fid=?',(fid,))
            self.conn.executemany('INSERT OR REPLACE INTO participants VALUES(?,?,?,?)',[(fid,str(p.get('id')),p.get('name',''),p.get('team')) for p in match.get('players',[])])
            self.conn.commit()
    def annotate(self, players: list[dict]) -> dict:
        ids=[str(p.get('id')) for p in players]; result={}
        with self.lock:
            for pid in ids:
                row=self.conn.execute('SELECT note FROM blacklist WHERE id=?',(pid,)).fetchone()
                if row: result[pid]={'blacklisted':True,'label':'熟人开黑','note':row[0]}; continue
                seen=self.conn.execute('SELECT COUNT(DISTINCT fid) FROM participants WHERE id=?',(pid,)).fetchone()
                result[pid]={'blacklisted':False,'matches_seen':seen[0] if seen else 0}
        return result
    def annotate_against_local(self, players: list[dict], local_id: str|None) -> dict:
        if not local_id:return self.annotate(players)
        out=self.annotate(players)
        with self.lock:
            for p in players:
                pid=str(p.get('id')); row=self.conn.execute('SELECT COUNT(DISTINCT a.fid) FROM participants a JOIN participants b ON a.fid=b.fid WHERE a.id=? AND b.id=? AND a.team=b.team',(local_id,pid)).fetchone(); opp=self.conn.execute('SELECT COUNT(DISTINCT a.fid) FROM participants a JOIN participants b ON a.fid=b.fid WHERE a.id=? AND b.id=? AND a.team<>b.team',(local_id,pid)).fetchone();
                out.setdefault(pid,{}).update({'prior_teammate_matches':row[0] if row else 0,'prior_opponent_matches':opp[0] if opp else 0})
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
