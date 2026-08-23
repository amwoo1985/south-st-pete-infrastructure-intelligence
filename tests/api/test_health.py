"""Tests for GET /health (app/api/health.py) — both the real happy path
(DB reachable, matches the docker-compose db service being up per
tests/api/conftest.py's requirement) and a real DB-down case (mocked)."""

from __future__ import annotations


def test_health_ok_when_db_reachable(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "db": "ok"}


def test_health_degraded_when_db_unreachable(client, monkeypatch):
    def _raise_connection_error():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr("app.api.health.get_connection", _raise_connection_error)

    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body == {"status": "degraded", "db": "error"}
    # Never leak the raw internal exception message into the response body.
    assert "simulated DB outage" not in resp.text
