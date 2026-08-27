"""Test the deferred 'new alternate links found' notification flow.

Covers: registration, merge across overnight runs, first-price-check-run-only
delivery, drop-when-user-already-acted, retry-when-send-fails, and drop-when-
no-chat-id. No network access is required (send_message is stubbed).
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

_tmpdir = tempfile.mkdtemp(prefix="pm_pln_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir.replace(os.sep, '/')}pm_pln.db"
os.environ.pop("TELEGRAM_CHAT_ID", None)  # the flush must rely on per-user settings only

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as dbmod  # noqa: E402
import telegram_notifier  # noqa: E402
import scheduler  # noqa: E402
import alternate_links as alt  # noqa: E402
from models import Base, LinkCandidate, PriceEntry, Product, User, PendingLinkNotification  # noqa: E402

SessionLocal = alt.SessionLocal
Base.metadata.create_all(dbmod.engine)

sent = []


def fake_send_message(chat_id, text, parse_mode="HTML"):
    sent.append({"chat_id": chat_id, "text": text})
    return True


telegram_notifier.send_message = fake_send_message


def now_utc():
    return datetime.now(timezone.utc)


def seed_product(name, chat_enabled=True):
    s = SessionLocal()
    try:
        u = User(username=f"user_{name}", password_hash="x",
                 telegram_chat_id="12345" if chat_enabled else None,
                 telegram_notifications_enabled=chat_enabled)
        s.add(u)
        s.commit()
        p = Product(user_id=u.id, name=name, url=f"https://mea.ro/{name.lower()}")
        s.add(p)
        s.commit()
        cycle = now_utc()
        s.add(PriceEntry(product_id=p.id, price=100.0, checked_at=cycle, check_cycle=cycle, source="mea.ro"))
        s.commit()
        return p.id
    finally:
        s.close()


def add_candidate(pid, url, price=90.0, status="pending"):
    s = SessionLocal()
    try:
        c = LinkCandidate(product_id=pid, url=url, price=price, match_method="code",
                          status=status, found_at=now_utc())
        s.add(c)
        s.commit()
        return c.id
    finally:
        s.close()


def notes_for(pid):
    s = SessionLocal()
    try:
        return s.query(PendingLinkNotification).filter(PendingLinkNotification.product_id == pid).all()
    finally:
        s.close()


def main():
    # ── 1) registration + merge across two overnight runs -> one row ──
    pid_a = seed_product("A")
    c1 = add_candidate(pid_a, "https://s1.ro/a")
    c2 = add_candidate(pid_a, "https://s2.ro/a")
    alt.register_pending_link_notification(pid_a, [c1])
    alt.register_pending_link_notification(pid_a, [c2])
    alt.register_pending_link_notification(pid_a, [c1])  # duplicate id, ignored
    time.sleep(0.01)
    alt.register_pending_link_notification(pid_a, [])  # no-op

    notes = notes_for(pid_a)
    assert len(notes) == 1, notes
    import json
    assert sorted(json.loads(notes[0].candidate_ids)) == sorted([c1, c2]), notes[0].candidate_ids
    assert notes[0].user_id, "user_id must be set"

    # ── 2) first flush delivers once, deletes the row; second flush is a no-op ──
    scheduler._flush_pending_link_notifications()
    assert len(sent) == 1, sent
    text = sent[0]["text"]
    assert sent[0]["chat_id"] == "12345"
    assert "New Links Found" in text
    assert "2 cheaper link(s)" in text
    assert "s1.ro" in text and "s2.ro" in text
    assert "90.00 RON" in text
    assert "saves" in text and "10.00" in text
    assert notes_for(pid_a) == []

    scheduler._flush_pending_link_notifications()
    assert len(sent) == 1, "second price-check run must not re-send"

    # ── 3) user already dismissed the links before the check -> dropped, not sent ──
    pid_b = seed_product("B")
    cb1 = add_candidate(pid_b, "https://s3.ro/b")
    alt.register_pending_link_notification(pid_b, [cb1])
    s = SessionLocal()
    try:
        row = s.get(LinkCandidate, cb1)
        row.status = "dismissed"
        row.decided_at = now_utc()
        s.commit()
    finally:
        s.close()
    scheduler._flush_pending_link_notifications()
    assert len(sent) == 1, "dismissed-before-check must not be sent"
    assert notes_for(pid_b) == []

    # ── 4) send failure keeps the row; next run retries and delivers ──
    pid_c = seed_product("C")
    cc1 = add_candidate(pid_c, "https://s4.ro/c")
    alt.register_pending_link_notification(pid_c, [cc1])

    def failing_send(chat_id, text, parse_mode="HTML"):
        raise RuntimeError("telegram down")

    telegram_notifier.send_message = failing_send
    scheduler._flush_pending_link_notifications()
    assert len(notes_for(pid_c)) == 1, "failed send must keep the row for the next run"

    telegram_notifier.send_message = fake_send_message
    scheduler._flush_pending_link_notifications()
    assert len(sent) == 2, "retry after failure must deliver exactly once"
    assert notes_for(pid_c) == []

    # ── 5) no chat IDs (notifications disabled) -> dropped without sending ──
    pid_d = seed_product("D", chat_enabled=False)
    cd1 = add_candidate(pid_d, "https://s5.ro/d")
    alt.register_pending_link_notification(pid_d, [cd1])
    scheduler._flush_pending_link_notifications()
    assert len(sent) == 2, "disabled user must not receive the notification"
    assert notes_for(pid_d) == []

    # ── 6) foreign-currency candidate: page price shown, savings computed on converted price ──
    pid_e = seed_product("E")
    s = SessionLocal()
    try:
        ce = LinkCandidate(product_id=pid_e, url="https://fx.example/e", price=50.0,
                           original_price=10.0, price_currency="EUR",
                           match_method="code", status="pending", found_at=now_utc())
        s.add(ce)
        s.commit()
        ce_id = ce.id
    finally:
        s.close()
    alt.register_pending_link_notification(pid_e, [ce_id])
    scheduler._flush_pending_link_notifications()
    text_e = sent[-1]["text"]
    assert "50.00 RON" in text_e and "page: 10.00 EUR" in text_e, text_e
    assert "saves" in text_e and "50.00" in text_e

    # ── 7) scheduled_alternate_discovery registers (mocked discovery, no network) ──
    pid_f = seed_product("F")
    s = SessionLocal()
    try:
        for p in s.query(Product).filter(Product.id != pid_f).all():
            p.auto_alternate_links = False
        s.commit()
    finally:
        s.close()

    def fake_find(product_id):
        c = add_candidate(product_id, "https://s6.ro/f")
        return {"success": True, "candidates_found": 1, "new_candidate_ids": [c],
                "pending_candidates": 1, "message": "ok"}

    orig_find = alt.find_alternate_links
    try:
        alt.find_alternate_links = fake_find
        alt.scheduled_alternate_discovery()
    finally:
        alt.find_alternate_links = orig_find
    assert len(notes_for(pid_f)) == 1, "scheduled run must register the notification"

    print("ALL PENDING LINK NOTIFICATION TESTS PASSED")


if __name__ == "__main__":
    main()
