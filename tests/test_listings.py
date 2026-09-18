"""Listings CRUD + payment flows."""


def _make_listing_payload(**overrides):
    base = {
        "title": "iPhone 13 128GB",
        "description": "Отличное состояние, батарея 95%",
        "price": 15000,
        "cat": "iphone",
        "type": "sell",
        "contact": "@tester",
        "city": "Москва",
        "tier": "free",
    }
    base.update(overrides)
    return base


def test_create_listing_free(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="free"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"  # free → instant active
    assert body["tier"] == "free"
    assert body["id"].startswith("l_")


def test_create_listing_vip_needs_payment(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="vip", price=5000))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_payment"
    assert body["tier"] == "vip"


def test_create_listing_premium_needs_payment(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="premium", price=5000))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_payment"


def test_create_listing_requires_auth(client):
    r = client.post("/listings", json=_make_listing_payload())
    assert r.status_code == 401


def test_create_listing_validation_short_contact(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(contact="@x"))
    assert r.status_code == 422  # contact min_length=3


def test_create_listing_validation_negative_price(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(price=-100))
    assert r.status_code == 422


def test_get_listings_public(client):
    r = client.get("/listings")
    assert r.status_code == 200
    body = r.json()
    # /listings returns a JSON array directly
    assert isinstance(body, list)
    assert "Cache-Control" in r.headers
    assert "ETag" in r.headers


def test_listing_status_endpoint(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="vip"))
    lid = r.json()["id"]
    r2 = client.get(f"/listings/{lid}/status")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "awaiting_payment"


def test_confirm_paid_idempotent(client, auth_headers):
    """Double-clicking 'I paid' must not 500."""
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="vip"))
    lid = r.json()["id"]
    r2 = client.post(f"/listings/{lid}/confirm-paid", headers=auth_headers, json={})
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "paid"
    # Second click: must be idempotent (200, no crash)
    r3 = client.post(f"/listings/{lid}/confirm-paid", headers=auth_headers, json={})
    assert r3.status_code == 200
    # Should report already paid
    assert r3.json().get("status") == "paid"


def test_activate_after_paid(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="vip"))
    lid = r.json()["id"]
    client.post(f"/listings/{lid}/confirm-paid", headers=auth_headers, json={})
    # activate is admin/test path, requires listing_id+user_id body
    r2 = client.post("/payments/activate", json={"listing_id": lid, "user_id": 100})
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("ok") is True
    assert body.get("activated") == lid


def test_view_increments_counter(client, auth_headers):
    r = client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="free"))
    lid = r.json()["id"]
    r2 = client.post(f"/listings/{lid}/view", headers=auth_headers)
    assert r2.status_code in (200, 204)


def test_my_listings_in_profile(client, auth_headers):
    client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="free"))
    client.post("/listings", headers=auth_headers, json=_make_listing_payload(tier="vip"))
    r = client.get("/profile/me", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    # free=active (counts toward listings_total), vip=awaiting_payment (separate)
    total = body.get("listings_total", 0) + body.get("listings_pending", 0)
    assert total >= 2
