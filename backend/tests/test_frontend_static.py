import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>shell</title>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
    monkeypatch.setattr(main, "frontend_path", str(dist))
    app = FastAPI()
    app.mount("/", main.FrontendFiles(directory=str(dist), html=True), name="frontend")
    app.add_exception_handler(404, main.spa_not_found_exception_handler)
    return TestClient(app)


def test_index_is_revalidated_on_every_load(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "shell" in response.text


def test_spa_route_gets_the_shell_without_caching(client):
    response = client.get("/projects/abc")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert "shell" in response.text


def test_missing_hashed_asset_is_a_404_not_the_shell(client):
    response = client.get("/assets/nope-123.js")
    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/html")
    assert response.json() == {"detail": "Not Found"}


def test_hashed_asset_is_cached_as_immutable(client):
    response = client.get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_api_404_keeps_its_json_detail(client):
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
