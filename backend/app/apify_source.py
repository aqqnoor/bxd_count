"""Получение данных через скрейперы Apify и разбор их ответов.

Логика та же, что в рабочем скрипте bxd_count.py (проверено на реальных данных).
"""
import re
from datetime import datetime, timezone
from typing import Optional

IG_ACTOR = "apify/instagram-reel-scraper"
TT_ACTOR = "clockworks/tiktok-profile-scraper"


def has_tag(text: Optional[str], hashtag: str) -> bool:
    """Хэштег целиком: #bxdbattle подходит, #bxdbattle2 и #bxdbattles — нет."""
    pattern = rf"#{re.escape(hashtag.lstrip('#'))}(?!\w)"
    return re.search(pattern, text or "", flags=re.IGNORECASE) is not None


def parse_dt(v, tz) -> Optional[datetime]:
    """ISO-строка или unix-время (сек/мс) -> aware datetime в часовом поясе кампании."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            ts = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(tz)
    except (ValueError, OSError, OverflowError):
        return None


def to_int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def get_client(token: str):
    from apify_client import ApifyClient
    return ApifyClient(token)


def _field(obj, *names):
    """Читает поле и из словаря (старый apify-client), и из объекта (новый)."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def run_actor(client, actor: str, run_input: dict):
    """Запускает скрейпер, ждёт окончания. Возвращает (items, cost_usd)."""
    run = client.actor(actor).call(run_input=run_input)
    if not run:
        raise RuntimeError("Apify не вернул информацию о запуске")
    status = _field(run, "status")
    status = str(getattr(status, "value", status))
    if "SUCCEEDED" not in status:
        raise RuntimeError(f"запуск завершился со статусом {status}")
    dataset_id = _field(run, "defaultDatasetId", "default_dataset_id")
    items = list(client.dataset(dataset_id).iterate_items())
    cost = _field(run, "usageTotalUsd", "usage_total_usd")
    return items, (float(cost) if cost is not None else None)


def parse_tiktok(item: dict, tz) -> dict:
    owner = ((item.get("authorMeta") or {}).get("name") or "").lower()
    tags = [(h.get("name") if isinstance(h, dict) else str(h)) for h in (item.get("hashtags") or [])]
    return {
        "owner": owner,
        "published": parse_dt(item.get("createTimeISO") or item.get("createTime"), tz),
        "text": item.get("text") or "",
        "tags": [t.lower() for t in tags if t],
        "id": str(item.get("id")),
        "url": item.get("webVideoUrl"),
        "views": to_int(item.get("playCount")),
    }


def parse_instagram(item: dict, tz) -> dict:
    owner = (item.get("ownerUsername") or (item.get("owner") or {}).get("username") or "").lower()
    code = item.get("shortCode") or item.get("code")
    views = None
    for k in ("videoPlayCount", "playsCount", "videoViewCount"):
        if to_int(item.get(k)) is not None:
            views = to_int(item.get(k))
            break
    return {
        "owner": owner,
        "published": parse_dt(item.get("timestamp") or item.get("takenAtTimestamp"), tz),
        "text": item.get("caption") or "",
        "tags": [str(t).lstrip("#").lower() for t in (item.get("hashtags") or [])],
        "id": str(code or item.get("id")),
        "url": item.get("url") or (f"https://www.instagram.com/reel/{code}/" if code else None),
        "views": views,
    }


def build_input(platform: str, usernames: list, max_videos: int) -> dict:
    if platform == "tt":
        return {"profiles": usernames, "resultsPerPage": max_videos,
                "profileScrapeSections": ["videos"], "profileSorting": "latest"}
    return {"username": usernames, "resultsLimit": max_videos}


ACTORS = {"tt": (TT_ACTOR, parse_tiktok, "TikTok"), "ig": (IG_ACTOR, parse_instagram, "Instagram")}
