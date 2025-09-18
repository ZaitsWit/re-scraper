# app/src/scrapers/avito.py
import asyncio
import httpx
import re
import os
import json
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from src.core.config import settings

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEBUG_DIR = Path("/app/_debug")
DEBUG_DIR.mkdir(exist_ok=True, parents=True)

def _headers() -> Dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.avito.ru/",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

class FetchError(Exception): pass

ANTI_BOT_MARKERS = [
    "подозрительная активность", "вы робот", "captcha", "Доступ ограничен",
    "Похоже, вы слишком часто", "Пожалуйста, подождите"
]

@retry(reraise=True,
       stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=0.8, min=1, max=8),
       retry=retry_if_exception_type(FetchError))
async def _fetch_page(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, timeout=25.0, follow_redirects=True)

    # сохраняем дамп всегда (для отладки)
    if os.getenv("DUMP_HTML", "0") == "1":
        m = re.search(r"[?&]p=(\d+)", str(r.url))
        p = m.group(1) if m else "1"
        fname = DEBUG_DIR / f"avito_p{p}.html"
        try:
            fname.write_text(r.text, encoding="utf-8")
            logger.info(f"[AVITO] saved HTML -> {fname}")
        except Exception as e:
            logger.warning(f"[AVITO] failed to save HTML: {e}")

    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")

    low = r.text.lower()
    if any(m in low for m in ANTI_BOT_MARKERS) or len(r.text) < 3000:
        logger.warning("[AVITO] anti-bot/short heuristics triggered, trying to parse anyway")
    return r.text


# --- Вспомогательные функции ---
def _parse_int(text: Optional[str]) -> Optional[int]:
    if not text: return None
    s = re.sub(r"[^\d]", "", text)
    return int(s) if s else None

def _parse_float_m2(text: Optional[str]) -> Optional[float]:
    if not text: return None
    m = re.search(r"(\d+[.,]?\d*)\s*м²", text)
    if not m: m = re.search(r"(\d+[.,]?\d*)\s*m2", text, flags=re.I)
    if not m: return None
    return float(m.group(1).replace(",", "."))

def _price_per_m2(price: Optional[int], area: Optional[float]) -> Optional[float]:
    return (price / area) if (price and area and area > 0) else None

def _extract_external_id(url: Optional[str]) -> Optional[str]:
    if not url: return None
    m = re.search(r"/(\d+)(?:\?|$|/)", url)
    if m: return m.group(1)
    return None

def _detect_rent_period(text: str) -> str:
    low = (text or "").lower()
    if any(x in low for x in ["в сутки", "за сутки", "/сут", "сутки", "в день", "/день", "за день"]):
        return "daily"
    if any(x in low for x in ["в месяц", "в мес", "/мес", "мес.", "месяц"]):
        return "monthly"
    return "unknown"

def _is_room_listing(title: str | None, context: str | None) -> bool:
    t = f"{title or ''} {context or ''}".lower()
    if "квартира" in t or "студия" in t:
        return False
    if "комната в" in t or "комн. в" in t or "в подселени" in t or "подселение" in t:
        return True
    if re.search(r"\bкомнат[аеы]\b", t) and "комнатн" not in t:
        return True
    return False

