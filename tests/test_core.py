import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from stat import S_IMODE

from signal_radar.core import HttpClient, HttpResult, Item, RadarStore, canonical_url, parse_feed


def test_canonical_url_and_rss_parse():
    assert canonical_url("HTTPS://Example.com/a?utm_source=x&b=2#frag")=="https://example.com/a?b=2"
    payload=b'''<rss><channel><item><title>Release</title><link>https://example.test/a</link><pubDate>Mon, 02 Mar 2026 09:00:00 GMT</pubDate><description>new API</description></item></channel></rss>'''
    items=parse_feed(payload,"feed")
    assert items[0].title=="Release" and items[0].published_at.endswith("+00:00")


def test_filter_dedup_update_history_and_explanation(tmp_path):
    store=RadarStore(str(tmp_path/"radar.db"));store.add_source("x","rss",{"url":"https://example.test/feed"});source=store.sources()[0]
    store.add_rule("include","api");store.add_rule("exclude","deprecated")
    assert store.upsert_items(source["id"],[Item("https://example.test/a?utm_source=x","New API","x",None,"body")])==(1,0,0)
    assert store.upsert_items(source["id"],[Item("https://example.test/a","New API","x",None,"body")])==(0,0,0)
    assert store.upsert_items(source["id"],[Item("https://example.test/a","Updated API","x",None,"changed")])==(0,1,0)
    assert store.upsert_items(source["id"],[Item("https://example.test/b","Deprecated API","x",None)])==(0,0,1)
    row=store.items()[0];assert "include keyword: api" in row["why"] and row["title"]=="Updated API"
    with store.db() as db: assert db.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0]==1


def test_one_source_failure_does_not_block_other_and_duplicates(tmp_path):
    store=RadarStore(str(tmp_path/"radar.db"));store.add_source("bad","rss",{"url":"https://bad.test/feed"});store.add_source("good","rss",{"url":"https://good.test/feed"})
    good=b'<rss><channel><item><title>News</title><link>https://good.test/n</link></item></channel></rss>'
    def fake(url,headers,timeout):
        if "bad" in url:return HttpResult(b"oops",{},503)
        return HttpResult(good,{"ETag":"x"})
    result=store.collect(HttpClient(fake,retries=0))
    assert result["inserted"]==1 and len(result["failed"])==1
    assert store.collect(HttpClient(fake,retries=0))["inserted"]==0


def test_delivery_claim_and_rate_limit(tmp_path):
    store=RadarStore(str(tmp_path/"radar.db"));store.add_source("x","rss",{"url":"https://x.test"})
    source=store.sources()[0];store.upsert_items(source["id"],[Item("https://x.test/a","A","x",None)])
    row=store.pending_delivery()[0];assert store.reserve_delivery(row["id"]);assert not store.reserve_delivery(row["id"])
    store.finish_delivery(row["id"],True);assert store.pending_delivery()==[]
    store.set_min_interval(3600);assert not store.delivery_allowed()


def test_github_adapter_paginates_with_link_header():
    pages=[]
    def fake(url,headers,timeout):
        pages.append(url)
        body=json.dumps([{"name":"v1","html_url":"https://github.com/a/b/releases/tag/v1","published_at":"2026-01-01T00:00:00Z","body":"release"}]).encode()
        link={'Link':'<https://api.github.com/repos/a/b/releases?per_page=100&page=2>; rel="next"'} if len(pages)==1 else {}
        return HttpResult(body,link)
    from signal_radar.core import GitHubAdapter
    result=GitHubAdapter(HttpClient(fake,retries=0)).fetch({"name":"github","repo":"a/b"})
    assert len(result.items)==2 and len(pages)==2


def test_github_rejects_untrusted_pagination_url():
    from signal_radar.core import GitHubAdapter
    def fake(url,headers,timeout):
        return HttpResult(b"[]",{"Link":'<https://attacker.example/collect>; rel="next"'})
    import pytest
    with pytest.raises(ValueError,match="non-GitHub"):
        GitHubAdapter(HttpClient(fake,retries=0)).fetch({"name":"github","repo":"a/b"})


def test_http_error_is_bounded_and_reported():
    import pytest
    http=HttpClient(lambda url,headers,timeout:HttpResult(b"down",{},503),retries=0)
    with pytest.raises(RuntimeError,match="HTTP 503"):
        http.get("https://source.example")


