"""Модели базы данных (SQLAlchemy 2.0) и вспомогательные функции."""
import csv
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    """Все даты в БД хранятся как UTC без tzinfo (одинаково для SQLite и PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (UniqueConstraint("name", "tiktok", "instagram", name="uq_creator"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    tiktok: Mapped[str] = mapped_column(String(100), default="")
    instagram: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # есть ли в текущем списке
    # Состояние последнего сбора по каждой платформе
    tt_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ig_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    tt_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ig_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tt_warn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ig_warn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    videos: Mapped[list["Video"]] = relationship(back_populates="creator")


class Video(Base):
    __tablename__ = "videos"
    # Уникальность публикации: платформа + ID ролика (ТЗ, п. 4.3)
    __table_args__ = (UniqueConstraint("platform", "platform_video_id", name="uq_video"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(10))  # "tt" или "ig"
    platform_video_id: Mapped[str] = mapped_column(String(100))
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id"), index=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    views: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # NULL = нет данных, не ноль
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # False, если ролик пропал у автора
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    creator: Mapped[Creator] = relationship(back_populates="videos")


class Snapshot(Base):
    """История просмотров ролика (по одной записи на каждое успешное обновление)."""
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    views: Mapped[int] = mapped_column(BigInteger)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running / ok / partial / failed
    cost_usd: Mapped[Optional[float]] = mapped_column(nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CreatorTotal(Base):
    """Итог блогера на момент каждого успешного обновления (для стрелок движения и прироста)."""
    __tablename__ = "creator_totals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id"), index=True)
    total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    rank: Mapped[int] = mapped_column(Integer)


def make_session_factory(database_url: str):
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, pool_pre_ping=True, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def clean_username(v: Optional[str]) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    v = v.rstrip("/").split("/")[-1].split("?")[0]
    return v.lstrip("@")


def import_creators(session, csv_path: str) -> int:
    """Загружает список блогеров из CSV (name,tiktok,instagram). Тех, кого в файле нет, скрывает."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    keep = set()
    for r in rows:
        name = (r.get("name") or "").strip()
        tt, ig = clean_username(r.get("tiktok")), clean_username(r.get("instagram"))
        if not name or not (tt or ig):
            continue
        c = session.scalar(select(Creator).where(
            Creator.name == name, Creator.tiktok == tt, Creator.instagram == ig))
        if c is None:
            c = Creator(name=name, tiktok=tt, instagram=ig)
            session.add(c)
            session.flush()
        c.active = True
        keep.add(c.id)
    for c in session.scalars(select(Creator)):
        if c.id not in keep:
            c.active = False
    session.commit()
    return len(keep)
