"""Health and version endpoints."""
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert "ts" in body


def test_debug_version(client):
    r = client.get("/debug/version")
    assert r.status_code == 200
    body = r.json()
    assert "local_git_sha" in body or "module_sha" in body


def test_debug_state(client):
    r = client.get("/debug/state")
    assert r.status_code == 200
    body = r.json()
    assert body.get("bot_initialized") is True
    assert body.get("db_kind") in ("sqlite", "postgres")


def test_root(client):
    r = client.get("/")
    assert r.status_code in (200, 404)  # depends on implementation


def test_mini_html(client):
    r = client.get("/mini")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "v=" in r.text or "var v" in r.text
    assert "v74" in r.text or "v=" in r.text  # v74 Reports


def test_mini_head(client):
    r = client.head("/mini")
    assert r.status_code == 200


def test_mini_hero_image(client):
    r = client.get("/mini/img/hero.jpeg")
    assert r.status_code == 200
    assert r.headers.get("content-type") in ("image/jpeg", "image/png", "image/webp")


def test_openapi(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    body = r.json()
    assert len(body.get("paths", {})) > 50
