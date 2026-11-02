import os
import sys
from pathlib import Path

os.environ["BXD_NO_AUTOSTART"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TT = [
    {"id": "t1", "authorMeta": {"name": "alice_tt"}, "createTimeISO": "2026-10-05T10:00:00.000Z",
     "text": "go #BXDBATTLE", "playCount": 1000, "webVideoUrl": "https://tt/t1"},
    {"id": "t2", "authorMeta": {"name": "alice_tt"}, "createTimeISO": "2026-10-06T10:00:00.000Z",
     "text": "wrong tag #bxdbattle2", "playCount": 9999, "webVideoUrl": "https://tt/t2"},
    {"id": "t3", "authorMeta": {"name": "alice_tt"}, "createTimeISO": "2026-09-20T10:00:00.000Z",
     "text": "before period #BXDBATTLE", "playCount": 5000, "webVideoUrl": "https://tt/t3"},
    {"id": "t4", "authorMeta": {"name": "alice_tt"}, "createTimeISO": "2026-10-28T20:00:00.000Z",
     "text": "after period (Almaty) #BXDBATTLE", "playCount": 777, "webVideoUrl": "https://tt/t4"},
    {"id": "t5", "authorMeta": {"name": "bob_tt"}, "createTimeISO": "2026-10-10T10:00:00.000Z",
     "text": "#bxdbattle", "playCount": 300, "webVideoUrl": "https://tt/t5"},
]
IG = [
    {"shortCode": "i1", "ownerUsername": "Alice_IG", "timestamp": "2026-10-07T10:00:00.000Z",
     "caption": "#BXDBATTLE", "videoPlayCount": 500, "url": "https://ig/i1"},
    {"shortCode": "i2", "ownerUsername": "alice_ig", "timestamp": "2026-10-08T10:00:00.000Z",
     "caption": "#BXDBATTLE", "videoViewCount": 50},
]


class FakeClient:
    """Подставной Apify: отдаёт заранее заданные данные, можно «сломать» по флагу."""
    tt, ig, fail = TT, IG, False

    def actor(self, name):
        outer = self

        class A:
            def call(self, run_input):
                if FakeClient.fail:
                    raise RuntimeError("quota exceeded")
                return {"status": "SUCCEEDED", "defaultDatasetId": name, "usageTotalUsd": 0.05}
        return A()

    def dataset(self, ds):
        class D:
            def iterate_items(self):
                return iter(FakeClient.tt if "tiktok" in ds else FakeClient.ig)
        return D()


@pytest.fixture()
def client(tmp_path):
    FakeClient.tt, FakeClient.ig, FakeClient.fail = list(TT), list(IG), False
    csv = tmp_path / "bloggers.csv"
    csv.write_text("name,tiktok,instagram\nAlice,@alice_tt,@alice_ig\nBob,https://www.tiktok.com/@bob_tt,\n"
                   "Carol,,@carol_ig\n", encoding="utf-8")
    s = Settings(database_url=f"sqlite:///{tmp_path/'t.db'}", admin_token="adm", apify_token="tok",
                 creators_file=str(csv), frontend_dir=str(tmp_path / "none"), max_videos=10,
                 min_refresh_hours=6)
    with TestClient(create_app(s, client_factory=lambda t: FakeClient())) as c:
        yield c


H = {"X-Admin-Token": "adm"}


def rows(c):
    return {r["name"]: r for r in c.get("/api/ranking").json()["rows"]}


def test_before_first_refresh_no_zeros(client):
    d = client.get("/api/ranking").json()
    assert d["generated_at"] is None and len(d["rows"]) == 3
    assert all(r["total"] is None for r in d["rows"])  # нет данных, а не нули


def test_refresh_requires_token(client):
    assert client.post("/api/refresh").status_code == 401
    assert client.post("/api/refresh", headers={"X-Admin-Token": "bad"}).status_code == 401


def test_refresh_disabled_without_admin_token(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path/'t.db'}", creators_file=str(tmp_path / "x.csv"),
                 frontend_dir=str(tmp_path))
    with TestClient(create_app(s)) as c:
        assert c.post("/api/refresh", headers=H).status_code == 503


