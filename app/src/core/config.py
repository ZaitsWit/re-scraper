from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List, Optional

class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "re"
    #db_user: str = "app"
    #db_password: str = "app"
    scrape_city: str = "sankt-petersburg"
    scrape_interval_min: int = 10
    cian_region_id: int = 1
    cian_deal_type: str = "rent"  # sale | rent
    cian_min_area_m2: float = 0.0
    cian_max_price_rub: Optional[int] = None
    cian_offer_type: str = "flat"
    cian_rooms: List[str] = ["studio", "1", "2"]
    cian_rent_long_only: bool = True
    cian_exclude_rooms: bool = True
    cian_max_pages: int = 3
    cian_rate_limit_ms: int = 1200

    @field_validator("cian_rooms", mode="before")
    @classmethod
    def _split_rooms(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.replace(" ", "").split(",") if s]
        return v

    @property
    def db_dsn(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()
