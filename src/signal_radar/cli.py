from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC

from dotenv import load_dotenv

from .core import HttpClient, RadarStore


def render_digest(rows)->str:
    if not rows:return "Новых сигналов нет."
    blocks=[]
    for row in rows:
        pub=f"\nОпубликовано: {row['published_at']}" if row["published_at"] else ""
        blocks.append(f"[{row['score']}] {row['title']}\nИсточник: {row['source_name'] if 'source_name' in row else row['source_id']}\nПочему: {row['why']}{pub}\n{row['canonical_url']}")
    return "\n\n".join(blocks)


async def send_digest(store:RadarStore,token:str,chat_id:str)->str:
    from aiogram import Bot
    from aiogram.exceptions import TelegramRetryAfter
    rows=store.pending_delivery()
    if not rows:return "Новых сигналов нет."
    if not store.delivery_allowed():return "Пауза между отправками ещё не прошла; сообщения не отправлены."
    # Keep one irreversible send under Telegram's message limit; remaining items stay pending.
    chosen=[]; text=""
    for row in rows:
        block=render_digest([row])
        candidate=(text+"\n\n"+block).strip()
        if len(candidate)>3800:break
        chosen.append(row);text=candidate
    claimed=[]
    for row in chosen:
        if store.reserve_delivery(row["id"]):claimed.append(row["id"])
    if not claimed:return "Эти элементы уже зарезервированы для доставки."
    bot=Bot(token)
    try:
        # Telegram explicitly rejects 429 messages, so bounded Retry-After retries are safe.
        for attempt in range(3):
            try:
                await bot.send_message(chat_id,text)
                break
            except TelegramRetryAfter as exc:
                if attempt==2 or exc.retry_after>60: raise
                await asyncio.sleep(exc.retry_after)
    except Exception as exc:
        for item_id in claimed:store.finish_delivery(item_id,False,type(exc).__name__)
        raise RuntimeError(f"Telegram delivery failed ({type(exc).__name__}); batch is recorded and will not be retried automatically") from exc
    else:
        for item_id in claimed:store.finish_delivery(item_id,True)
    finally: await bot.session.close()
    return f"Отправлено сигналов: {len(claimed)}"


def scheduled_key(now,timezone_name:str,cadence:str,send_hour:int,quiet_start:int,quiet_end:int)->str|None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:local=now.astimezone(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as exc:raise ValueError("Invalid DIGEST_TIMEZONE; use an IANA name") from exc
    if not 0<=send_hour<=23 or not 0<=quiet_start<=23 or not 0<=quiet_end<=23:raise ValueError("Digest hours must be 0..23")
    quiet=quiet_start<=local.hour<quiet_end if quiet_start<quiet_end else (local.hour>=quiet_start or local.hour<quiet_end)
    if quiet or local.hour!=send_hour:return None
    if cadence=="daily":return f"digest_sent:{local.date().isoformat()}"
    if cadence=="weekly" and local.weekday()==0:
        iso=local.date().isocalendar();return f"digest_sent:{iso.year}-W{iso.week:02d}"
    if cadence=="weekly":return None
    raise ValueError("DIGEST_CADENCE must be daily or weekly")


def main()->None:
    load_dotenv(); p=argparse.ArgumentParser(prog="signal-radar");p.add_argument("--db",default=os.getenv("DATABASE_PATH","signal_radar.sqlite3"))
    sub=p.add_subparsers(dest="command",required=True)
    add=sub.add_parser("add-source");add.add_argument("kind",choices=["rss","github"]);add.add_argument("name");add.add_argument("value",help="RSS URL or GitHub owner/repo")
    rule=sub.add_parser("rule");rule.add_argument("kind",choices=["include","exclude"]);rule.add_argument("keyword")
    sub.add_parser("collect")
    items=sub.add_parser("items");items.add_argument("--limit",type=int,default=20)
    why=sub.add_parser("why");why.add_argument("item_id",type=int)
    interval=sub.add_parser("min-interval");interval.add_argument("seconds",type=int)
    digest=sub.add_parser("digest");digest.add_argument("--send",action="store_true",help="send this digest to configured TELEGRAM_CHAT_ID")
    run=sub.add_parser("run");run.add_argument("--interval",type=int,default=int(os.getenv("POLL_SECONDS","900")))
    args=p.parse_args();store=RadarStore(args.db)
    if args.command=="add-source":
        config={"url":args.value} if args.kind=="rss" else {"repo":args.value}
        store.add_source(args.name,args.kind,config);print(f"Source configured: {args.name} ({args.kind})")
    elif args.command=="rule":store.add_rule(args.kind,args.keyword);print("Rule saved.")
    elif args.command in {"collect","run"}:
        if args.command=="collect":print(json.dumps(store.collect(HttpClient(),os.getenv("GITHUB_TOKEN")),ensure_ascii=False,indent=2))
        else:
            if args.interval<30:raise SystemExit("Polling interval must be at least 30 seconds")
            last_collect=0.0
            while True:
                if time.monotonic()-last_collect>=args.interval:
                    print(json.dumps(store.collect(HttpClient(),os.getenv("GITHUB_TOKEN")),ensure_ascii=False));last_collect=time.monotonic()
                if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
                    from datetime import datetime
                    timezone_name=os.getenv("DIGEST_TIMEZONE","UTC")
                    key=scheduled_key(datetime.now(UTC),timezone_name,os.getenv("DIGEST_CADENCE","daily"),int(os.getenv("DIGEST_HOUR","9")),int(os.getenv("QUIET_HOURS_START","23")),int(os.getenv("QUIET_HOURS_END","8")))
                    if key and not store.digest_marked(key):
                        try:print(asyncio.run(send_digest(store,os.environ["TELEGRAM_BOT_TOKEN"],os.environ["TELEGRAM_CHAT_ID"])))
                        except RuntimeError as exc:print(f"Digest delivery: {exc}")
                        finally:store.mark_digest(key)
                time.sleep(min(args.interval,60))
    elif args.command=="items":
        for row in store.items(args.limit):print(f"#{row['id']} [{row['score']}] {row['title']} — {row['canonical_url']} ({row['source_name']})")
    elif args.command=="why":
        row=store.explain(args.item_id)
        if row is None:raise SystemExit("Item not found")
        print(render_digest([row]))
    elif args.command=="min-interval":store.set_min_interval(args.seconds);print("Minimum interval updated.")
    elif args.command=="digest":
        rows=store.pending_delivery();preview=render_digest(rows);print(preview)
        if args.send:
            token=os.getenv("TELEGRAM_BOT_TOKEN","");chat=os.getenv("TELEGRAM_CHAT_ID","")
            if not token or not chat:raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            print(asyncio.run(send_digest(store,token,chat)))

if __name__=="__main__":main()
