"""Tests for the health endpoint."""

from fastapi.testclient import TestClient


def test_health_answers_ok(client):
    response = client.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_health_is_answered_to_other_machines_too(app):
    remote = TestClient(app, base_url="http://api.example.com", client=("203.0.113.7", 50000))
    assert remote.get("/health").status_code == 200
