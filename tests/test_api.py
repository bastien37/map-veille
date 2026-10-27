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
    assert r["total"] == 3
    assert r["zones"]["asi"] == {"total": 1, "nouveaux": 1}
    assert client.get("/api/synthese", params={"theme": "cli"}).json()["total"] == 3


def test_articles_filtres_et_pagination(client):
    r = client.get("/api/articles", params={"zone": "asi"}).json()
    assert r["total"] == 1 and {a["zone"] for a in r["articles"]} == {"asi"}
    assert all(a["nouveau"] is True for a in r["articles"])
    dates = [a["date"] for a in client.get("/api/articles").json()["articles"]]
    assert dates == sorted(dates, reverse=True)
    page2 = client.get("/api/articles", params={"limite": 2, "decalage": 2}).json()
    assert len(page2["articles"]) == 1 and page2["total"] == 3


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


# ---------- compression et cache HTTP ----------
def test_reponses_compressees(client):
    """Le référentiel pèse 87 Kio : il doit partir compressé."""
    nu = client.get("/api/referentiel", headers={"Accept-Encoding": "identity"})
    comprime = client.get("/api/referentiel", headers={"Accept-Encoding": "gzip"})
    assert comprime.headers["content-encoding"] == "gzip"
    assert int(comprime.headers["content-length"]) < len(nu.content) / 2
    assert comprime.json() == nu.json()


def test_petites_reponses_non_compressees(client):
    """En dessous de 512 octets, compresser coûte plus que cela ne rapporte."""
    r = client.get("/api/collectes", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers


@pytest.mark.parametrize("route, cache", [
    ("/api/referentiel", "max-age=3600"),
    ("/api/synthese", "max-age=60"),
    ("/api/articles", "max-age=60"),
])
def test_entetes_de_cache(client, route, cache):
    r = client.get(route)
    assert cache in r.headers["cache-control"]
    assert r.headers["etag"]


@pytest.mark.parametrize("route", ["/api/referentiel", "/api/synthese", "/api/articles"])
def test_304_si_le_navigateur_a_deja_la_version(client, route):
    etag = client.get(route).headers["etag"]
    rejoue = client.get(route, headers={"If-None-Match": etag})
    assert rejoue.status_code == 304 and not rejoue.content
    assert client.get(route, headers={"If-None-Match": '"autre"'}).status_code == 200


def test_le_front_ne_demande_pas_deux_fois_les_donnees(client):
    """Le rappel des polices recalculait la géométrie en redemandant tout à l'API."""
    page = client.get("/").text
    assert "document.fonts.ready.then(placerEtiquettes)" in page
    assert "fonts.ready.then(actualiser)" not in page
    assert 'cache:"no-store"' not in page


def test_les_polices_ne_bloquent_pas_le_rendu(client):
    page = client.get("/").text
    assert 'media="print" onload="this.media=\'all\'"' in page
    assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in page
