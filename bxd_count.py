#!/usr/bin/env python3
"""Разовый подсчёт просмотров по хэштегу #BXDBATTLE (TikTok + Instagram Reels)
через готовые скрейперы Apify (бесплатный план: $5 в месяц).

Запуск (токен Apify берётся из переменной APIFY_TOKEN или из --token):
    export APIFY_TOKEN=apify_api_xxx
    python3 bxd_count.py --start 2026-10-01 --end 2026-10-28 --only 3     # тест на 3 блогерах
    python3 bxd_count.py --start 2026-10-01 --end 2026-10-28              # все блогеры

Вход:  bloggers.csv (колонки: name,tiktok,instagram)
Выход: result.xlsx (листы: Рейтинг, Ролики, Проблемы)
Прогресс хранится в cache.json: уже собранные блогеры/платформы не запрашиваются
повторно (и не тратят кредиты); неудачные запросы повторяются при следующем запуске.
Удалите cache.json, чтобы собрать всё заново.
"""
import argparse
import csv
import getpass
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

CACHE = "cache.json"
IG_ACTOR = "apify/instagram-reel-scraper"
TT_ACTOR = "clockworks/tiktok-profile-scraper"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--bloggers", default="bloggers.csv")
    p.add_argument("--start", required=True, help="YYYY-MM-DD, включительно")
    p.add_argument("--end", required=True, help="YYYY-MM-DD, включительно")
    p.add_argument("--tz", default="Asia/Almaty")
    p.add_argument("--hashtag", default="BXDBATTLE")
    p.add_argument("--out", default="result.xlsx")
    p.add_argument("--json", default="results.json", help="файл с данными для сайта")
    p.add_argument("--max-videos", type=int, default=30,
                   help="сколько последних роликов брать у каждого аккаунта (влияет на расход кредитов)")
    p.add_argument("--batch", type=int, default=20, help="сколько аккаунтов за один запуск скрейпера")
    p.add_argument("--only", type=int, default=0, help="обработать только первых N блогеров (для теста)")
    p.add_argument("--token", default=None, help="токен Apify (или переменная APIFY_TOKEN)")
    p.add_argument("--memory", default="", help="файл, где запоминаются найденные ролики (например docs/videos_memory.json): "
                   "ролики, выпавшие из свежей части ленты, не теряются и не перечитываются")
    p.add_argument("--skip-pinned", action="store_true",
                   help="Instagram: не читать закреплённые ролики (они занимают слоты лимита); "
                        "закреплённый ролик с хэштегом при этом не будет засчитан")
    p.add_argument("--skip-ig", action="store_true")
    p.add_argument("--skip-tt", action="store_true")
    return p.parse_args()


# ---------------- утилиты ----------------
def clean_username(v):
    v = (v or "").strip()
    if not v:
        return ""
    v = v.rstrip("/").split("/")[-1].split("?")[0]
    return v.lstrip("@")


def has_tag(text, hashtag):
    """Хэштег целиком: #bxdbattle подходит, #bxdbattle2 и #bxdbattles — нет."""
    pattern = rf"#{re.escape(hashtag.lstrip('#'))}(?!\w)"
    return re.search(pattern, text or "", flags=re.IGNORECASE) is not None


def parse_dt(v, tz):
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


