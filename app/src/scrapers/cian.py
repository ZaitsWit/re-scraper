import asyncio
import httpx
import math
import re
import time
from typing import Dict, List, Optional
from loguru import logger
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from src.core.config import settings
import os
from pathlib import Path

DEBUG_DIR = Path("/app/_debug")
DEBUG_DIR.mkdir(exist_ok=True, parents=True)

ANTI_BOT_MARKERS = [
    "вы робот", "подтвердите, что вы не робот", "captcha", "подозрительная активность",
    "Too Many Requests", "Доступ временно ограничен"
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ROOM_MAP = {
    "studio": "1",  # упростим до «кол-во комнат» для нашей модели
    "1": "1", "2": "2", "3": "3", "4": "4", "5+": "5"
}

def _detect_rent_period(text: str) -> str:
    """Возвращает 'monthly' | 'daily' | 'unknown' по текстовым маркерам."""
    low = (text or "").lower()
    daily_markers = ["посуточ", "в сутки", "за сутки", "/сут", "сутки", "в день", "/день", "за день"]
    monthly_markers = ["в месяц", "в мес", "/мес", "мес.", "месяц"]
    if any(m in low for m in daily_markers):
        return "daily"
    if any(m in low for m in monthly_markers):
        return "monthly"
    return "unknown"

def _is_room_listing(title: str | None, summary_txt: str | None) -> bool:
    """
    Возвращает True, если объявление про комнату (а не про квартиру/студию).
    Эвристики:
      - если встречается отдельное слово "комната/комнаты" или фразы "комната в", "в подселении"
      - и при этом нигде рядом нет "квартира" или "студия"
    Не цепляем "1-комнатная квартира" (потому что это "комнатнАЯ", а не "комната").
    """
    t = f"{title or ''} {summary_txt or ''}".lower()

    # если явно указано "квартира" или "студия" — считаем, что это не комната
    if "квартира" in t or "студия" in t:
        return False

    # явные маркеры комнат
    if re.search(r"\bкомнат[аеы]\b", t):  # "комната", "комнаты"
        return True
    if "комната в" in t or "комн. в" in t or "в подселени" in t:
        return True

    # некоторые карточки пишут "Сдаётся комната ..." — ловим и это
    if "сдаётся комната" in t or "сдается комната" in t:
        return True

    return False

def _parse_int(txt: Optional[str]) -> Optional[int]:
    if not txt: return None
    nums = re.sub(r"[^\d]", "", txt)
    return int(nums) if nums else None

def _parse_float(txt: Optional[str]) -> Optional[float]:
    if not txt: return None
    txt = txt.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", txt)
    return float(m.group(1)) if m else None

def _price_per_m2(price: Optional[int], area: Optional[float]) -> Optional[float]:
    if price and area and area > 0:
        return price / area
    return None

def _headers() -> Dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

def _build_search_url(page: int) -> str:
    # Старый стабильный SSR-эндпоинт у CIAN: cat.php с query-параметрами
    # Пример: https://www.cian.ru/cat.php?deal_type=sale&engine_version=2&offer_type=flat&region=1&room1=1&room2=1&room9=1&p=2
    params = {
        "deal_type": settings.cian_deal_type,       # sale|rent
        "engine_version": "2",
        "offer_type": settings.cian_offer_type,     # flat
        "region": str(settings.cian_region_id),     # 1 = Москва
        "p": str(page),                             # страница
    }
    # Комнаты: studio -> room0, 1 -> room1, 2 -> room2, ...
    room_flags = []
    for r in settings.cian_rooms:
        key = None
        if r == "studio": key = "room0"
        elif r == "5+": key = "room5"
        else:
            if r.isdigit(): key = f"room{r}"
        if key:
            room_flags.append(f"{key}=1")

    base = "https://www.cian.ru/cat.php"
    query = "&".join([f"{k}={v}" for k,v in params.items()] + room_flags)
    return f"{base}?{query}"

class FetchError(Exception): pass

@retry(reraise=True,
       stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=0.8, min=1, max=10),
       retry=retry_if_exception_type(FetchError))