def test_schedule_timezone_quiet_hours_and_weekly():
    from datetime import datetime

    from signal_radar.cli import scheduled_key
    now=datetime(2026,10,26,6,0,tzinfo=UTC) # 09:00 Moscow, Monday
    assert scheduled_key(now,"Europe/Moscow","daily",9,23,8)=="digest_sent:2026-10-26"
    assert scheduled_key(now,"Europe/Moscow","weekly",9,23,8)=="digest_sent:2026-W44"
    assert scheduled_key(now,"Europe/Moscow","daily",9,8,10) is None
    assert scheduled_key(now.replace(hour=5),"Europe/Moscow","daily",9,23,8) is None
    catchup=now.replace(hour=7)
    assert scheduled_key(catchup,"Europe/Moscow","daily",9,23,8)=="digest_sent:2026-10-26"
    assert scheduled_key(catchup.replace(day=27),"Europe/Moscow","weekly",9,23,8)=="digest_sent:2026-W44"
    assert scheduled_key(catchup.replace(hour=23),"Europe/Moscow","daily",9,23,8) is None


def test_scheduled_digest_slot_only_consumed_after_confirmed_send(tmp_path):
    from signal_radar.cli import record_scheduled_delivery

    store=RadarStore(str(tmp_path/"schedule.db"));key="digest_sent:2026-10-30"
    assert not record_scheduled_delivery(store,key,"Пауза между отправками ещё не прошла")
    assert not store.digest_marked(key)
    assert not record_scheduled_delivery(store,key,"Новых сигналов нет.")
    assert not store.digest_marked(key)
    assert record_scheduled_delivery(store,key,"Отправлено сигналов: 1")
    assert store.digest_marked(key)


def test_rss_conditional_get_persists_validators_and_handles_304(tmp_path):
    store=RadarStore(str(tmp_path/"radar.db"))
    store.add_source("conditional","rss",{"url":"https://feed.example.test/rss"})
    calls=[]
    def fake(url,headers,timeout):
        calls.append(headers)
        if len(calls)==1:
            body=b'<rss><channel><item><title>Release</title><link>https://example.test/release</link></item></channel></rss>'
            return HttpResult(body,{"ETag":"\"v1\"","Last-Modified":"Thu, 01 Jan 2026 00:00:00 GMT"})
        return HttpResult(b"",{},304)
    http=HttpClient(fake,retries=0)
    assert store.collect(http)["inserted"]==1
    store=RadarStore(str(tmp_path/"radar.db"))
    second=store.collect(http)
    assert second["not_modified"]==1 and second["inserted"]==0
    assert calls[1]["If-None-Match"]=='"v1"'
    assert calls[1]["If-Modified-Since"]=="Thu, 01 Jan 2026 00:00:00 GMT"
    with store.db() as db:
        row=db.execute("SELECT etag,last_modified FROM sources WHERE name='conditional'").fetchone()
    assert row["etag"]=='"v1"' and row["last_modified"]=="Thu, 01 Jan 2026 00:00:00 GMT"


def test_rss_validator_columns_migrate_existing_database(tmp_path):
    path=tmp_path/"legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,config TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,last_fetched_at TEXT,last_checkpoint TEXT,last_error TEXT)")
    store=RadarStore(str(path))
    with store.db() as db:
        columns={row[1] for row in db.execute("PRAGMA table_info(sources)")}
        assert {"etag","last_modified","topic"}.issubset(columns)
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]==3


def test_scoped_source_and_topic_rules_only_apply_to_matching_items(tmp_path):
    store=RadarStore(str(tmp_path/"scoped.db"))
    store.add_source("vendor","rss",{"url":"https://vendor.test/rss"},topic="releases")
    store.add_source("other","rss",{"url":"https://other.test/rss"},topic="news")
    sources={row["name"]:row for row in store.sources()};vendor=sources["vendor"];other=sources["other"]
    store.add_rule("include","release",source="vendor")
    store.add_rule("exclude","preview",topic="releases")
    item=Item("https://item.test/a","Release candidate","feed",None,"details")
    assert store.upsert_items(vendor["id"],[item],vendor["topic"])==(1,0,0)
    assert store.upsert_items(other["id"],[item],other["topic"])==(1,0,0)
    excluded=Item("https://item.test/b","Release preview","feed",None)
    assert store.upsert_items(vendor["id"],[excluded],vendor["topic"])==(0,0,1)
    assert store.upsert_items(other["id"],[excluded],other["topic"])==(1,0,0)


def test_stale_delivery_is_marked_uncertain_and_never_requeued(tmp_path):
    from datetime import datetime, timedelta

    store=RadarStore(str(tmp_path/"delivery.db"));store.add_source("x","rss",{"url":"https://x.test"})
    source=store.sources()[0];store.upsert_items(source["id"],[Item("https://x.test/a","A","x",None)])
    row=store.pending_delivery()[0];assert store.reserve_delivery(row["id"])
    started=datetime.now(UTC)-timedelta(hours=1)
    with store.db() as db:db.execute("UPDATE delivery_log SET created_at=? WHERE item_id=?",(started.isoformat(),row["id"]))
    assert store.recover_stale_deliveries(now=datetime.now(UTC))==1
    assert store.recover_stale_deliveries(now=datetime.now(UTC))==0
    assert store.pending_delivery()==[]
    with store.db() as db:
        delivery=db.execute("SELECT state,error FROM delivery_log WHERE item_id=?",(row["id"],)).fetchone()
    assert delivery["state"]=="unknown" and "uncertain" in delivery["error"]