def to_int(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def get_client(token):
    from apify_client import ApifyClient
    return ApifyClient(token)


def _field(obj, *names):
    """Читает поле и из словаря (старый apify-client), и из объекта (новый, pydantic)."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def run_actor(client, actor, run_input):
    """Запускает скрейпер, ждёт завершения. Возвращает (items, cost_usd)."""
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


# ---------------- разбор ответов скрейперов ----------------
def parse_tiktok(item, tz):
    owner = ((item.get("authorMeta") or {}).get("name") or "").lower()
    published = parse_dt(item.get("createTimeISO") or item.get("createTime"), tz)
    tags = [(h.get("name") if isinstance(h, dict) else str(h)) for h in (item.get("hashtags") or [])]
    return {
        "owner": owner,
        "published": published,
        "text": item.get("text") or "",
        "tags": [t.lower() for t in tags if t],
        "rec": {
            "platform": "TikTok",
            "id": str(item.get("id")),
            "url": item.get("webVideoUrl"),
            "views": to_int(item.get("playCount")),
        },
    }


def parse_instagram(item, tz):
    owner = (item.get("ownerUsername") or (item.get("owner") or {}).get("username") or "").lower()
    published = parse_dt(item.get("timestamp") or item.get("takenAtTimestamp"), tz)
    code = item.get("shortCode") or item.get("code")
    views = None
    for k in ("videoPlayCount", "playsCount", "videoViewCount"):
        if to_int(item.get(k)) is not None:
            views = to_int(item.get(k))
            break
    tags = [str(t).lstrip("#").lower() for t in (item.get("hashtags") or [])]
    return {
        "owner": owner,
        "published": published,
        "text": item.get("caption") or "",
        "tags": tags,
        "rec": {
            "platform": "Instagram",
            "id": str(code or item.get("id")),
            "url": item.get("url") or (f"https://www.instagram.com/reel/{code}/" if code else None),
            "views": views,
        },
    }


def build_input(platform, usernames, max_videos, skip_pinned=False):
    if platform == "tt":
        return {"profiles": usernames, "resultsPerPage": max_videos,
                "profileScrapeSections": ["videos"], "profileSorting": "latest"}
    inp = {"username": usernames, "resultsLimit": max_videos}
    if skip_pinned:
        inp["skipPinnedPosts"] = True
    return inp


# ---------------- сбор по платформе ----------------
def collect_platform(client, platform, pending, cache, a, tz, start, end, state):
    """pending: список (key, username). Результат пишется в cache[key]."""
    actor, parser = (TT_ACTOR, parse_tiktok) if platform == "tt" else (IG_ACTOR, parse_instagram)
    label = "TikTok" if platform == "tt" else "Instagram"

    for i in range(0, len(pending), a.batch):
        chunk = pending[i:i + a.batch]
        users = [u for _, u in chunk]
        print(f"  {label}: запуск скрейпера для {len(users)} аккаунтов…")
        try:
            items, cost = run_actor(client, actor, build_input(platform, users, a.max_videos, a.skip_pinned))
        except Exception as e:  # noqa: BLE001
            print(f"  {label}: ошибка запуска — {str(e)[:200]}")
            for key, _ in chunk:
                cache[key][f"{platform}_err"] = f"{label}: {e}"
            save_cache(cache)
            continue
        if cost is not None:
            state["cost"] += cost
        print(f"  {label}: получено элементов: {len(items)}; "
              f"расход ≈ ${cost if cost is not None else '?'}; всего ≈ ${state['cost']:.3f}")

        by_owner, error_items = {}, []
        for it in items:
            if it.get("error") or it.get("errorDescription"):
                error_items.append(str(it.get("error") or it.get("errorDescription")))
                continue
            p = parser(it, tz)
            if p["owner"]:
                by_owner.setdefault(p["owner"], []).append(p)

        for key, user in chunk:
            parsed = by_owner.get(user.lower(), [])
            rec = cache[key]
            rec[f"{platform}_warn"] = []
            if not parsed:
                err = next((e for e in error_items if user.lower() in e.lower()), None)
                if err:
                    rec[f"{platform}_err"] = f"{label}: {err[:200]}"
                    continue
                # скрейпер ничего не вернул: пустой/закрытый/несуществующий аккаунт
                rec[platform] = []
                rec[f"{platform}_err"] = None
                rec[f"{platform}_done"] = True
                rec[f"{platform}_warn"].append(f"{label}: роликов не получено (аккаунт пуст, закрыт или не найден) — проверьте")
                continue
            videos = []
            for p in parsed:
                if p["published"] is None or not (start <= p["published"] <= end):
                    continue
                if not (has_tag(p["text"], a.hashtag) or a.hashtag.lower() in p["tags"]):
                    continue
                r = dict(p["rec"])
                r["published"] = p["published"].isoformat()
                videos.append(r)
            dates = [p["published"] for p in parsed if p["published"]]
            if len(parsed) >= a.max_videos and dates and min(dates) >= start:
                rec[f"{platform}_warn"].append(
                    f"{label}: достигнут лимит {a.max_videos} роликов, возможно есть ещё (увеличьте --max-videos)")
            # ролики, найденные раньше, но лежащие глубже прочитанной части ленты, остаются как были;
            # если ролик моложе самого старого прочитанного, но не вернулся, его удалили или сняли хэштег
            prev = rec.get(f"{platform}_prev") or []
            if prev:
                seen = {v["id"] for v in videos}
                oldest = min(dates).isoformat() if dates and len(parsed) >= a.max_videos else None
                for v in prev:
                    if v["id"] in seen:
                        continue
                    pub = datetime.fromisoformat(v["published"])
                    if oldest and v["published"] < oldest and start <= pub <= end:
                        videos.append(v)
            rec[platform] = videos
            rec[f"{platform}_err"] = None
            rec[f"{platform}_done"] = True
        save_cache(cache)


def save_cache(cache):
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


# ---------------- main ----------------
def main():
    a = parse_args()
    tz = ZoneInfo(a.tz)
    start = datetime.fromisoformat(a.start).replace(tzinfo=tz)
    end = (datetime.fromisoformat(a.end) + timedelta(days=1) - timedelta(seconds=1)).replace(tzinfo=tz)

    token = a.token or os.environ.get("APIFY_TOKEN") or getpass.getpass("Токен Apify (ввод скрыт): ")
    client = get_client(token.strip())

    with open(a.bloggers, encoding="utf-8-sig", newline="") as f:
        bloggers = list(csv.DictReader(f))
    if a.only:
        bloggers = bloggers[:a.only]

    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)

    memory = {}
    if a.memory and os.path.exists(a.memory) and os.path.getsize(a.memory) > 0:
        with open(a.memory, encoding="utf-8") as f:
            memory = json.load(f)

    pending_tt, pending_ig = [], []
    for b in bloggers:
        name = (b.get("name") or "").strip()
        tt, ig = clean_username(b.get("tiktok")), clean_username(b.get("instagram"))
        key = f"{name}|{tt}|{ig}"
        rec = cache.setdefault(key, {})
        for k, v in (("tt", []), ("ig", []), ("tt_err", None), ("ig_err", None),
                     ("tt_warn", []), ("ig_warn", [])):
            rec.setdefault(k, v)
        rec["tt_prev"] = memory.get(key, {}).get("tt", [])
        rec["ig_prev"] = memory.get(key, {}).get("ig", [])
        rec.setdefault("tt_done", not tt)
        rec.setdefault("ig_done", not ig)
        if tt and not rec["tt_done"] and not a.skip_tt:
            pending_tt.append((key, tt))
        if ig and not rec["ig_done"] and not a.skip_ig:
            pending_ig.append((key, ig))

    print(f"Блогеров: {len(bloggers)}. К запросу: TikTok {len(pending_tt)}, Instagram {len(pending_ig)}.")
    state = {"cost": 0.0}
    try:
        if pending_tt:
            collect_platform(client, "tt", pending_tt, cache, a, tz, start, end, state)
        if pending_ig:
            collect_platform(client, "ig", pending_ig, cache, a, tz, start, end, state)
    except KeyboardInterrupt:
        print("\nОстановлено вручную. Собранное сохранено в cache.json; строю отчёт по тому, что есть.")
    save_cache(cache)
    if a.memory:
        # по каждой платформе пишем новые данные только если запрос удался; иначе остаётся прежняя память
        mem = {k: dict(v) for k, v in memory.items()}
        for k, r in cache.items():
            for pl in ("tt", "ig"):
                if r.get(f"{pl}_done") and r.get(f"{pl}_err") is None:
                    mem.setdefault(k, {})[pl] = r.get(pl, [])
        os.makedirs(os.path.dirname(a.memory) or ".", exist_ok=True)
        with open(a.memory, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False)
    print(f"Расход Apify за этот запуск ≈ ${state['cost']:.3f}")

    # ---- сбор результата ----
    rows, videos_rows, problems = [], [], []
    for b in bloggers:
        name = (b.get("name") or "").strip()
        tt, ig = clean_username(b.get("tiktok")), clean_username(b.get("instagram"))
        rec = cache.get(f"{name}|{tt}|{ig}")
        if not rec:
            continue
        tt_ok = bool(tt) and rec["tt_done"]
        ig_ok = bool(ig) and rec["ig_done"]
        tt_views = sum(v["views"] or 0 for v in rec["tt"]) if tt_ok else None
        ig_views = sum(v["views"] or 0 for v in rec["ig"]) if ig_ok else None
        got = [x for x in (tt_views, ig_views) if x is not None]
        total = sum(got) if got else None
        missing = sum(1 for v in rec["tt"] + rec["ig"] if v["views"] is None)
        flags = []
        if tt and not rec["tt_done"]:
            flags.append("TikTok не получен" if rec["tt_err"] else "TikTok не запрашивался")
        if ig and not rec["ig_done"]:
            flags.append("Instagram не получен" if rec["ig_err"] else "Instagram не запрашивался")
        if missing:
            flags.append(f"у {missing} роликов нет просмотров")
        warns = rec["tt_warn"] + rec["ig_warn"]
        if warns:
            flags.append("проверить")
        rows.append({
            "name": name, "tt": tt, "ig": ig,
            "tt_views": tt_views, "ig_views": ig_views, "total": total,
            "tt_n": len(rec["tt"]) if tt_ok else None,
            "ig_n": len(rec["ig"]) if ig_ok else None,
            "status": "; ".join(flags) if flags else "OK",
        })
        for v in rec["tt"] + rec["ig"]:
            videos_rows.append([name, v["platform"], v["url"], v["published"][:19], v["views"]])
        for e in [rec["tt_err"], rec["ig_err"]] + warns:
            if e:
                problems.append([name, e])

    rows.sort(key=lambda r: (r["total"] is not None, r["total"] or 0), reverse=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Рейтинг"
    ws.append([f"Период: {a.start} — {a.end} ({a.tz}); хэштег #{a.hashtag}; "
               f"сформировано {datetime.now(tz):%Y-%m-%d %H:%M}"])
    ws.append([])
    ws.append(["Место", "Блогер", "TikTok", "Instagram", "Просмотры TikTok",
               "Просмотры Instagram", "Всего просмотров", "Роликов TikTok",
               "Роликов Instagram", "Статус данных"])
    for c in ws[3]:
        c.font = Font(bold=True)
    for n, r in enumerate(rows, 1):
        ws.append([n, r["name"], r["tt"], r["ig"], r["tt_views"], r["ig_views"],
                   r["total"], r["tt_n"], r["ig_n"], r["status"]])
    ws.freeze_panes = "A4"

    ws2 = wb.create_sheet("Ролики")
    ws2.append(["Блогер", "Платформа", "Ссылка", "Дата публикации", "Просмотры"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    for r in videos_rows:
        ws2.append(r)

    ws3 = wb.create_sheet("Проблемы")
    ws3.append(["Блогер", "Проблема"])
    for c in ws3[1]:
        c.font = Font(bold=True)
    for r in problems:
        ws3.append(r)

    for sheet in (ws, ws2, ws3):
        for col in range(1, sheet.max_column + 1):
            width = max((len(str(c.value)) for c in sheet[get_column_letter(col)][2:]
                         if c.value is not None), default=10)
            sheet.column_dimensions[get_column_letter(col)].width = min(max(width + 2, 12), 60)

    wb.save(a.out)

    # ---- данные для сайта ----
    # Если платформа в этот раз не получена, берём прошлые значения из results.json,
    # чтобы на сайте не пропадали просмотры из-за временного сбоя.
    prev = {}
    if os.path.exists(a.json):
        try:
            with open(a.json, encoding="utf-8") as f:
                prev = {r["name"]: r for r in json.load(f).get("rows", [])}
        except (ValueError, KeyError, OSError):
            prev = {}
    site_rows = []
    for r in rows:
        old = prev.get(r["name"], {})
        tt_v, ig_v, tt_n, ig_n, status = r["tt_views"], r["ig_views"], r["tt_n"], r["ig_n"], r["status"]
        carried = False
        if r["tt"] and tt_v is None and old.get("tt_views") is not None:
            tt_v, tt_n, carried = old["tt_views"], old.get("tt_videos"), True
        if r["ig"] and ig_v is None and old.get("ig_views") is not None:
            ig_v, ig_n, carried = old["ig_views"], old.get("ig_videos"), True
        got = [x for x in (tt_v, ig_v) if x is not None]
        if carried:
            status = "часть данных от прошлого обновления; " + status
        site_rows.append({
            "name": r["name"], "tiktok": r["tt"] or None, "instagram": r["ig"] or None,
            "tt_views": tt_v, "ig_views": ig_v, "total": sum(got) if got else None,
            "tt_videos": tt_n, "ig_videos": ig_n, "complete": status == "OK", "status": status,
        })
    site_rows.sort(key=lambda r: (r["total"] is not None, r["total"] or 0), reverse=True)
    for n, r in enumerate(site_rows, 1):
        r["rank"] = n
        p = prev.get(r["name"], {})
        r["rank_change"] = (p["rank"] - n) if p.get("rank") else None      # плюс = поднялся
        r["delta"] = (r["total"] - p["total"]) if (r["total"] is not None and p.get("total") is not None) else None
    site = {
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "hashtag": a.hashtag,
        "period": {"start": a.start, "end": a.end, "tz": a.tz},
        "totals": {
            "views": sum(r["total"] or 0 for r in site_rows),
            "bloggers": len(site_rows),
            "videos": len(videos_rows),
        },
        "rows": site_rows,
    }
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(site, f, ensure_ascii=False, indent=1)
    print(f"\nГотово: {a.out}. Блогеров: {len(rows)}, роликов: {len(videos_rows)}, проблем: {len(problems)}")


if __name__ == "__main__":
    main()