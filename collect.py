#!/usr/bin/env python3
"""Сборщик сырых данных для ежедневного дайджеста новостей.

Без всякой модели: забирает RSS-ленты, Telegram-каналы (t.me/s/...),
курсы ЦБ, ключевую ставку, индекс Мосбиржи, Brent, биткоин и погоду,
и сохраняет всё в один JSON-файл data/ГГГГ-ММ-ДД.json (+ data/latest.json).

Запуск:  python collect.py [--hours 26] [--out data]
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

MSK = timezone(timedelta(hours=3))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
TIMEOUT = 25
SUMMARY_LIMIT = 600   # символов на анонс — чтобы файл оставался компактным
KEEP_DAYS = 14        # сколько дней хранить старые файлы

ROOT = Path(__file__).resolve().parent


def clean_text(raw: str | None, limit: int = SUMMARY_LIMIT) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- RSS
def fetch_rss(src: dict, since: datetime) -> tuple[list[dict], dict]:
    status = {"source": src["name"], "kind": "rss", "url": src["url"]}
    try:
        resp = get(src["url"])
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"не RSS: {feed.bozo_exception}")
        items = []
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            pub = (datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
                   if t else None)
            if pub and pub < since:
                continue
            title = clean_text(e.get("title"), 300)
            if not title:
                continue
            items.append({
                "source": src["name"],
                "section_hint": src.get("section", ""),
                "title": title,
                "link": e.get("link", ""),
                "published": pub.astimezone(MSK).isoformat() if pub else None,
                "summary": clean_text(e.get("summary") or e.get("description")),
            })
        status.update(ok=True, total_in_feed=len(feed.entries), fresh=len(items))
        return items, status
    except Exception as ex:  # noqa: BLE001 — одна плохая лента не должна ронять сбор
        status.update(ok=False, error=f"{type(ex).__name__}: {ex}"[:300])
        return [], status


# ---------------------------------------------------------------- Telegram
def fetch_telegram(src: dict, since: datetime) -> tuple[list[dict], dict]:
    url = f"https://t.me/s/{src['channel']}"
    status = {"source": src["name"], "kind": "telegram", "url": url}
    try:
        soup = BeautifulSoup(get(url).text, "html.parser")
        items = []
        for msg in soup.select("div.tgme_widget_message"):
            tnode = msg.select_one("time[datetime]")
            pub = datetime.fromisoformat(tnode["datetime"]) if tnode else None
            if pub and pub < since:
                continue
            body = msg.select_one("div.tgme_widget_message_text")
            text = clean_text(str(body) if body else "", 1200)
            if not text:
                continue
            post = msg.get("data-post", "")
            items.append({
                "source": src["name"],
                "section_hint": src.get("section", ""),
                "title": text[:160],
                "link": f"https://t.me/{post}" if post else url,
                "published": pub.astimezone(MSK).isoformat() if pub else None,
                "summary": text,
                "telegram": True,
            })
        status.update(ok=True, fresh=len(items))
        return items, status
    except Exception as ex:  # noqa: BLE001
        status.update(ok=False, error=f"{type(ex).__name__}: {ex}"[:300])
        return [], status


# ---------------------------------------------------------------- Рынки
def markets() -> tuple[dict, list[dict]]:
    out, statuses = {}, []

    def step(name, fn):
        try:
            out[name] = fn()
            statuses.append({"source": name, "kind": "market", "ok": True})
        except Exception as ex:  # noqa: BLE001
            out[name] = None
            statuses.append({"source": name, "kind": "market", "ok": False,
                             "error": f"{type(ex).__name__}: {ex}"[:300]})

    def cbr_rates():
        # Официальные курсы ЦБ (зеркало cbr-xml-daily.ru даёт и предыдущее значение)
        j = get("https://www.cbr-xml-daily.ru/daily_json.js").json()
        res = {"date": j.get("Date"), "source": "https://www.cbr.ru/currency_base/daily/"}
        for code in ("USD", "EUR", "CNY"):
            v = j["Valute"][code]
            res[code] = {"value": round(v["Value"] / v["Nominal"], 4),
                         "previous": round(v["Previous"] / v["Nominal"], 4)}
        return res

    def key_rate():
        soup = BeautifulSoup(get("https://www.cbr.ru/hd_base/KeyRate/").text, "html.parser")
        rows = soup.select("table.data tr")
        for tr in rows:
            tds = [td.get_text(strip=True) for td in tr.select("td")]
            if len(tds) >= 2:
                return {"date": tds[0], "value": float(tds[1].replace(",", ".")),
                        "source": "https://www.cbr.ru/hd_base/KeyRate/"}
        raise ValueError("таблица ключевой ставки не найдена")

    def imoex():
        j = get("https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX.json",
                params={"iss.meta": "off", "iss.only": "marketdata"}).json()
        md = j["marketdata"]
        row = dict(zip(md["columns"], md["data"][0]))
        return {"value": row.get("CURRENTVALUE") or row.get("LASTVALUE"),
                "change_pct": row.get("LASTCHANGEPRC"),
                "time": row.get("UPDATETIME") or row.get("SYSTIME"),
                "source": "https://www.moex.com/ru/index/IMOEX"}

    def brent():
        # Котировка фьючерса Brent со stooq (CSV); при сбое — н/д
        txt = get("https://stooq.com/q/l/", params={"s": "cb.f", "f": "sd2t2ohlc",
                                                     "h": "", "e": "csv"}).text
        lines = [l for l in txt.strip().splitlines() if l]
        hdr, row = lines[0].split(","), lines[1].split(",")
        d = dict(zip(hdr, row))
        return {"date": d.get("Date"), "open": float(d["Open"]), "close": float(d["Close"]),
                "source": "https://stooq.com/q/?s=cb.f"}

    def bitcoin():
        j = get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd",
                        "include_24hr_change": "true"}).json()
        return {"usd": j["bitcoin"]["usd"],
                "change_24h_pct": round(j["bitcoin"].get("usd_24h_change", 0), 2),
                "source": "https://www.coingecko.com/en/coins/bitcoin"}

    for name, fn in [("cbr_rates", cbr_rates), ("key_rate", key_rate), ("imoex", imoex),
                     ("brent", brent), ("bitcoin", bitcoin)]:
        step(name, fn)
    return out, statuses


# ---------------------------------------------------------------- Погода
WMO = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
       45: "туман", 48: "изморозь", 51: "морось", 53: "морось", 55: "сильная морось",
       56: "ледяная морось", 57: "ледяная морось", 61: "небольшой дождь", 63: "дождь",
       65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь", 71: "небольшой снег",
       73: "снег", 75: "сильный снег", 77: "снежная крупа", 80: "ливень", 81: "ливень",
       82: "сильный ливень", 85: "снегопад", 86: "сильный снегопад", 95: "гроза",
       96: "гроза с градом", 99: "гроза с градом"}


def weather(cfg: dict) -> tuple[dict | None, dict]:
    status = {"source": "Погода (Open-Meteo)", "kind": "weather"}
    try:
        j = get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": "Europe/Moscow",
            "forecast_days": 3,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,wind_speed_10m_max,weather_code",
            "wind_speed_unit": "ms"}).json()
        d = j["daily"]
        days = [{"date": d["time"][i],
                 "t_max": d["temperature_2m_max"][i], "t_min": d["temperature_2m_min"][i],
                 "precip_mm": d["precipitation_sum"][i],
                 "precip_prob": d["precipitation_probability_max"][i],
                 "wind_ms": d["wind_speed_10m_max"][i],
                 "conditions": WMO.get(d["weather_code"][i], str(d["weather_code"][i]))}
                for i in range(len(d["time"]))]
        status["ok"] = True
        return {"place": cfg["name"], "days": days, "source": "https://open-meteo.com"}, status
    except Exception as ex:  # noqa: BLE001
        status.update(ok=False, error=f"{type(ex).__name__}: {ex}"[:300])
        return None, status


# ---------------------------------------------------------------- Main
def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = it["link"].split("?")[0].rstrip("/") or it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def prune(out_dir: Path, today: datetime) -> None:
    limit = (today - timedelta(days=KEEP_DAYS)).date()
    for f in out_dir.glob("????-??-??.json"):
        try:
            if datetime.strptime(f.stem, "%Y-%m-%d").date() < limit:
                f.unlink()
        except ValueError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=26, help="окно свежести, часов")
    ap.add_argument("--out", default=str(ROOT / "data"))
    ap.add_argument("--sources", default=str(ROOT / "sources.yaml"))
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.sources).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    started = time.time()

    jobs = [(fetch_rss, s) for s in cfg.get("rss", [])] + \
           [(fetch_telegram, s) for s in cfg.get("telegram", [])]
    items, statuses = [], []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for its, st in pool.map(lambda j: j[0](j[1], since), jobs):
            items += its
            statuses.append(st)

    mk, mk_status = markets()
    wx, wx_status = weather(cfg["weather"]) if cfg.get("weather") else (None, None)
    statuses += mk_status + ([wx_status] if wx_status else [])

    items = dedupe(items)
    items.sort(key=lambda x: x["published"] or "", reverse=True)

    today_msk = now.astimezone(MSK)
    result = {
        "generated_at": today_msk.isoformat(timespec="seconds"),
        "window_hours": args.hours,
        "counts": {"items": len(items),
                   "sources_ok": sum(1 for s in statuses if s.get("ok")),
                   "sources_failed": sum(1 for s in statuses if not s.get("ok"))},
        "markets": mk,
        "weather": wx,
        "items": items,
        "status": statuses,
        "elapsed_sec": round(time.time() - started, 1),
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=1)
    (out_dir / f"{today_msk:%Y-%m-%d}.json").write_text(payload, encoding="utf-8")
    (out_dir / "latest.json").write_text(payload, encoding="utf-8")
    prune(out_dir, today_msk)

    failed = [s for s in statuses if not s.get("ok")]
    print(f"Материалов: {len(items)}; источников ок: {result['counts']['sources_ok']}, "
          f"с ошибкой: {len(failed)}; {result['elapsed_sec']} с")
    for s in failed:
        print(f"  ✗ {s['source']}: {s.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
