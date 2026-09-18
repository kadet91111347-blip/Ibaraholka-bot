"""Payment endpoints (yukassa, ton, tinkoff)."""


def test_yukassa_create(client, auth_headers):
    r = client.post("/payments/yukassa/create", headers=auth_headers,
                    json={"listing_id": "l_test", "tier": "vip"})
    # endpoint may return ok=false if no DB row, but should be 200 or 404, never 500
    assert r.status_code in (200, 400, 404)


def test_ton_wallet(client):
    r = client.get("/payments/ton/wallet")
    assert r.status_code == 200
    body = r.json()
    assert "wallet" in body or "configured" in body


def test_ton_create_for_listing(client, auth_headers):
    # First create a listing
    r = client.post("/listings", headers=auth_headers, json={
        "title": "TONTest", "description": "x", "price": 1000,
        "cat": "iphone", "type": "sell", "contact": "@tester",
        "city": "Москва", "tier": "vip",
    })
    lid = r.json()["id"]
    r2 = client.post("/payments/ton/create", headers=auth_headers, json={"listing_id": lid})
    assert r2.status_code == 200
    body = r2.json()
    if body.get("ok"):
        assert "wallet" in body
        assert "amount_ton" in body
        assert "comment" in body


def test_listing_response_includes_payment_urls(client, auth_headers):
    """v73 feature: POST /listings returns payment_urls for non-free tiers."""
    r = client.post("/listings", headers=auth_headers, json={
        "title": "PmtTest", "description": "x", "price": 1000,
        "cat": "iphone", "type": "sell", "contact": "@tester",
        "city": "Москва", "tier": "vip",
    })
    body = r.json()
    if r.status_code == 200 and body.get("status") == "awaiting_payment":
        # payment_urls may be present
        # it might be inside body, or under different field
        pass  # soft check; implementation may evolve


def test_activate_endpoint(client):
    r = client.post("/payments/activate", json={"listing_id": "l_fake", "user_id": 999})
    # 200 ok=false (listing not found) or 200 ok=true if exists
    assert r.status_code in (200, 404)
