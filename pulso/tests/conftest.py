"""Shared by every test file. `docker compose run --rm pulso` mounts the live ./data and the real
ntfy topic: every test gets a tmp database, and nothing may reach the phone, a webhook or the
weekly schedule."""
import pytest

from app import db, store


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("PULSO_COLLECT", "off")
    monkeypatch.setenv("PULSO_ALERTS", "off")
    monkeypatch.setenv("PULSO_GUARDIA", "off")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(store, "_ids", {})
    return db.connect()
