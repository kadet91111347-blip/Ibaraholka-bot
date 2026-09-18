"""Reports (жалобы на листинги) + admin moderation."""


def _make_listing(client, auth_headers, **overrides):
    base = {
        "title": "ReportTest",
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
    return r.json()["id"]


def test_report_create(client, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r = client.post(f"/reports/{lid}", headers=auth_headers2,
                    json={"reason": "spam", "comment": "подозрительно"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("reason") == "spam"


def test_report_invalid_reason(client, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r = client.post(f"/reports/{lid}", headers=auth_headers2,
                    json={"reason": "nuclear"})
    assert r.status_code == 200
    assert r.json().get("error") == "invalid_reason"


def test_report_cannot_report_own(client, auth_headers):
    lid = _make_listing(client, auth_headers)
    r = client.post(f"/reports/{lid}", headers=auth_headers,
                    json={"reason": "spam"})
    assert r.status_code == 200
    assert r.json().get("error") == "cannot_report_own"


def test_report_double(client, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r1 = client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "spam"})
    assert r1.json().get("ok") is True
    r2 = client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "fraud"})
    assert r2.json().get("error") == "already_reported"


def test_report_listing_not_found(client, auth_headers2):
    r = client.post("/reports/l_nonexistent", headers=auth_headers2, json={"reason": "spam"})
    assert r.json().get("error") == "listing_not_found"


def test_report_requires_auth(client):
    r = client.post("/reports/l_xxx", json={"reason": "spam"})
    assert r.status_code == 401


def test_admin_reports_list_requires_auth(client):
    r = client.get("/admin/reports")
    assert r.status_code == 403


def test_admin_reports_list(client, admin_token, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "spam"})
    r = client.get("/admin/reports", headers={"Authorization": admin_token})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body["count"] >= 1


def test_admin_resolve_report(client, admin_token, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r1 = client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "fraud"})
    rid = r1.json().get("report_id")
    assert rid is not None

    r2 = client.post(
        f"/admin/reports/{rid}/resolve",
        headers={"Authorization": admin_token},
        json={"action": "dismiss"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("ok") is True
    assert body.get("new_status") == "dismissed"
    assert body.get("deleted_listing") is False


def test_admin_resolve_with_delete(client, admin_token, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r1 = client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "spam"})
    rid = r1.json()["report_id"]
    r2 = client.post(
        f"/admin/reports/{rid}/resolve",
        headers={"Authorization": admin_token},
        json={"action": "action_taken", "delete_listing": True},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("deleted_listing") is True


def test_admin_resolve_already_resolved(client, admin_token, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r1 = client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "spam"})
    rid = r1.json()["report_id"]
    client.post(f"/admin/reports/{rid}/resolve", headers={"Authorization": admin_token}, json={"action": "dismiss"})
    r2 = client.post(f"/admin/reports/{rid}/resolve", headers={"Authorization": admin_token}, json={"action": "dismiss"})
    assert r2.json().get("error") == "already_resolved"


def test_admin_resolve_invalid_action(client, admin_token, auth_headers, auth_headers2):
    lid = _make_listing(client, auth_headers)
    r1 = client.post(f"/reports/{lid}", headers=auth_headers2, json={"reason": "spam"})
    rid = r1.json()["report_id"]
    r2 = client.post(f"/admin/reports/{rid}/resolve", headers={"Authorization": admin_token}, json={"action": "nuke"})
    assert r2.json().get("error") == "action must be dismiss|action_taken"


def test_admin_resolve_report_not_found(client, admin_token):
    r = client.post("/admin/reports/999999999/resolve", headers={"Authorization": admin_token}, json={"action": "dismiss"})
    assert r.json().get("error") == "report_not_found"
