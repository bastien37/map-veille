"""Tests de l'API (serveur/app.py). Lancer avec : uv run pytest"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(RACINE))
import collect  # noqa: E402
from serveur import app as serveur  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    base_test = tmp_path / "map.sqlite"
    collect.main(["--base", str(base_test), "--migrer", str(tmp_path / "rien.json"), "--hors-ligne"])
    monkeypatch.setattr(serveur, "BASE", base_test)
    return TestClient(serveur.app)


def test_referentiel(client):
    r = client.get("/api/referentiel").json()
    assert [z["code"] for z in r["zones"]] == ["eur", "rus", "mena", "afr", "asi", "ame"]
    assert all(z["d"].startswith("M") and len(z["etiquette"]) == 2 for z in r["zones"])
    assert [t["code"] for t in r["themes"]] == ["def", "dip", "eco", "tec", "cli"]


def test_synthese(client):
    r = client.get("/api/synthese").json()
    assert r["total"] == 6
    assert r["zones"]["asi"] == {"total": 2, "nouveaux": 2}
    assert client.get("/api/synthese", params={"theme": "cli"}).json()["total"] == 3


def test_articles_filtres_et_pagination(client):
    r = client.get("/api/articles", params={"zone": "asi"}).json()
    assert r["total"] == 2 and {a["zone"] for a in r["articles"]} == {"asi"}
    assert all(a["nouveau"] is True for a in r["articles"])
    dates = [a["date"] for a in client.get("/api/articles").json()["articles"]]
    assert dates == sorted(dates, reverse=True)
    page2 = client.get("/api/articles", params={"limite": 4, "decalage": 4}).json()
    assert len(page2["articles"]) == 2 and page2["total"] == 6


def test_codes_inconnus_refuses(client):
    assert client.get("/api/articles", params={"zone": "xyz"}).status_code == 422
    assert client.get("/api/synthese", params={"theme": "xyz"}).status_code == 422


def test_journal_des_collectes(client):
    j = client.get("/api/collectes").json()
    assert j[0]["hors_ligne"] is True and {s["statut"] for s in j[0]["sources"]} == {"ok"}


def test_base_absente(tmp_path, monkeypatch):
    monkeypatch.setattr(serveur, "BASE", tmp_path / "absente.sqlite")
    assert TestClient(serveur.app).get("/api/articles").status_code == 503


def test_front_sans_donnees(client):
    page = client.get("/")
    assert page.status_code == 200 and 'const API="api"' in page.text
    for interdit in ("MAP_DATA", "data.json", "Conflits et défense", "Europe"):
        assert interdit not in page.text