async def _fetch_page(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, timeout=20.0)
    from loguru import logger
    logger.debug(f"[CIAN] resp: {r.status_code}, len={len(r.text)} for {r.url}")

    # если включён дамп
    if os.getenv("DUMP_HTML", "0") == "1":
        fname = DEBUG_DIR / f"cian_p{r.request.url.params.get('p', '1')}.html"
        try:
            fname.write_text(r.text, encoding="utf-8")
            logger.info(f"[CIAN] saved HTML -> {fname}")
        except Exception as e:
            logger.warning(f"[CIAN] failed to save HTML: {e}")

    # антибот-эвристика
    low = r.text.lower()
    if any(m in low for m in ANTI_BOT_MARKERS):
        raise FetchError("Anti-bot/verification page detected")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")
    # Небольшая защита от антибота: иногда отдают пустую/укороченную страницу
    if len(r.text) < 5000:
        raise FetchError("Suspiciously short HTML")
    return r.text

def _parse_cards(html: str) -> List[Dict]:
    doc = HTMLParser(html)
    cards = []

    # Несколько вариантов контейнеров карточек:
    card_nodes = []
    for css in [
        '[data-cian-id]',                                # старый вариант
        'article[data-name="CardComponent"]',            # частый вариант
        'div[data-name="CardComponent"]',
        'div[data-testid="offer-card"]',
        'div[data-mark="Offer"]',
    ]:
        card_nodes.extend(doc.css(css))
    # dedup
    seen = set()
    uniq_nodes = []
    for n in card_nodes:
        ident = (n.tag, n.attributes.get("data-cian-id") or n.attributes.get("data-name") or id(n))
        if ident in seen:
            continue
        seen.add(ident)
        uniq_nodes.append(n)

    for card in uniq_nodes:
        try:
            # Заголовок (несколько вариантов)
            title_node = (
                card.css_first('[data-mark="OfferTitle"]')
                or card.css_first('a[data-name*="LinkArea"]')
                or card.css_first('[data-testid="card-title"]')
                or card.css_first('a[href*="/sale/"]')   # fallback
            )
            title = title_node.text(strip=True) if title_node else None

            # Ссылка
            link = (
                card.css_first("a[href*='/sale/']")
                or card.css_first("a[href*='/rent/']")
                or card.css_first('a[data-name*="LinkArea"]')
            )
            url = link.attributes.get("href") if link else None
            if url and url.startswith("//"):
                url = "https:" + url

            # external_id из ссылки
            external_id = None
            if url:
                m = re.search(r"/(\d+)/?$", url)
                if m: external_id = m.group(1)

            # Адрес
            addr_node = (
                card.css_first('[data-name="GeoLabel"]')
                or card.css_first('[data-testid="address"]')
                or card.css_first('[data-mark="OfferSummary"]')
            )
            address = addr_node.text(strip=True) if addr_node else None

            # Summary текстом — вытаскиваем площадь и этажи регулярками
            summary_txt = " ".join(n.text(strip=True) for n in card.css('[data-mark="OfferSummary"]')) or card.text(strip=True)
            area_m2 = _parse_float(re.search(r"(\d+[,.]?\d*)\s*м²", summary_txt) and re.search(r"(\d+[,.]?\d*)\s*м²", summary_txt).group(0))
            floor = floors_total = None
            mfl = re.search(r"(\d+)\s*/\s*(\d+)", summary_txt)
            if mfl:
                floor = int(mfl.group(1)); floors_total = int(mfl.group(2))

            # Комнатность
            rooms = None
            mrooms = re.search(r"(студия|\d+)[-\s]*к", (title or "") + " " + summary_txt, flags=re.IGNORECASE)
            if mrooms:
                rooms = 0 if "студ" in mrooms.group(1).lower() else int(re.search(r"\d+", mrooms.group(1)).group(0))

            # Цена (несколько вариантов селекторов)
            price_node = (
                    card.css_first('[data-mark="MainPrice"]')
                    or card.css_first('[data-testid="price"]')
                    or card.css_first('span:has(> span[data-mark="MainPrice"])')
            )
            price_text = price_node.text(strip=True) if price_node else ""
            price_rub = _parse_int(price_text) if price_text else None

            # Контекст для определения периода (в т.ч. «в месяц»/«в сутки»)
            context_txt = " ".join([
                price_text or "",
                title or "",
                summary_txt or "",
                card.text(strip=True) or "",
            ])

            rent_period = _detect_rent_period(context_txt)
            is_room = _is_room_listing(title, summary_txt)

            cards.append({
                "source": "cian",
                "external_id": external_id,
                "title": title,
                "address": address,
                "rooms": rooms,
                "area_m2": area_m2,
                "floor": floor,
                "floors_total": floors_total,
                "price_rub": price_rub,
                "price_per_m2": _price_per_m2(price_rub, area_m2),
                "url": url,
                "_is_room": is_room,
                "_rent_period": rent_period,  # временно, для фильтрации
            })
        except Exception as e:
            logger.debug(f"[CIAN] card parse error: {e}")

    return [c for c in cards if c.get("url")]