def test_cross_source_canonical_duplicates_share_one_delivery_claim(tmp_path):
    store=RadarStore(str(tmp_path/"cross-source.db"))
    store.add_source("alpha","rss",{"url":"https://alpha.test/rss"})
    store.add_source("beta","rss",{"url":"https://beta.test/rss"})
    sources={row["name"]:row for row in store.sources()}
    url="https://same.test/release?utm_source=alpha"
    assert store.upsert_items(sources["alpha"]["id"],[Item(url,"Release","alpha",None)])==(1,0,0)
    assert store.upsert_items(sources["beta"]["id"],[Item("https://same.test/release","Release","beta",None)])==(1,0,0)
    pending=store.pending_delivery()
    assert len(pending)==1 and set(pending[0]["source_names"].split(","))=={"alpha","beta"}
    claimed=store.reserve_delivery_group(pending[0]["id"])
    assert len(claimed)==2 and store.pending_delivery()==[]
    for item_id in claimed:store.finish_delivery(item_id,True)
    assert store.pending_delivery()==[]


def test_digest_fake_telegram_recovers_stale_claim_and_sends_only_pending(tmp_path):
    import sys
    from datetime import timedelta
    from types import ModuleType
    from unittest.mock import patch

    from signal_radar.cli import send_digest

    store=RadarStore(str(tmp_path/"digest.db"));store.add_source("x","rss",{"url":"https://x.test"});store.add_source("y","rss",{"url":"https://y.test"})
    sources={row["name"]:row for row in store.sources()};source=sources["x"]
    store.upsert_items(source["id"],[Item("https://x.test/old","Old stuck signal","x",None)])
    old=store.pending_delivery()[0];assert store.reserve_delivery(old["id"])
    with store.db() as db:db.execute("UPDATE delivery_log SET created_at=? WHERE item_id=?",((datetime.now(UTC)-timedelta(hours=1)).isoformat(),old["id"]))
    store.upsert_items(source["id"],[Item("https://x.test/new","Fresh signal","x",None)])
    store.upsert_items(sources["y"]["id"],[Item("https://x.test/new","Fresh signal","y",None)])

    class Session:
        async def close(self):pass
    sent_messages=[]
    class FakeBot:
        def __init__(self,token):self.session=Session()
        async def send_message(self,chat_id,text):sent_messages.append((chat_id,text))
    aiogram=ModuleType("aiogram");aiogram.Bot=FakeBot
    exceptions=ModuleType("aiogram.exceptions")
    class FakeRetryAfter(Exception):
        retry_after=0
    exceptions.TelegramRetryAfter=FakeRetryAfter
    with patch.dict(sys.modules,{"aiogram":aiogram,"aiogram.exceptions":exceptions}):
        result=asyncio.run(send_digest(store,"synthetic-token","synthetic-chat"))
    assert result=="Отправлено сигналов: 1"
    assert len(sent_messages)==1 and "Fresh signal" in sent_messages[0][1] and "Old stuck signal" not in sent_messages[0][1]
    assert "Источники: x, y" in sent_messages[0][1]
    with store.db() as db:
        states={row["item_id"]:row["state"] for row in db.execute("SELECT item_id,state FROM delivery_log")}
    assert states[old["id"]]=="unknown"
    assert list(states.values()).count("sent")==2


def test_database_file_is_private_by_default(tmp_path):
    path=tmp_path/"private.db"
    RadarStore(str(path))
    assert S_IMODE(path.stat().st_mode)==0o600


def test_source_failure_does_not_persist_url_secrets(tmp_path):
    from unittest.mock import patch

    store=RadarStore(str(tmp_path/"radar.db"))
    store.add_source("private feed","rss",{"url":"https://example.test/feed?token=synthetic-secret"})
    with patch("signal_radar.core.RSSAdapter.fetch",side_effect=RuntimeError("request failed for token=synthetic-secret")):
        result=store.collect(HttpClient(retries=0))
    assert result["failed"][0]["error"]=="RuntimeError (details omitted)"
    with store.db() as db:
        persisted=db.execute("SELECT last_error FROM sources WHERE name='private feed'").fetchone()[0]
    assert "synthetic-secret" not in persisted


def test_rss_source_rejects_embedded_credentials(tmp_path):
    import pytest

    store=RadarStore(str(tmp_path/"radar.db"))
    with pytest.raises(ValueError,match="Do not put credentials"):
        store.add_source("private","rss",{"url":"https://user:password@example.test/feed"})
