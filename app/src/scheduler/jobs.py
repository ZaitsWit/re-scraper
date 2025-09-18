from datetime import datetime
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Listing, PriceSnapshot
from src.scrapers.cian import fetch_cian
from src.scrapers.avito import fetch_avito


async def upsert_listing(db: AsyncSession, payload: dict) -> None:
    src = payload["source"]; ext = payload.get("external_id")
    result = await db.execute(
        select(Listing).where(Listing.source == src, Listing.external_id == ext)
    )
    obj = result.scalar_one_or_none()
    if obj is None:
        obj = Listing(**payload)
        if payload.get("area_m2") and payload.get("price_rub"):
            obj.price_per_m2 = payload["price_rub"] / payload["area_m2"]
        db.add(obj)
        await db.flush()
        db.add(PriceSnapshot(listing_id=obj.id, price_rub=obj.price_rub, price_per_m2=obj.price_per_m2))
        logger.info(f"Inserted listing {src}:{ext}")
    else:
        changed = False
        # обновим цену и снимок при изменении
        new_price = payload.get("price_rub")
        if new_price and new_price != obj.price_rub:
            obj.price_rub = new_price
            obj.price_per_m2 = new_price / (payload.get("area_m2") or obj.area_m2 or 1)
            db.add(PriceSnapshot(listing_id=obj.id, price_rub=obj.price_rub, price_per_m2=obj.price_per_m2))
            changed = True
        # можно обновлять и другие поля
        if changed:
            logger.info(f"Updated price for {src}:{ext}")

async def job_scrape_city(db: AsyncSession, city: str):
    logger.info(f"Run job: scrape city={city} @ {datetime.utcnow().isoformat()}Z")
    # CIAN
    for item in await fetch_cian(city):
        await upsert_listing(db, item)
    # AVITO
    for item in await fetch_avito(city):
        await upsert_listing(db, item)
    await db.commit()
