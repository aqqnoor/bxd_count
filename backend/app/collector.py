"""Сбор данных: запускает скрейперы Apify по списку блогеров и записывает результат в БД."""
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import apify_source as src
from .config import Settings
from .db import Creator, Run, Snapshot, Video, utcnow

log = logging.getLogger("bxd.collector")
_start_lock = threading.Lock()
STALE_RUN_AFTER = timedelta(hours=2)  # «зависший» запуск перестаёт блокировать новые


class RefreshRefused(Exception):
    """Запуск не разрешён (уже идёт сбор или не прошёл интервал между платными запусками)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def running_run(session):
    r = session.scalar(select(Run).where(Run.status == "running").order_by(Run.id.desc()))
    if r and utcnow() - r.started_at < STALE_RUN_AFTER:
        return r
    return None


def start_run(session_factory, settings: Settings, force: bool = False) -> int:
    """Проверяет, можно ли запускать сбор, и создаёт запись о запуске."""
    with _start_lock, session_factory() as s:
        if running_run(s):
            raise RefreshRefused(409, "Сбор уже идёт. Подождите его окончания.")
        if not force:
            last = s.scalar(select(Run).where(Run.status.in_(("ok", "partial")))
                            .order_by(Run.id.desc()))
            if last and last.finished_at:
                wait = timedelta(hours=settings.min_refresh_hours) - (utcnow() - last.finished_at)
                if wait > timedelta(0):
                    mins = int(wait.total_seconds() // 60) + 1
                    raise RefreshRefused(
                        429, f"Прошлое обновление было недавно. Следующее без force через {mins} мин.")
        run = Run(status="running")
        s.add(run)
        s.commit()
        return run.id


def _short(e) -> str:
    return str(e)[:300]


def execute_run(run_id: int, session_factory, settings: Settings, client_factory=src.get_client):
    """Выполняет сбор. Никогда не бросает исключение наружу: итог пишется в таблицу runs."""
    cost_total, failed_batches, ok_batches = 0.0, 0, 0
    try:
        tz = ZoneInfo(settings.tz)
        start = datetime.fromisoformat(settings.start).replace(tzinfo=tz)
        end = (datetime.fromisoformat(settings.end) + timedelta(days=1)
               - timedelta(seconds=1)).replace(tzinfo=tz)
        client = client_factory(settings.apify_token)
        run_started = utcnow()

        for platform in ("tt", "ig"):
            actor, parser, label = src.ACTORS[platform]
            with session_factory() as s:
                creators = [(c.id, getattr(c, "tiktok" if platform == "tt" else "instagram"))
                            for c in s.scalars(select(Creator).where(Creator.active.is_(True)))]
            creators = [(cid, u) for cid, u in creators if u]

            for i in range(0, len(creators), settings.batch):
                chunk = creators[i:i + settings.batch]
                users = [u for _, u in chunk]
                try:
                    items, cost = src.run_actor(
                        client, actor, src.build_input(platform, users, settings.max_videos))
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: запуск не удался: %s", label, e)
                    failed_batches += 1
                    with session_factory() as s:
                        for cid, _ in chunk:
                            setattr(s.get(Creator, cid), f"{platform}_error", f"{label}: {_short(e)}")
                        s.commit()
                    continue
                ok_batches += 1
                cost_total += cost or 0.0

                by_owner, error_items = {}, []
                for it in items:
                    if it.get("error") or it.get("errorDescription"):
                        error_items.append(str(it.get("error") or it.get("errorDescription")))
                        continue
                    p = parser(it, tz)
                    if p["owner"]:
                        by_owner.setdefault(p["owner"], []).append(p)

                with session_factory() as s:
                    for cid, user in chunk:
                        _store_creator(s, cid, user, platform, label, by_owner.get(user.lower(), []),
                                       error_items, settings, start, end, run_started)
                    s.commit()

        status = "failed" if ok_batches == 0 and failed_batches else ("partial" if failed_batches else "ok")
        message = f"Успешных пакетов: {ok_batches}, неудачных: {failed_batches}"
    except Exception as e:  # noqa: BLE001
        log.exception("Сбор завершился ошибкой")
        status, message = "failed", _short(e)

    with session_factory() as s:
        run = s.get(Run, run_id)
        run.status, run.message = status, message
        run.cost_usd, run.finished_at = round(cost_total, 4), utcnow()
        s.commit()
    log.info("Сбор %s завершён: %s, расход ≈ $%.3f", run_id, status, cost_total)


def _store_creator(s, creator_id, user, platform, label, parsed, error_items, settings,
                   start, end, run_started):
    creator = s.get(Creator, creator_id)
    now = utcnow()

    if not parsed:
        err = next((e for e in error_items if user.lower() in e.lower()), None)
        if err:
            setattr(creator, f"{platform}_error", f"{label}: {err[:250]}")
            return
        # Скрейпер ничего не вернул: аккаунт пуст, закрыт или не найден. Не считаем это сбоем,
        # но просим проверить.
        setattr(creator, f"{platform}_error", None)
        setattr(creator, f"{platform}_ok_at", now)
        setattr(creator, f"{platform}_warn",
                f"{label}: роликов не получено (аккаунт пуст, закрыт или не найден)")
        return

    for p in parsed:
        if p["published"] is None or not (start <= p["published"] <= end):
            continue
        if not (src.has_tag(p["text"], settings.hashtag) or settings.hashtag.lower() in p["tags"]):
            continue
        pub = _to_utc_naive(p["published"])
        v = s.scalar(select(Video).where(Video.platform == platform,
                                         Video.platform_video_id == p["id"]))
        if v is None:
            v = Video(platform=platform, platform_video_id=p["id"], creator_id=creator.id,
                      url=p["url"], published_at=pub, views=p["views"], first_seen_at=now)
            s.add(v)
            s.flush()
        else:
            v.url, v.published_at = p["url"] or v.url, pub
            if p["views"] is not None:  # пустое значение не затирает последнее известное
                v.views = p["views"]
        v.active, v.last_seen_at = True, now
        if p["views"] is not None:
            s.add(Snapshot(video_id=v.id, views=p["views"], collected_at=now))

    # Ролики, которые попадали в окно сбора, но больше не вернулись, считаем удалёнными автором
    dates = [_to_utc_naive(p["published"]) for p in parsed if p["published"]]
    warn = []
    if dates:
        coverage_from = min(dates)
        for v in s.scalars(select(Video).where(
                Video.creator_id == creator.id, Video.platform == platform,
                Video.active.is_(True), Video.published_at >= coverage_from,
                Video.last_seen_at < run_started)):
            v.active = False
        if len(parsed) >= settings.max_videos and min(p["published"] for p in parsed if p["published"]) >= start:
            warn.append(f"{label}: достигнут лимит {settings.max_videos} роликов, возможно есть ещё")
    setattr(creator, f"{platform}_error", None)
    setattr(creator, f"{platform}_ok_at", now)
    setattr(creator, f"{platform}_warn", "; ".join(warn) or None)
