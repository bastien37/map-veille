"""Tests des sources stockées en base et de leur gestion (base.py, collect.py, administration)."""
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(RACINE))
import base  # noqa: E402
import collect  # noqa: E402
from serveur import app as serveur  # noqa: E402

AUTH = ("admin", "mdp-test")
TESTS = RACINE / "tests"


@pytest.fixture
def chemin(tmp_path):
    return tmp_path / "map.sqlite"


@pytest.fixture
def client(chemin, monkeypatch):
    base.ouvrir(chemin).close()
    monkeypatch.setattr(serveur, "BASE", chemin)
    monkeypatch.setenv("MAP_ADMIN_MOT_DE_PASSE", AUTH[1])
    return TestClient(serveur.app)


# ---------- base ----------
def test_sources_initiales(chemin):
    cx = base.ouvrir(chemin)
    sources = base.lister_sources(cx)
    assert len(sources) == len(base.lire_config(base.FLUX_JSON, "flux"))
    actives = {s["nom"] for s in base.lister_sources(cx, actives=True)}
    declarees_actives = {f["nom"] for f in base.lire_config(base.FLUX_JSON, "flux")
                         if f.get("actif", True)}
    assert actives == declarees_actives
    assert {"Le Monde", "France 24", "RFI", "franceinfo"} <= actives
    gdacs = next(s for s in sources if s["type"] == "gdacs")
    assert gdacs["parametres"] == {"niveaux": ["orange", "red"], "jours": 7}
    assert base.config_collecte(gdacs)["niveaux"] == "orange;red"


def test_migration_v2_vers_v3_ajoute_les_sources(chemin):
    cx = sqlite3.connect(chemin)
    schema_v2 = base.SCHEMA.split("CREATE TABLE IF NOT EXISTS sources")[0]
    cx.executescript(schema_v2)
    cx.execute("PRAGMA user_version = 2")
    cx.commit()
    cx.close()
    cx = base.ouvrir(chemin)
    assert len(base.lister_sources(cx)) == len(base.lire_config(base.FLUX_JSON, "flux")) and base.a_jour(cx)


@pytest.mark.parametrize("saisie, message", [
    ({"nom": "", "type": "rss", "url": "https://a.fr/rss"}, "nom"),
    ({"nom": "X", "type": "ftp"}, "Type inconnu"),
    ({"nom": "X", "type": "rss", "url": "a.fr/rss"}, "http"),
    ({"nom": "X", "type": "gdacs", "parametres": {"niveaux": []}}, "niveau"),
    ({"nom": "X", "type": "gdacs", "parametres": {"jours": 90}}, "30 jours"),
    ({"nom": "X", "type": "reliefweb", "parametres": {"theme_defaut": "zzz"}}, "Thème"),
])
def test_validation(saisie, message):
    with pytest.raises(ValueError, match=message):
        base.valider_source(saisie)


def test_renommer_reporte_sur_les_articles(chemin):
    collect.main(["--base", str(chemin), "--migrer", "rien.json", "--hors-ligne"])
    cx = base.ouvrir(chemin)
    gdacs = next(s for s in base.lister_sources(cx) if s["type"] == "gdacs")
    base.modifier_source(cx, gdacs["id"], {"nom": "GDACS"})
    assert cx.execute("SELECT COUNT(*) FROM articles WHERE source = 'GDACS'").fetchone()[0] == 3
    assert cx.execute("SELECT COUNT(*) FROM collectes_sources WHERE source = 'GDACS'").fetchone()[0] == 1


def test_collecte_ignore_les_sources_desactivees(chemin):
    cx = base.ouvrir(chemin)
    gdacs = next(s for s in base.lister_sources(cx) if s["type"] == "gdacs")
    base.modifier_source(cx, gdacs["id"], {"actif": False})
    cx.close()
    with pytest.raises(SystemExit, match="Aucune source"):   # plus aucune source à interroger
        collect.main(["--base", str(chemin), "--migrer", "rien.json", "--hors-ligne"])
    cx = base.ouvrir(chemin)
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0


# ---------- administration ----------
def test_admin_liste_les_sources_avec_leur_etat(client, chemin):
    collect.main(["--base", str(chemin), "--hors-ligne"])
    d = client.get("/api/admin/sources", auth=AUTH).json()
    assert len(d["sources"]) == len(base.lire_config(base.FLUX_JSON, "flux"))
    assert "rss" in d["types"]
    gdacs = next(s for s in d["sources"] if s["type"] == "gdacs")
    assert gdacs["articles"] == 3 and "fichier_test" not in gdacs
    assert client.get("/api/admin/sources").status_code == 401


def test_admin_ajouter_modifier_supprimer(client):
    nouvelle = {"nom": "Le Figaro International", "type": "rss",
                "url": "https://www.lefigaro.fr/rss/figaro_international.xml", "notes": "À valider LT3"}
    r = client.post("/api/admin/sources", json=nouvelle, auth=AUTH)
    assert r.status_code == 201 and r.json()["actif"] is True
    ident = r.json()["id"]
    assert client.post("/api/admin/sources", json=nouvelle, auth=AUTH).status_code == 422   # doublon de nom
    r = client.patch(f"/api/admin/sources/{ident}", json={"actif": False}, auth=AUTH)
    assert r.json()["actif"] is False and r.json()["url"] == nouvelle["url"]
    assert client.patch(f"/api/admin/sources/{ident}", json={"url": "pas-une-url"}, auth=AUTH).status_code == 422
    assert client.delete(f"/api/admin/sources/{ident}", auth=AUTH).status_code == 204
    assert client.delete(f"/api/admin/sources/{ident}", auth=AUTH).status_code == 404
    declares = len(base.lire_config(base.FLUX_JSON, "flux"))
    assert len(client.get("/api/admin/sources", auth=AUTH).json()["sources"]) == declares


def test_admin_tester_une_source_sans_enregistrer(client, monkeypatch, chemin):
    monkeypatch.setattr(collect, "http", lambda *a, **k: (TESTS / "flux_test_rss.xml").read_bytes())
    r = client.post("/api/admin/sources/tester", auth=AUTH,
                    json={"nom": "Essai", "type": "rss", "url": "https://exemple.test/rss"}).json()
    assert r["ok"] is True and r["total"] == 7 and r["a_trier"] == 1
    assert {"zone": "rus", "theme": "def"}.items() <= r["apercu"][0].items()
    cx = base.ouvrir(chemin)
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0          # rien d'écrit
    assert len(base.lister_sources(cx)) == len(base.lire_config(base.FLUX_JSON, "flux"))


def test_admin_tester_une_source_en_panne(client, monkeypatch):
    def panne(*a, **k):
        raise OSError("site injoignable")
    monkeypatch.setattr(collect, "http", panne)
    r = client.post("/api/admin/sources/tester", auth=AUTH,
                    json={"nom": "Essai", "type": "rss", "url": "https://exemple.test/rss"}).json()
    assert r["ok"] is False and "injoignable" in r["message"]
