from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from .models import Base
from src.core.config import settings
from src.core.vault import vault_client
from loguru import logger

def make_dsn():
    creds = vault_client.get_db_creds()
    user = creds.username
    pwd = creds.password
    host = settings.db_host
    port = settings.db_port
    name = settings.db_name
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{name}"

dsn = make_dsn()
engine = create_async_engine(dsn, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