async def fetch_cian(city: str) -> List[Dict]:
    # city пока не используем — CIAN фильтруем по region_id
    max_pages = max(1, settings.cian_max_pages)
    out: List[Dict] = []
    rate_sleep = settings.cian_rate_limit_ms / 1000.0

    async with httpx.AsyncClient(headers=_headers(), follow_redirects=True) as client:
        for p in range(1, max_pages + 1):
            url = _build_search_url(page=p)
            logger.info(f"[CIAN] GET {url}")
            try:
                html = await _fetch_page(client, url)
            except Exception as e:
                logger.warning(f"[CIAN] fetch failed p={p}: {e}")
                break

            items = _parse_cards(html)
            logger.info(f"[CIAN] parsed {len(items)} cards on p={p}")
            out.extend(items)

            # эвристика окончания: если карточек мало — выходим
            if len(items) < 5:
                break

            await asyncio.sleep(rate_sleep)


    # dedup по (external_id, url)
    seen_ids = set()
    deduped = []
    for item in out:
        key = (item.get("external_id"), item.get("url"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deduped.append(item)

    # ▶ Фильтр: только долгосрочная аренда (исключаем «посуточно»)
    if settings.cian_deal_type == "rent" and settings.cian_rent_long_only:
        before = len(deduped)
        deduped = [it for it in deduped if it.get("_rent_period") != "daily"]
        logger.info(f"[CIAN] long-rent filter: {len(deduped)} kept, {before - len(deduped)} daily removed")

    # Удаляем служебное поле перед возвратом
    for it in deduped:
        it.pop("_is_room", None)
        it.pop("_rent_period", None)

    # ▶ Фильтр "только квартиры/студии"
    if settings.cian_exclude_rooms:
        before = len(deduped)
        deduped = [it for it in deduped if not it.get("_is_room")]
        logger.info(f"[CIAN] rooms filter: kept {len(deduped)}, removed {before - len(deduped)} rooms")

    # ▶ Клиентская фильтрация по площади и цене
    filtered = []
    for item in deduped:
        area_ok = (item.get("area_m2") is not None and item["area_m2"] >= settings.cian_min_area_m2)
        price_ok = True
        if settings.cian_max_price_rub is not None:
            price_ok = (item.get("price_rub") is not None and item["price_rub"] <= settings.cian_max_price_rub)
        if area_ok and price_ok:
            filtered.append(item)

    logger.info(f"[CIAN] filtered: {len(filtered)} / parsed: {len(deduped)} "
                f"(minArea={settings.cian_min_area_m2}, maxPrice={settings.cian_max_price_rub}, longOnly={settings.cian_rent_long_only})")
    return filtered

