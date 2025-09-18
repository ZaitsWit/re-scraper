# app/src/api/main.py
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import ORJSONResponse
from loguru import logger
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.core.config import settings
from src.db.session import SessionLocal, init_db
from src.db.models import Listing, PriceSnapshot
from src.scheduler.jobs import job_scrape_city

app = FastAPI(default_response_class=ORJSONResponse, title="RE Scraper MVP")

# -------- Helpers --------
async def run_job(coro_fn, *args, **kwargs):
    async with SessionLocal() as db:  # type: AsyncSession
        await coro_fn(db, *args, **kwargs)

async def scheduled_scrape_city():
    await run_job(job_scrape_city, settings.scrape_city)

# -------- Startup / Scheduler --------
@app.on_event("startup")
async def on_startup():
    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_scrape_city,  # ← напрямую корутину
        IntervalTrigger(minutes=settings.scrape_interval_min),
        id="scrape_city",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # стартуем сразу
        misfire_grace_time=60,  # 1 мин пропуска
    )
    scheduler.start()
    app.state.scheduler = scheduler

    logger.info(
        f"Scheduler started; added 'scrape_city' every {settings.scrape_interval_min} min "
        f"for city={settings.scrape_city}"
    )

# -------- Endpoints --------
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/jobs")
def list_jobs():
    jobs = []
    for j in app.state.scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run_time": j.next_run_time,
            "trigger": str(j.trigger),
        })
    return jobs

@app.post("/jobs/scrape/cian")
async def trigger_cian(background_tasks: BackgroundTasks):
    # Мгновенно пинаем задачу не дожидаясь интервала
    background_tasks.add_task(run_job, job_scrape_city, settings.scrape_city)
    return {"queued": True}

@app.get("/listings")
async def list_listings(limit: int = 50):
    async with SessionLocal() as db:
        res = await db.execute(
            select(Listing).order_by(desc(Listing.updated_at)).limit(limit)
        )
        rows: List[Listing] = res.scalars().all()
        return [
            {
                "id": r.id, "source": r.source, "external_id": r.external_id,
                "title": r.title, "address": r.address,
                "rooms": r.rooms, "area_m2": r.area_m2, "floor": r.floor, "floors_total": r.floors_total,
                "price_rub": r.price_rub, "price_per_m2": r.price_per_m2,
                "url": r.url, "active": r.active, "updated_at": r.updated_at
            } for r in rows
        ]

@app.get("/listings/{listing_id}/history")
async def price_history(listing_id: int):
    async with SessionLocal() as db:
        res = await db.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.listing_id == listing_id)
            .order_by(PriceSnapshot.ts)
        )
        snaps: List[PriceSnapshot] = res.scalars().all()
        return [{"ts": s.ts, "price_rub": s.price_rub, "price_per_m2": s.price_per_m2} for s in snaps]
