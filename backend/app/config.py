"""Настройки приложения. Всё берётся из переменных окружения (см. README_BACKEND.md)."""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # папка backend/


def _normalize_db_url(url: str) -> str:
    # Neon/Render/Supabase выдают postgres:// или postgresql://, а SQLAlchemy нужен драйвер
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


@dataclass
class Settings:
    apify_token: str = ""
    admin_token: str = ""                      # без него обновление через API отключено
    database_url: str = "sqlite:///./bxd.db"
    hashtag: str = "BXDBATTLE"
    start: str = "2026-10-01"                  # YYYY-MM-DD, включительно
    end: str = "2026-10-28"                    # YYYY-MM-DD, включительно
    tz: str = "Asia/Almaty"
    max_videos: int = 10                       # сколько последних роликов брать у аккаунта
    batch: int = 20                            # аккаунтов за один запуск скрейпера
    min_refresh_hours: float = 6.0             # защита от случайных повторных платных запусков
    creators_file: str = str(BASE_DIR.parent / "bloggers.csv")
    frontend_dir: str = str(BASE_DIR.parent / "frontend")

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        d = cls()
        return cls(
            apify_token=e("APIFY_TOKEN", d.apify_token),
            admin_token=e("ADMIN_TOKEN", d.admin_token),
            database_url=_normalize_db_url(e("DATABASE_URL", d.database_url)),
            hashtag=e("HASHTAG", d.hashtag).lstrip("#"),
            start=e("PERIOD_START", d.start),
            end=e("PERIOD_END", d.end),
            tz=e("TZ_NAME", d.tz),
            max_videos=int(e("MAX_VIDEOS", d.max_videos)),
            batch=int(e("BATCH_SIZE", d.batch)),
            min_refresh_hours=float(e("MIN_REFRESH_HOURS", d.min_refresh_hours)),
            creators_file=e("CREATORS_FILE", d.creators_file),
            frontend_dir=e("FRONTEND_DIR", d.frontend_dir),
        )
