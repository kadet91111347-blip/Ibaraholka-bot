"""Favorites, search, deals, profile."""


def _make_listing(client, auth_headers, **overrides):
    base = {
        "title": "FeatureTest " + overrides.get("suffix", ""),
        "description": "тест",
        "price": 100,
        "cat": "iphone",
        "type": "sell",
        "contact": "@tester",
        "city": "Москва",
        "tier": "free",
    }
    base.update(overrides)
    r = client.post("/listings", headers=auth_headers, json=base)
    assert r.status_code == 200
    return r.json()["id"]


def test_favorites_add_remove(client, auth_headers):
    lid = _make_listing(client, auth_headers, suffix="Fav1")
    r = client.post(f"/favorites/{lid}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json().get("ok") is True
    r2 = client.get("/favorites", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["count"] >= 1
    r3 = client.delete(f"/favorites/{lid}", headers=auth_headers)
    assert r3.status_code == 200


def test_favorites_list_empty(client, auth_headers):
    r = client.get("/favorites", headers=auth_headers)
    assert r.status_code == 200
    assert "favorites" in r.json()


def test_favorites_requires_auth(client):
    r = client.get("/favorites")
    assert r.status_code == 401


def test_search_endpoint(client, auth_headers):
    _make_listing(client, auth_headers, title="iPhone15 Pro")
    r = client.get("/search", params={"q": "iPhone15"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert isinstance(body.get("listings"), list)


def test_search_by_cat(client, auth_headers):
    _make_listing(client, auth_headers, cat="airpods", title="AirPods Pro 2")
    r = client.get("/search", params={"cat": "airpods"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True


def test_search_by_city(client, auth_headers):
    _make_listing(client, auth_headers, city="Санкт-Петербург", title="СПб тест")
    r = client.get("/search", params={"city": "Петербург"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True


def test_search_max_price(client, auth_headers):
    _make_listing(client, auth_headers, price=50, title="Дешёвка")
    _make_listing(client, auth_headers, price=99999, title="Дорогое")
    r = client.get("/search", params={"max_price": 100})
    assert r.status_code == 200
    for item in r.json().get("listings", []):
        assert item["price"] <= 100


def test_profile_me_no_auth(client):
    r = client.get("/profile/me")
    assert r.status_code == 401


def test_profile_me_with_auth(client, auth_headers):
    r = client.get("/profile/me", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "user_id" in body or "id" in body


def test_deals_create_requires_listing(client, auth_headers):
    r = client.post("/deals/create", headers=auth_headers, json={
        "listing_id": "l_nonexistent",
        "payment_method": "tinkoff",
        "shipping_address": "ул. Тест, 1",
        "shipping_city": "Москва",
    })
    # 200 with ok=false (listing_not_found) or 404 — must not 500
    assert r.status_code in (200, 404)


def test_deals_create_with_real_listing(client, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers, suffix="Deal1", price=1000)
    r = client.post("/deals/create", headers=auth_headers2, json={
        "listing_id": lid,
        "payment_method": "tinkoff",
        "shipping_address": "ул. Тест, 1",
        "shipping_city": "Москва",
    })
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    deal_id = body.get("deal_id")
    assert deal_id is not None

    # Post message
    r2 = client.post(f"/deals/{deal_id}/messages", headers=auth_headers2,
                     json={"text": "Привет, ещё актуально?"})
    assert r2.status_code == 200

    # Get messages
    r3 = client.get(f"/deals/{deal_id}/messages", headers=auth_headers)
    assert r3.status_code == 200
    msgs = r3.json().get("messages", [])
    assert len(msgs) >= 1


def test_match_subscribe(client, auth_headers):
    r = client.post("/match/subscribe", headers=auth_headers, json={
        "cat": "iphone", "max_price": 50000, "city": "Москва",
    })
    assert r.status_code == 200