def test_ranking_rules(client):
    assert client.post("/api/refresh", headers=H).status_code == 202
    r = rows(client)
    # TikTok Alice: только t1 (t2 — чужой хэштег, t3/t4 — вне периода по Алматы)
    assert r["Alice"]["tt_views"] == 1000
    assert r["Alice"]["ig_views"] == 550 and r["Alice"]["total"] == 1550
    assert r["Alice"]["tt_videos"] == 1 and r["Alice"]["ig_videos"] == 2
    assert r["Bob"]["total"] == 300 and r["Bob"]["ig_views"] is None
    # Carol: Instagram вернул пусто -> не ноль «ок», а пометка «проверить»
    assert r["Carol"]["total"] == 0 and not r["Carol"]["complete"]
    d = client.get("/api/ranking").json()
    assert [x["name"] for x in d["rows"]][:2] == ["Alice", "Bob"]
    assert d["totals"]["views"] == 1850 and d["generated_at"] is not None


def test_cooldown_and_force(client):
    assert client.post("/api/refresh", headers=H).status_code == 202
    assert client.post("/api/refresh", headers=H).status_code == 429  # защита от лишних трат
    assert client.post("/api/refresh?force=true", headers=H).status_code == 202


def test_failure_keeps_previous_data(client):
    client.post("/api/refresh", headers=H)
    FakeClient.fail = True
    client.post("/api/refresh?force=true", headers=H)
    a = rows(client)["Alice"]
    assert a["total"] == 1550          # прошлые значения не пропали
    assert not a["complete"] and "прошлые данные" in a["status"]
    assert client.get("/api/status").json()["last_run"]["status"] == "failed"


def test_deleted_video_disappears_and_views_update(client):
    client.post("/api/refresh", headers=H)
    FakeClient.tt = [dict(TT[0], playCount=2500)]          # у Alice остался один ролик, просмотры выросли
    FakeClient.ig = [IG[0]]                                # i2 пропал
    client.post("/api/refresh?force=true", headers=H)
    a = rows(client)["Alice"]
    assert a["tt_views"] == 2500 and a["ig_views"] == 500 and a["ig_videos"] == 1


def test_creator_videos_endpoint(client):
    client.post("/api/refresh", headers=H)
    cid = rows(client)["Alice"]["id"]
    v = client.get(f"/api/creators/{cid}/videos").json()["videos"]
    assert [x["views"] for x in v] == [1000, 500, 50]
    assert client.get("/api/creators/9999/videos").status_code == 404


def test_missing_views_not_zero(client):
    FakeClient.ig = [{"shortCode": "n1", "ownerUsername": "alice_ig", "timestamp": "2026-10-07T10:00:00Z",
                      "caption": "#BXDBATTLE"}]
    client.post("/api/refresh", headers=H)
    a = rows(client)["Alice"]
    assert "нет просмотров" in a["status"] and not a["complete"]


def test_movement_and_delta(client):
    client.post("/api/refresh", headers=H)
    first = rows(client)
    assert first["Alice"]["rank_change"] is None and first["Alice"]["delta"] is None  # сравнивать пока не с чем
    # Боб набирает больше Алисы
    FakeClient.tt = [dict(TT[0], playCount=1100), dict(TT[4], playCount=5000)]
    client.post("/api/refresh?force=true", headers=H)
    second = rows(client)
    assert second["Bob"]["rank"] == 1 and second["Bob"]["rank_change"] == 1 and second["Bob"]["delta"] == 4700
    assert second["Alice"]["rank"] == 2 and second["Alice"]["rank_change"] == -1 and second["Alice"]["delta"] == 100
