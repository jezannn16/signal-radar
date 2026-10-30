"""Source adapters, normalization, persistence and explainable prioritization."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import certifi


def utc_now()->str: return datetime.now(UTC).isoformat()

def canonical_url(url:str)->str:
    parts=urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() not in {"http","https"} or not parts.hostname: raise ValueError("Source item URL must be absolute HTTP(S)")
    host=parts.hostname.lower(); port=parts.port
    netloc=host+(f":{port}" if port and not ((parts.scheme.lower()=="http" and port==80) or (parts.scheme.lower()=="https" and port==443)) else "")
    query=urllib.parse.parse_qsl(parts.query,keep_blank_values=True)
    query=[(k,v) for k,v in query if not k.lower().startswith("utm_") and k.lower() not in {"fbclid","gclid","mc_cid","mc_eid"}]
    return urllib.parse.urlunsplit((parts.scheme.lower(),netloc,parts.path or "/",urllib.parse.urlencode(sorted(query)),""))

def parse_datetime(value:str|None)->str|None:
    if not value: return None
    try:
        dt=parsedate_to_datetime(value)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    except (TypeError,ValueError,OverflowError):
        try:
            dt=datetime.fromisoformat(value)
            if dt.tzinfo is None: dt=dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat()
        except ValueError: return None


@dataclass(frozen=True)
class Item:
    url:str
    title:str
    source:str
    published_at:str|None
    content:str=""

    @property
    def canonical(self)->str: return canonical_url(self.url)
    @property
    def content_hash(self)->str: return hashlib.sha256((self.title+"\n"+self.content).encode()).hexdigest()


@dataclass
class HttpResult:
    body:bytes
    headers:dict[str,str]
    status:int=200


@dataclass
class SourceFetch:
    items: list[Item]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class HttpClient:
    def __init__(self,transport:Callable[[str,dict[str,str],float],HttpResult]|None=None,timeout:float=8,retries:int=2):
        self.transport=transport or self._request; self.timeout=timeout; self.retries=retries
    @staticmethod
    def _request(url:str,headers:dict[str,str],timeout:float)->HttpResult:
        req=urllib.request.Request(url,headers=headers)
        context=ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(req,timeout=timeout,context=context) as response: return HttpResult(response.read(3_000_000),dict(response.headers.items()),response.status)
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.read(100_000),dict(exc.headers.items()),exc.code)
    def get(self,url:str,headers:dict[str,str]|None=None)->HttpResult:
        request_headers={"User-Agent":"SignalRadar/0.1 (personal read-only monitor)","Accept":"application/atom+xml, application/rss+xml, application/json, application/xml, text/xml"}
        request_headers.update(headers or {})
        for attempt in range(self.retries+1):
            try: result=self.transport(url,request_headers,self.timeout)
            except (TimeoutError,urllib.error.URLError,ConnectionError) as exc:
                if attempt==self.retries: raise RuntimeError(f"Source request failed: {type(exc).__name__}") from exc
                time.sleep(random.uniform(0,min(8,0.5*(2**attempt)))); continue
            if result.status==429 or 500<=result.status<600:
                if attempt==self.retries: raise RuntimeError(f"Source returned HTTP {result.status}")
                retry_after=result.headers.get("Retry-After")
                delay=0.0
                if retry_after:
                    try: delay=min(30,max(0,float(retry_after)))
                    except ValueError:
                        try: delay=min(30,max(0,(parsedate_to_datetime(retry_after)-datetime.now(UTC)).total_seconds()))
                        except (TypeError, ValueError, OverflowError): delay=0
                time.sleep(delay or random.uniform(0,min(8,0.5*(2**attempt)))); continue
            if result.status == 304:
                return result
            if not 200<=result.status<300: raise RuntimeError(f"Source returned HTTP {result.status}")
            return result
        raise RuntimeError("Source request failed")


def parse_feed(xml:bytes,source_name:str)->list[Item]:
    root=ET.fromstring(xml); out=[]
    atom_ns="{http://www.w3.org/2005/Atom}"
    if root.tag.endswith("feed"):
        for entry in root.findall(f"{atom_ns}entry"):
            link=next((e.attrib.get("href") for e in entry.findall(f"{atom_ns}link") if e.attrib.get("rel","alternate")=="alternate"),None)
            title=entry.findtext(f"{atom_ns}title","").strip()
            content=(entry.findtext(f"{atom_ns}summary") or entry.findtext(f"{atom_ns}content") or "").strip()
            published=entry.findtext(f"{atom_ns}published") or entry.findtext(f"{atom_ns}updated")
            if link and title: out.append(Item(link,title,source_name,parse_datetime(published),content))
    else:
        channel=root.find("channel")
        entries=channel.findall("item") if channel is not None else root.findall("{http://www.w3.org/2005/Atom}entry")
        for entry in entries:
            link=entry.findtext("link")
            if link is None:
                node=entry.find("{http://www.w3.org/2005/Atom}link"); link=node.attrib.get("href") if node is not None else None
            title=entry.findtext("title","").strip()
            published=entry.findtext("pubDate") or entry.findtext("published") or entry.findtext("updated")
            content=entry.findtext("description") or entry.findtext("summary") or ""
            if link and title: out.append(Item(link,title,source_name,parse_datetime(published),content.strip()))
    return out


class SourceAdapter:
    def __init__(self,http:HttpClient): self.http=http
    def fetch(self,config:dict,etag:str|None=None,last_modified:str|None=None)->SourceFetch: raise NotImplementedError

class RSSAdapter(SourceAdapter):
    def fetch(self,config:dict,etag:str|None=None,last_modified:str|None=None)->SourceFetch:
        headers={}
        if etag: headers["If-None-Match"]=etag
        if last_modified: headers["If-Modified-Since"]=last_modified
        res=self.http.get(config["url"],headers)
        return SourceFetch(
            items=[] if res.status==304 else parse_feed(res.body,config["name"]),
            etag=res.headers.get("ETag") or etag,
            last_modified=res.headers.get("Last-Modified") or last_modified,
            not_modified=res.status==304,
        )

class GitHubAdapter(SourceAdapter):
    """Official public GitHub REST API, paginates with Link; no page scraping."""
    def __init__(self,http:HttpClient,token:str|None=None): super().__init__(http); self.token=token
    def fetch(self,config:dict,etag:str|None=None,last_modified:str|None=None)->SourceFetch:
        # Release notes can be large; a smaller page keeps the bounded HTTP body intact.
        owner,repo=config["repo"].split("/",1); url=f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=20"
        headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
        if self.token: headers["Authorization"]=f"Bearer {self.token}"
        items=[]; page_count=0
        while url and page_count<10:
            result=self.http.get(url,headers); payload=json.loads(result.body)
            if not isinstance(payload,list): raise TypeError("GitHub API returned an unexpected response")
            for release in payload:
                html = release.get("html_url")
                title = release.get("name") or release.get("tag_name")
                if html and title:
                    items.append(Item(html, title, config["name"], parse_datetime(release.get("published_at") or release.get("created_at")), release.get("body") or ""))
            link = result.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
            if url:
                next_url = urllib.parse.urlsplit(url)
                if next_url.scheme != "https" or next_url.hostname != "api.github.com":
                    raise ValueError("GitHub API returned a non-GitHub pagination URL")
            page_count += 1
        if url: raise RuntimeError("GitHub pagination exceeded 10 pages; reduce collection interval")
        return SourceFetch(items=items)


class RadarStore:
    def __init__(self,path:str):
        self.path=path
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,config TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,last_fetched_at TEXT,last_checkpoint TEXT,last_error TEXT,etag TEXT,last_modified TEXT);
            CREATE TABLE IF NOT EXISTS normalized_items(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL,canonical_url TEXT NOT NULL,title TEXT NOT NULL,published_at TEXT,fetched_at TEXT NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,score INTEGER NOT NULL DEFAULT 0,why TEXT NOT NULL DEFAULT '',UNIQUE(source_id,canonical_url));
            CREATE TABLE IF NOT EXISTS item_versions(id INTEGER PRIMARY KEY,item_id INTEGER NOT NULL,title TEXT NOT NULL,content_hash TEXT NOT NULL,observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS keyword_rules(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('include','exclude')),keyword TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS scoped_keyword_rules(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('include','exclude')),keyword TEXT NOT NULL,scope_type TEXT NOT NULL CHECK(scope_type IN ('source','topic')),scope_value TEXT NOT NULL,UNIQUE(kind,keyword,scope_type,scope_value));
            CREATE TABLE IF NOT EXISTS delivery_log(id INTEGER PRIMARY KEY,item_id INTEGER NOT NULL UNIQUE,state TEXT NOT NULL,created_at TEXT NOT NULL,delivered_at TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
            INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,CURRENT_TIMESTAMP);
            """)
            columns={row[1] for row in db.execute("PRAGMA table_info(sources)")}
            if "etag" not in columns: db.execute("ALTER TABLE sources ADD COLUMN etag TEXT")
            if "last_modified" not in columns: db.execute("ALTER TABLE sources ADD COLUMN last_modified TEXT")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,CURRENT_TIMESTAMP)")
            if "topic" not in columns: db.execute("ALTER TABLE sources ADD COLUMN topic TEXT")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,CURRENT_TIMESTAMP)")
    def db(self):
        if self.path != ":memory:":
            fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
        db=sqlite3.connect(self.path,timeout=10)
        if self.path != ":memory:":
            for candidate in (self.path,self.path+"-wal",self.path+"-shm",self.path+"-journal"):
                try:os.chmod(candidate,0o600)
                except FileNotFoundError:pass
        db.row_factory=sqlite3.Row; return db
    def add_source(self,name:str,kind:str,config:dict,topic:str|None=None)->None:
        if not name.strip() or kind not in {"rss","github"}: raise ValueError("Source requires a name and kind rss|github")
        if kind=="rss":
            url=config.get("url","")
            parts=urllib.parse.urlsplit(url)
            if parts.scheme not in {"http","https"} or not parts.hostname:
                raise ValueError("RSS source URL must be absolute HTTP(S)")
            if parts.username is not None or parts.password is not None:
                raise ValueError("Do not put credentials in RSS URLs; use a public feed URL")
        if kind=="github" and not re.fullmatch(r"[^/]+/[^/]+",config.get("repo","")): raise ValueError("GitHub repository must use owner/repo")
        config={**config,"name":name.strip()}; topic=(topic or "").strip() or None
        with self.db() as db: db.execute("INSERT INTO sources(name,kind,config,topic) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,config=excluded.config,topic=excluded.topic,enabled=1",(name.strip(),kind,json.dumps(config),topic))
    def sources(self):
        with self.db() as db: return list(db.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY name"))
    def rules(self)->list[sqlite3.Row]:
        with self.db() as db:return list(db.execute("SELECT * FROM keyword_rules ORDER BY id"))
    def add_rule(self,kind:str,keyword:str,source:str|None=None,topic:str|None=None)->None:
        kind=kind.lower(); keyword=keyword.strip().casefold()
        if kind not in {"include","exclude"} or not keyword: raise ValueError("Rule must be include|exclude and non-empty")
        if source and topic: raise ValueError("Choose either a source or topic scope")
        if not source and not topic:
            with self.db() as db:db.execute("INSERT OR IGNORE INTO keyword_rules(kind,keyword) VALUES(?,?)",(kind,keyword))
            return
        scope_type="source" if source else "topic"; scope_value=(source or topic or "").strip()
        if not scope_value: raise ValueError("Rule scope cannot be empty")
        with self.db() as db:
            if source and not db.execute("SELECT 1 FROM sources WHERE name=? AND enabled=1",(scope_value,)).fetchone(): raise ValueError("Scoped rule source not found")
            db.execute("INSERT OR IGNORE INTO scoped_keyword_rules(kind,keyword,scope_type,scope_value) VALUES(?,?,?,?)",(kind,keyword,scope_type,scope_value))
    def _rules_for(self,source_id:int|None,topic:str|None):
        with self.db() as db:
            rows=list(db.execute("SELECT kind,keyword,NULL scope_type,NULL scope_value FROM keyword_rules"))
            if source_id is not None: rows.extend(db.execute("SELECT kind,keyword,scope_type,scope_value FROM scoped_keyword_rules WHERE scope_type='source' AND scope_value=(SELECT name FROM sources WHERE id=?)",(source_id,)))
            if topic: rows.extend(db.execute("SELECT kind,keyword,scope_type,scope_value FROM scoped_keyword_rules WHERE scope_type='topic' AND scope_value=?",(topic,)))
        return rows
    def score(self,item:Item,source_id:int|None=None,topic:str|None=None)->tuple[int,str]|None:
        text=(item.title+" "+item.content).casefold(); rules=self._rules_for(source_id,topic); excludes=[r["keyword"] for r in rules if r["kind"]=="exclude"]
        matched_ex=[w for w in excludes if w in text]
        if matched_ex:return None
        includes=[r["keyword"] for r in rules if r["kind"]=="include"]
        matched=[w for w in includes if w in text]
        if includes and not matched:return None
        reasons=[f"include keyword: {w}" for w in matched]
        reasons.extend(f"{r['scope_type']} rule: {r['keyword']}" for r in rules if r["kind"]=="exclude" and r["keyword"] in text and r["scope_type"])
        score=min(100,10+25*len(matched))
        if item.published_at: reasons.append("publication date available")
        else: reasons.append("publication date unavailable")
        return score,"; ".join(reasons)
    def upsert_items(self,source_id:int,items:list[Item],topic:str|None=None)->tuple[int,int,int]:
        inserted=updated=ignored=0; now=utc_now()
        with self.db() as db:
            for item in items:
                scored=self.score(item,source_id,topic)
                if scored is None: ignored+=1; continue
                canonical=item.canonical
                old=db.execute("SELECT id,title,content_hash FROM normalized_items WHERE source_id=? AND canonical_url=?",(source_id,canonical)).fetchone()
                if old:
                    if old["content_hash"]!=item.content_hash:
                        db.execute("INSERT INTO item_versions(item_id,title,content_hash,observed_at) VALUES(?,?,?,?)",(old["id"],old["title"],old["content_hash"],now))
                        db.execute("UPDATE normalized_items SET title=?,published_at=COALESCE(?,published_at),fetched_at=?,content=?,content_hash=?,score=?,why=? WHERE id=?",(item.title,item.published_at,now,item.content,item.content_hash,scored[0],scored[1],old["id"])); updated+=1
                    else:
                        db.execute("UPDATE normalized_items SET fetched_at=? WHERE id=?",(now,old["id"]))
                else:
                    db.execute("INSERT INTO normalized_items(source_id,canonical_url,title,published_at,fetched_at,content,content_hash,score,why) VALUES(?,?,?,?,?,?,?,?,?)",(source_id,canonical,item.title,item.published_at,now,item.content,item.content_hash,scored[0],scored[1])); inserted+=1
        return inserted,updated,ignored
    def collect(self,http:HttpClient,token:str|None=None)->dict:
        totals={"inserted":0,"updated":0,"ignored":0,"not_modified":0,"failed":[]}
        for source in self.sources():
            config=json.loads(source["config"])
            try:
                adapter=RSSAdapter(http) if source["kind"]=="rss" else GitHubAdapter(http,token)
                result=adapter.fetch(config,source["etag"],source["last_modified"])
                added,updated,ignored=self.upsert_items(source["id"],result.items,source["topic"])
                with self.db() as db:db.execute("UPDATE sources SET last_fetched_at=?,last_checkpoint=?,etag=?,last_modified=?,last_error=NULL WHERE id=?",(utc_now(),result.etag or result.last_modified,result.etag,result.last_modified,source["id"]))
                totals["inserted"]+=added; totals["updated"]+=updated; totals["ignored"]+=ignored
                if result.not_modified: totals["not_modified"]+=1
            except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError, UnicodeError, sqlite3.Error, ET.ParseError, urllib.error.URLError) as exc:
                message=str(exc)
                safe=message if re.fullmatch(r"Source returned HTTP \d{3}|Source request failed: [A-Za-z]+|GitHub pagination exceeded 10 pages; reduce collection interval",message) else f"{type(exc).__name__} (details omitted)"
                totals["failed"].append({"source":source["name"],"error":safe})
                with self.db() as db:db.execute("UPDATE sources SET last_fetched_at=?,last_error=? WHERE id=?",(utc_now(),safe,source["id"]))
        return totals
    def items(self,limit:int=50):
        with self.db() as db:return list(db.execute("SELECT i.*,s.name source_name FROM normalized_items i JOIN sources s ON s.id=i.source_id ORDER BY COALESCE(i.published_at,i.fetched_at) DESC,i.id DESC LIMIT ?",(limit,)))
    def explain(self,item_id:int):
        with self.db() as db:return db.execute("SELECT i.*,s.name source_name FROM normalized_items i JOIN sources s ON s.id=i.source_id WHERE i.id=?",(item_id,)).fetchone()
    def reserve_delivery(self,item_id:int)->bool:
        with self.db() as db:
            try:db.execute("INSERT INTO delivery_log(item_id,state,created_at) VALUES(?,'sending',?)",(item_id,utc_now()));return True
            except sqlite3.IntegrityError:return False
    def reserve_delivery_group(self,item_id:int)->list[int]:
        """Atomically claim all source copies of one canonical URL for one digest send."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT canonical_url FROM normalized_items WHERE id=?",(item_id,)).fetchone()
            if row is None:return []
            aliases=[int(x[0]) for x in db.execute("SELECT id FROM normalized_items WHERE canonical_url=? ORDER BY id",(row["canonical_url"],))]
            marks=",".join("?" for _ in aliases)
            if db.execute(f"SELECT 1 FROM delivery_log WHERE item_id IN ({marks}) LIMIT 1",aliases).fetchone():return []
            db.executemany("INSERT INTO delivery_log(item_id,state,created_at) VALUES(?,'sending',?)",[(alias,utc_now()) for alias in aliases])
            return aliases
    def finish_delivery(self,item_id:int,ok:bool,error:str="")->None:
        with self.db() as db:db.execute("UPDATE delivery_log SET state=?,delivered_at=?,error=? WHERE item_id=? AND state='sending'",("sent" if ok else "failed",utc_now() if ok else None,error[:300],item_id))
    def pending_delivery(self,limit:int=30):
        with self.db() as db:return list(db.execute("""SELECT i.*,s.name source_name,
            (SELECT group_concat(DISTINCT s2.name) FROM normalized_items i2 JOIN sources s2 ON s2.id=i2.source_id WHERE i2.canonical_url=i.canonical_url) source_names
            FROM normalized_items i JOIN sources s ON s.id=i.source_id
            WHERE i.id=(SELECT MIN(i2.id) FROM normalized_items i2 WHERE i2.canonical_url=i.canonical_url)
              AND NOT EXISTS(SELECT 1 FROM normalized_items i2 JOIN delivery_log d2 ON d2.item_id=i2.id WHERE i2.canonical_url=i.canonical_url)
            ORDER BY COALESCE(i.published_at,i.fetched_at) DESC,i.id DESC LIMIT ?""",(limit,)))
    def recover_stale_deliveries(self,older_than_seconds:int=600,now:datetime|None=None)->int:
        """Mark interrupted Telegram sends uncertain; never resend automatically."""
        if older_than_seconds<1: raise ValueError("Recovery age must be positive")
        cutoff=(now or datetime.now(UTC)).astimezone(UTC).timestamp()-older_than_seconds
        cutoff_iso=datetime.fromtimestamp(cutoff,UTC).isoformat()
        with self.db() as db:
            cur=db.execute("UPDATE delivery_log SET state='unknown',error='Delivery outcome uncertain after interrupted send' WHERE state='sending' AND created_at<?",(cutoff_iso,))
            return cur.rowcount
    @staticmethod
    def _delivery_window_open(db:sqlite3.Connection,now:datetime)->bool:
        row=db.execute("SELECT value FROM metadata WHERE key='min_interval_seconds'").fetchone()
        interval=int(row[0]) if row else 0
        if interval==0:return True
        markers=[]
        reservation=db.execute("SELECT value FROM metadata WHERE key='last_digest_window_at'").fetchone()
        if reservation:
            markers.append(datetime.fromisoformat(reservation[0]))
        last_delivery=db.execute("SELECT created_at FROM delivery_log ORDER BY id DESC LIMIT 1").fetchone()
        if last_delivery:
            markers.append(datetime.fromisoformat(last_delivery[0]))
        if not markers:return True
        then=max(markers)
        if then.tzinfo is None:then=then.replace(tzinfo=UTC)
        return (now.astimezone(UTC)-then.astimezone(UTC)).total_seconds()>=interval

    def delivery_allowed(self,now:datetime|None=None)->bool:
        """Read the persisted cooldown state; claim_digest_window also serializes the check."""
        clock=now or datetime.now(UTC)
        if clock.tzinfo is None:raise ValueError("Delivery clock must be timezone-aware")
        with self.db() as db:return self._delivery_window_open(db,clock)

    def claim_digest_window(self,now:datetime|None=None)->bool:
        """Atomically reserve the global notification interval across concurrent processes.

        The reservation is persisted before network delivery. A process crash or uncertain send
        therefore consumes the cooldown conservatively instead of allowing a parallel duplicate.
        """
        clock=now or datetime.now(UTC)
        if clock.tzinfo is None:raise ValueError("Delivery clock must be timezone-aware")
        timestamp=clock.astimezone(UTC).isoformat()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._delivery_window_open(db,clock):return False
            interval_row=db.execute("SELECT value FROM metadata WHERE key='min_interval_seconds'").fetchone()
            if interval_row and int(interval_row[0])>0:
                db.execute("INSERT INTO metadata(key,value) VALUES('last_digest_window_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(timestamp,))
            return True
    def digest_marked(self,key:str)->bool:
        with self.db() as db:return db.execute("SELECT 1 FROM metadata WHERE key=?",(key,)).fetchone() is not None
    def mark_digest(self,key:str)->None:
        with self.db() as db:db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,utc_now()))
    def set_min_interval(self,seconds:int):
        if seconds<0:raise ValueError("Minimum interval cannot be negative")
        with self.db() as db:db.execute("INSERT INTO metadata(key,value) VALUES('min_interval_seconds',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(seconds),))
