"""Unit test for alternate_links._save_candidates: upsert, cap, dedupe, cheaper-only."""
import os
import sys
import tempfile
from datetime import datetime, timezone

_tmpdir = tempfile.mkdtemp(prefix="pm_save_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir.replace(os.sep, '/')}pm_save.db"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as dbmod  # noqa: E402 (imported for side-effect-free consistency only)
from models import Base, LinkCandidate, Product, User  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

engine = create_engine(os.environ["DATABASE_URL"])
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

from alternate_links import _save_candidates, _max_links  # noqa: E402

MAX = _max_links()


def now_utc():
    return datetime.now(timezone.utc)


def seed_product(s, name):
    url = f"https://main.example/{name}"
    p = s.query(Product).filter(Product.user_id == 1, Product.url == url).first()
    if p is None:
        p = Product(user_id=1, name=name, url=url, alternative_urls=None)
        s.add(p)
        s.commit()
    return s.get(Product, p.id)


def pending_rows(s, product):
    rows = s.query(LinkCandidate).filter(
        LinkCandidate.product_id == product.id,
        LinkCandidate.status == "pending",
    ).all()
    return {r.url: (r.price, r.match_method) for r in rows}


def main():
    now = now_utc()

    # ── 1) first run: cheaper-only filter + capped by ALTERNATE_LINKS_MAX ──
    s = Session()
    p = seed_product(s, "A")
    pool = {
        "https://z.example/1": [90.0, "code"],
        "https://z.example/2": [95.0, "name"],
        "https://z.example/3": [92.0, "model"],
        "https://z.example/4": [91.0, "name"],
        "https://z.example/5": [150.0, "code"],  # not cheaper than 100 -> skipped
    }
    new_count, pending_count = _save_candidates(s, p, pool, 100.0)
    rows = pending_rows(s, p)
    # ranked: code z1, model z3, name z4 (91), name z2 (95); capped at MAX (3)
    assert new_count == MAX, new_count
    assert pending_count == MAX, pending_count
    assert set(rows) == {f"https://z.example/{i}" for i in (1, 3, 4)}, rows
    assert rows["https://z.example/1"] == (90.0, "code")
    assert rows["https://z.example/3"] == (92.0, "model")
    assert rows["https://z.example/4"] == (91.0, "name")
    assert "https://z.example/2" not in rows
    assert "https://z.example/5" not in rows
    s.close()

    # ── 2) re-run: price refresh, no duplicates, no method downgrade ──
    s = Session()
    p = seed_product(s, "A")
    pool2 = {
        "https://z.example/1": [85.0, "name"],   # price dropped; weaker method -> keep "code"
        "https://z.example/2": [95.0, "name"],   # new row (was dropped by cap last run)
        "https://z.example/3": [92.0, "model"],
    }
    new_count, pending_count = _save_candidates(s, p, pool2, 100.0)
    assert new_count == 1, new_count          # only z2 is new
    rows = pending_rows(s, p)
    assert rows["https://z.example/1"] == (85.0, "code"), rows   # price refreshed, method upgraded-only
    assert rows["https://z.example/2"] == (95.0, "name")
    assert rows["https://z.example/4"] == (91.0, "name"), rows   # old pending row persists
    total = s.query(LinkCandidate).filter(
        LinkCandidate.product_id == p.id, LinkCandidate.url == "https://z.example/1"
    ).count()
    assert total == 1, f"duplicate rows for same URL: {total}"
    s.close()

    # ── 3) dismissed candidates are never resaved or refreshed ──
    s = Session()
    p = seed_product(s, "A")
    target = s.query(LinkCandidate).filter(
        LinkCandidate.product_id == p.id, LinkCandidate.url == "https://z.example/2"
    ).first()
    target.status = "dismissed"
    target.decided_at = now
    s.commit()
    _save_candidates(s, p, {"https://z.example/2": [80.0, "code"]}, 100.0)
    row = s.get(LinkCandidate, target.id)
    assert row.status == "dismissed", row.status
    assert row.price == 95.0, row.price
    assert row.match_method == "name", row.match_method
    pending = s.query(LinkCandidate).filter(
        LinkCandidate.product_id == p.id, LinkCandidate.status == "pending"
    ).count()
    assert pending == 3, pending              # z1, z3, z4
    s.close()

    # ── 4) no current price recorded -> rank by price, verify-priced first, capped ──
    s = Session()
    p = seed_product(s, "B")
    pool4 = {
        "https://a.example/1": [None, "name"],  # ranked last (no price), dropped by cap
        "https://a.example/2": [50.0, "code"],
        "https://a.example/3": [70.0, "model"],
        "https://a.example/4": [60.0, "name"],
    }
    new_count, pending_count = _save_candidates(s, p, pool4, None)
    rows = pending_rows(s, p)
    assert set(rows) == {f"https://a.example/{i}" for i in (2, 3, 4)}, rows
    assert new_count == 3 and pending_count == 3
    s.close()

    print("ALL _save_candidates TESTS PASSED")


if __name__ == "__main__":
    main()
