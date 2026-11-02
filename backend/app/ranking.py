"""Расчёт рейтинга по данным из БД."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select

from .collector import running_run
from .config import Settings
from .db import Creator, CreatorTotal, Run, Video


def to_local_iso(dt: datetime, tz: ZoneInfo) -> str:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def last_good_run(session):
    return session.scalar(select(Run).where(Run.status.in_(("ok", "partial")),
                                            Run.finished_at.is_not(None)).order_by(Run.id.desc()))


def build_ranking(session, settings: Settings) -> dict:
    tz = ZoneInfo(settings.tz)

    agg = {}
    q = (select(Video.creator_id, Video.platform, func.sum(Video.views), func.count(Video.id),
                func.sum(case((Video.views.is_(None), 1), else_=0)))
         .where(Video.active.is_(True)).group_by(Video.creator_id, Video.platform))
    for cid, platform, views, n, missing in session.execute(q):
        agg[(cid, platform)] = (int(views) if views is not None else 0, int(n), int(missing or 0))

    rows, videos_total = [], 0
    for c in session.scalars(select(Creator).where(Creator.active.is_(True))):
        sides = {}
        flags = []
        for platform, label, account, ok_at, err, warn in (
                ("tt", "TikTok", c.tiktok, c.tt_ok_at, c.tt_error, c.tt_warn),
                ("ig", "Instagram", c.instagram, c.ig_ok_at, c.ig_error, c.ig_warn)):
            if not account:
                sides[platform] = (None, None)
                continue
            if ok_at is None:
                sides[platform] = (None, None)
                flags.append(f"{label} не получен" if err else f"{label} ещё не запрашивался")
                continue
            views, n, missing = agg.get((c.id, platform), (0, 0, 0))
            sides[platform] = (views, n)
            videos_total += n
            if err:
                flags.append(f"{label}: обновление не удалось, показаны прошлые данные")
            if missing:
                flags.append(f"у {missing} роликов нет просмотров")
            if warn:
                flags.append("проверить: " + warn)
        got = [v for v, _ in sides.values() if v is not None]
        status = "; ".join(flags) if flags else "OK"
        rows.append({
            "id": c.id, "name": c.name,
            "tiktok": c.tiktok or None, "instagram": c.instagram or None,
            "tt_views": sides["tt"][0], "ig_views": sides["ig"][0],
            "total": sum(got) if got else None,
            "tt_videos": sides["tt"][1], "ig_videos": sides["ig"][1],
            "complete": status == "OK", "status": status,
        })

    rows.sort(key=lambda r: (r["total"] is not None, r["total"] or 0), reverse=True)
    for n, r in enumerate(rows, 1):
        r["rank"] = n

    good = last_good_run(session)
    _add_movement(session, rows, good)
    return {
        "generated_at": to_local_iso(good.finished_at, tz) if good else None,
        "running": running_run(session) is not None,
        "hashtag": settings.hashtag,
        "period": {"start": settings.start, "end": settings.end, "tz": settings.tz},
        "totals": {"views": sum(r["total"] or 0 for r in rows), "bloggers": len(rows),
                   "videos": videos_total},
        "rows": rows,
    }


def _add_movement(session, rows, good):
    """Сравнивает с предыдущим успешным обновлением: движение по рейтингу и прирост просмотров."""
    prev_map = {}
    if good:
        prev_run = session.scalar(select(Run).where(
            Run.status.in_(("ok", "partial")), Run.id < good.id).order_by(Run.id.desc()))
        if prev_run:
            prev_map = {t.creator_id: t for t in session.scalars(
                select(CreatorTotal).where(CreatorTotal.run_id == prev_run.id))}
    for r in rows:
        p = prev_map.get(r["id"])
        r["prev_rank"] = p.rank if p else None
        r["rank_change"] = (p.rank - r["rank"]) if p else None   # плюс = поднялся
        r["delta"] = (r["total"] - p.total) if (p and p.total is not None and r["total"] is not None) else None


def save_totals(session_factory, settings: Settings, run_id: int):
    """Запоминает итоги блогеров после успешного обновления."""
    with session_factory() as s:
        run = s.get(Run, run_id)
        if run is None or run.status not in ("ok", "partial"):
            return
        for r in build_ranking(s, settings)["rows"]:
            s.add(CreatorTotal(run_id=run_id, creator_id=r["id"], total=r["total"], rank=r["rank"]))
        s.commit()


def creator_videos(session, creator_id: int, tz: ZoneInfo):
    c = session.get(Creator, creator_id)
    if c is None or not c.active:
        return None
    vids = session.scalars(select(Video).where(Video.creator_id == creator_id, Video.active.is_(True))
                           .order_by(Video.views.is_(None), Video.views.desc(), Video.published_at.desc()))
    return [{"platform": "TikTok" if v.platform == "tt" else "Instagram", "url": v.url,
             "published_at": to_local_iso(v.published_at, tz), "views": v.views} for v in vids]