def _parse_from_jsonld(html: str) -> List[Dict]:
    doc = HTMLParser(html)
    scripts = doc.css('script[type="application/ld+json"]')
    out: List[Dict] = []

    for node in scripts:
        try:
            data = json.loads(node.text())
        except Exception:
            continue

        products: List[dict] = []
        if isinstance(data, dict):
            if data.get("@type") == "Product":
                products = [data]
            elif isinstance(data.get("@graph"), list):
                products = [g for g in data["@graph"] if isinstance(g, dict) and g.get("@type") == "Product"]
        elif isinstance(data, list):
            products = [g for g in data if isinstance(g, dict) and g.get("@type") == "Product"]

        for product in products:
            offers_block = product.get("offers") or {}
            offers = offers_block.get("offers") or []
            for off in offers:
                if not isinstance(off, dict):
                    continue
                url = off.get("url")
                title = off.get("name")
                price_rub = _parse_int(off.get("price"))
                if not url or not title:
                    continue

                ctx = title
                area_m2 = _parse_float_m2(ctx)
                floor = floors_total = None
                mfl = re.search(r"(\d+)\s*/\s*(\d+)\s*эт", ctx, flags=re.I)
                if mfl:
                    floor, floors_total = int(mfl.group(1)), int(mfl.group(2))
                rooms = None
                if re.search(r"\bстуд", ctx, flags=re.I): rooms = 0
                mrooms = re.search(r"(\d+)\s*[-–]?\s*к", ctx, flags=re.I)
                if mrooms: rooms = int(mrooms.group(1))

                out.append({
                    "source": "avito",
                    "external_id": _extract_external_id(url),
                    "title": title,
                    "address": None,
                    "rooms": rooms,
                    "area_m2": area_m2,
                    "floor": floor,
                    "floors_total": floors_total,
                    "price_rub": price_rub,
                    "price_per_m2": _price_per_m2(price_rub, area_m2),
                    "url": url,
                    "_rent_period": "monthly",
                    "_is_room": _is_room_listing(title, ctx),
                })

    return [c for c in out if c.get("url")]


# --- Основной парсер (JSON-LD приоритет, DOM fallback) ---
def _parse_cards(html: str) -> List[Dict]:
    # можно оставить твой старый DOM-парсер как запасной
    return []

# --- Fetch Avito ---
async def fetch_avito(city: str) -> List[Dict]:
    base = os.getenv("AVITO_SEARCH_URL")
    if not base:
        logger.warning("[AVITO] AVITO_SEARCH_URL is empty — skip")
        return []

    max_pages = max(1, int(os.getenv("AVITO_MAX_PAGES", "1")))
    rate_sleep = int(os.getenv("AVITO_RATE_LIMIT_MS", "1500")) / 1000.0

    items: List[Dict] = []
    async with httpx.AsyncClient(headers=_headers()) as client:
        for p in range(1, max_pages + 1):
            url = base if p == 1 else f"{base}&p={p}"
            logger.info(f"[AVITO] GET {url}")
            try:
                html = await _fetch_page(client, url)
            except Exception as e:
                logger.warning(f"[AVITO] fetch failed p={p}: {e}")
                break

            page_items = _parse_from_jsonld(html)
            if not page_items:
                page_items = _parse_cards(html)
            logger.info(f"[AVITO] parsed {len(page_items)} cards on p={p}")
            items.extend(page_items)
            if len(page_items) < 5:
                break
            await asyncio.sleep(rate_sleep)

    # dedup и фильтры такие же, как у CIAN
    seen = set(); deduped: List[Dict] = []
    for it in items:
        key = (it.get("external_id"), it.get("url"))
        if key in seen: continue
        seen.add(key); deduped.append(it)

    if settings.cian_rent_long_only:
        before = len(deduped)
        deduped = [it for it in deduped if it.get("_rent_period") != "daily"]
        logger.info(f"[AVITO] long-rent filter: kept {len(deduped)}, removed {before - len(deduped)} daily")

    if settings.cian_exclude_rooms:
        before = len(deduped)
        deduped = [it for it in deduped if not it.get("_is_room")]
        logger.info(f"[AVITO] rooms filter: kept {len(deduped)}, removed {before - len(deduped)} rooms")

    filtered: List[Dict] = []
    for it in deduped:
        area_ok = (it.get("area_m2") is not None and it["area_m2"] >= settings.cian_min_area_m2)
        price_ok = (it.get("price_rub") is not None and it["price_rub"] <= (settings.cian_max_price_rub or 10**12))
        if area_ok and price_ok:
            filtered.append(it)

    for it in filtered:
        it.pop("_rent_period", None)
        it.pop("_is_room", None)

    logger.info(f"[AVITO] filtered: {len(filtered)} / parsed: {len(items)}")
    return filtered
