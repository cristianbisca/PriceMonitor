"""E2E test for the link-candidate review API (list / approve / dismiss).

Drives the real FastAPI app against a temp DB. Users are registered through the
public endpoint; products, prices and candidates are seeded via the ORM.
No network access is required.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

_tmpdir = tempfile.mkdtemp(prefix="pm_e2e_")
_dbfile = os.path.join(_tmpdir, "pm_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_dbfile}"
os.environ.pop("RESTORE_LATEST_BACKUP", None)
os.environ.pop("BACKUP_SCHEDULE", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json  # noqa: E402
from collections import Counter  # noqa: E402

import database as dbmod  # import database first so it reads DATABASE_URL

db = dbmod.SessionLocal
from fastapi.testclient import TestClient  # noqa: E402
import api  # noqa: E402
from auth import generate_token  # noqa: E402
from models import LinkCandidate, PriceEntry, Product  # noqa: E402

app = api.app


def _utcnow():
    return datetime.now(timezone.utc)


def register(client, username):
    r = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["token"]


def find_user(user_id):
    from models import User
    s = db()
    try:
        return s.query(User).filter(User.id == user_id).first()
    finally:
        s.close()


def main():
    with TestClient(app) as client:
        tok_alice = register(client, "alice")
        h_alice = {"X-PM-Token": tok_alice}

        # create a product through the real endpoint (initial check fails offline, but
        # the product is still created with a known URL)
        r = client.post(
            "/api/products",
            headers=h_alice,
            json={"name": "Test Product", "url": "https://shop.example/product"},
        )
        assert r.status_code == 201, (r.status_code, r.text)
        pid = r.json()["id"]

        # seed price entries + candidates directly
        now = _utcnow()
        s = db()
        try:
            s.add(PriceEntry(product_id=pid, price=100.0, checked_at=now, check_cycle=now, source="shop.example"))
            s.add(PriceEntry(product_id=pid, price=110.0, checked_at=now, check_cycle=now, source="other.example"))
            c1 = LinkCandidate(product_id=pid, url="https://store-a.example/p", price=90.0, match_method="code", status="pending", found_at=now)
            c2 = LinkCandidate(product_id=pid, url="https://store-b.example/p/", price=95.0, match_method="model", status="pending", found_at=now)
            c3 = LinkCandidate(product_id=pid, url="https://store-c.example/p", price=120.0, match_method="name", status="pending", found_at=now)
            c4 = LinkCandidate(product_id=pid, url="https://store-d.example/p", price=80.0, match_method="code", status="dismissed", found_at=now, decided_at=now)
            c5 = LinkCandidate(product_id=pid, url="https://store-b.example/p", price=94.0, match_method="name", status="pending", found_at=now)  # same base URL as c2
            s.add_all([c1, c2, c3, c4, c5])
            s.commit()
            cid_a, cid_b, cid_5 = c1.id, c2.id, c5.id
        finally:
            s.close()

        # unauthorized listing
        r = client.get(f"/api/products/{pid}/candidates")
        assert r.status_code == 401, (r.status_code, r.text)

        # list: only cheaper pending candidates, sorted by reliability (code first)
        r = client.get(f"/api/products/{pid}/candidates", headers=h_alice)
        assert r.status_code == 200, (r.status_code, r.text)
        items = r.json()
        urls = [it["url"] for it in items]
        assert urls == ["https://store-a.example/p", "https://store-b.example/p/", "https://store-b.example/p"], urls
        assert [it["match_method"] for it in items] == ["code", "model", "name"]
        assert items[0]["savings"] == 10.0, items[0]   # 100 - 90
        assert items[1]["savings"] == 5.0, items[1]    # 100 - 95
        assert items[2]["savings"] == 6.0, items[2]    # 100 - 94

        # product listing carries candidate_count (3 pending + cheaper, capped at 3)
        r = client.get("/api/products", headers=h_alice)
        prods = {p["id"]: p for p in r.json()}
        assert prods[pid]["candidate_count"] == 3, prods[pid]

        # single product endpoint carries it too
        r = client.get(f"/api/products/{pid}", headers=h_alice)
        assert r.json()["candidate_count"] == 3, r.json()

        # ── approve c2 (model match, store-b with trailing slash) ──
        r = client.post(f"/api/products/{pid}/candidates/{cid_b}/approve", headers=h_alice)
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["success"] is True
        assert "https://store-b.example/p/" in body["alternative_urls"], body
        assert body["candidate"]["status"] == "approved"
        assert body["candidate"]["decided_at"] is not None

        # approved candidate no longer listed; c1 and c5 still show
        r = client.get(f"/api/products/{pid}/candidates", headers=h_alice)
        assert [it["url"] for it in r.json()] == [
            "https://store-a.example/p", "https://store-b.example/p"
        ]
        r = client.post(f"/api/products/{pid}/candidates/{cid_b}/approve", headers=h_alice)
        assert r.status_code == 400, (r.status_code, r.text)

        # ── approve c1 (code match, store-a) ──
        r = client.post(f"/api/products/{pid}/candidates/{cid_a}/approve", headers=h_alice)
        assert r.status_code == 200, r.text
        urls = r.json()["alternative_urls"]
        assert urls == ["https://store-b.example/p/", "https://store-a.example/p"], urls
        assert "https://shop.example/product" not in urls

        # ── approve c5 (same base URL as the already-attached store-b link) -> deduped ──
        r = client.post(f"/api/products/{pid}/candidates/{cid_5}/approve", headers=h_alice)
        assert r.status_code == 200, r.text
        urls = r.json()["alternative_urls"]
        assert len(urls) == 2, urls                      # no duplicate added
        assert Counter(u.rstrip("/") for u in urls) == {"https://store-b.example/p": 1, "https://store-a.example/p": 1}, urls

        # ── dismiss c3 (name match, not cheaper - still dismissible) ──
        s = db()
        try:
            c3row = s.query(LinkCandidate).filter(LinkCandidate.url == "https://store-c.example/p").first()
            cid_c = c3row.id
        finally:
            s.close()
        r = client.post(f"/api/products/{pid}/candidates/{cid_c}/dismiss", headers=h_alice)
        assert r.status_code == 200, r.text
        assert r.json()["candidate"]["status"] == "dismissed"

        # approving a dismissed candidate -> 400
        r = client.post(f"/api/products/{pid}/candidates/{cid_c}/approve", headers=h_alice)
        assert r.status_code == 400, (r.status_code, r.text)

        # pending list is now empty (c1 approved, c2 approved, c3 dismissed)
        r = client.get(f"/api/products/{pid}/candidates", headers=h_alice)
        assert r.json() == [], r.json()
        r = client.get("/api/products", headers=h_alice)
        prods = {p["id"]: p for p in r.json()}
        assert prods[pid]["candidate_count"] == 0, prods[pid]

        # ── per-user isolation ──
        tok_bob = register(client, "bob")
        r = client.get(f"/api/products/{pid}/candidates", headers={"X-PM-Token": tok_bob})
        assert r.status_code == 404, (r.status_code, r.text)
        r = client.post(f"/api/products/{pid}/candidates/{cid_a}/approve", headers={"X-PM-Token": tok_bob})
        assert r.status_code == 404, (r.status_code, r.text)

    print("ALL E2E CANDIDATE TESTS PASSED")


if __name__ == "__main__":
    main()
