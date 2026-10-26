"""Tests de l'interface d'administration (serveur/admin.py). Lancer avec : uv run pytest"""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(RACINE))
import collect  # noqa: E402
from serveur import admin  # noqa: E402
from serveur import app as serveur  # noqa: E402

MDP = "mot-de-passe-de-test"
AUTH = ("admin", MDP)
TESTS = RACINE / "tests"


@pytest.fixture
def client(tmp_path, monkeypatch):
    base_test = tmp_path / "map.sqlite"
    collect.main(["--base", str(base_test), "--migrer", str(tmp_path / "rien.json"),
                  "--fichiers", f"Test={TESTS / 'flux_test_rss.xml'}", f"Atom={TESTS / 'flux_test_atom.xml'}"])
    monkeypatch.setattr(serveur, "BASE", base_test)
    monkeypatch.setenv("MAP_ADMIN_MOT_DE_PASSE", MDP)
    return TestClient(serveur.app)


# ---------- accès ----------
def test_acces_refuse_sans_mot_de_passe(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/api/admin/tableau").status_code == 401
    assert client.get("/api/admin/tableau", auth=("admin", "faux")).status_code == 401


def test_administration_desactivee_sans_variable(client, monkeypatch):
    monkeypatch.delenv("MAP_ADMIN_MOT_DE_PASSE")
    assert client.get("/api/admin/tableau", auth=AUTH).status_code == 503


def test_page_admin(client):
    r = client.get("/admin", auth=AUTH)
    assert r.status_code == 200 and "Administration" in r.text


# ---------- tableau de bord et listes ----------
def test_tableau(client):
    d = client.get("/api/admin/tableau", auth=AUTH).json()
    assert d["compteurs"]["a_trier"] == 1
    assert d["compteurs"]["total"] == 9
    assert d["croise"]["asi"]["cli"] == 1


def test_recherche_et_filtres(client):
    a_trier = client.get("/api/admin/articles", params={"statut": "a_trier"}, auth=AUTH).json()
    assert [a["titre"] for a in a_trier["articles"]] == ["Réforme des retraites : le débat reprend à l'Assemblée"]
    trouve = client.get("/api/admin/articles", params={"q": "seisme"}, auth=AUTH).json()   # sans accent
    assert trouve["total"] == 1 and trouve["articles"][0]["zone"] == "asi"
    sans_zone = client.get("/api/admin/articles", params={"zone": "aucun"}, auth=AUTH).json()
    assert sans_zone["total"] == 1


# ---------- modifications ----------
def test_classer_un_article_le_publie(client):
    ident = client.get("/api/admin/articles", params={"statut": "a_trier"}, auth=AUTH).json()["articles"][0]["id"]
    r = client.patch(f"/api/admin/articles/{ident}", json={"zone": "eur", "theme": "dip"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["manuel"] is True
    public = client.get("/api/articles", params={"zone": "eur", "theme": "dip"}).json()
    assert any(a["id"] == ident for a in public["articles"])


def test_masquer_retire_du_site_public(client):
    public = client.get("/api/articles").json()
    ident, total = public["articles"][0]["id"], public["total"]
    assert client.patch(f"/api/admin/articles/{ident}", json={"masque": True}, auth=AUTH).json()["masque"] is True
    apres = client.get("/api/articles").json()
    assert apres["total"] == total - 1 and all(a["id"] != ident for a in apres["articles"])
    client.patch(f"/api/admin/articles/{ident}", json={"masque": False}, auth=AUTH)
    assert client.get("/api/articles").json()["total"] == total


def test_modifications_refusees(client):
    assert client.patch("/api/admin/articles/9999", json={"masque": True}, auth=AUTH).status_code == 404
    assert client.patch("/api/admin/articles/1", json={"zone": "xyz", "theme": "def"}, auth=AUTH).status_code == 422
    assert client.patch("/api/admin/articles/1", json={"masque": True}).status_code == 401


# ---------- collectes ----------
def test_lancer_une_collecte_hors_ligne(client):
    r = client.post("/api/admin/collecte", params={"hors_ligne": True}, auth=AUTH)
    assert r.status_code == 202
    assert client.post("/api/admin/collecte", auth=AUTH).status_code == 409   # déjà en cours
    for _ in range(100):
        etat = client.get("/api/admin/collecte", auth=AUTH).json()
        if not etat["en_cours"]:
            break
        time.sleep(0.2)
    assert etat["code"] == 0, etat["sortie"]
    journal = client.get("/api/admin/collectes", auth=AUTH).json()
    assert journal[0]["hors_ligne"] is True and len(journal) == 2


# ---------- verrous SQLite (erreur « database is locked ») ----------
def test_administration_lisible_pendant_une_ecriture(client):
    """Un autre programme écrit dans la base (collecte en cours) : l'administration doit répondre."""
    import sqlite3
    autre = sqlite3.connect(serveur.BASE, isolation_level=None)
    autre.execute("BEGIN IMMEDIATE")
    autre.execute("INSERT INTO collectes (debut) VALUES ('2026-10-26T00:00:00+00:00')")
    try:
        assert client.get("/api/admin/tableau", auth=AUTH).status_code == 200
        assert client.get("/api/admin/collectes", auth=AUTH).status_code == 200
        assert client.get("/api/articles").status_code == 200
    finally:
        autre.execute("ROLLBACK")
        autre.close()


def test_migration_v1_sans_verrou_bloquant(tmp_path):
    """Une base v1 (sans colonne masque) est migrée, puis rouverte sans aucune écriture."""
    import sqlite3
    import base
    chemin = tmp_path / "v1.sqlite"
    cx = sqlite3.connect(chemin)
    cx.executescript(base.SCHEMA.replace(
        "    masque      INTEGER NOT NULL DEFAULT 0 CHECK (masque IN (0, 1)),  -- retiré du site par un administrateur\n", ""))
    cx.execute("PRAGMA user_version = 1")
    cx.commit()
    cx.close()
    base.ouvrir(chemin).close()
    cx = base.ouvrir(chemin)
    assert "masque" in {r["name"] for r in cx.execute("PRAGMA table_info(articles)")}
    assert base.a_jour(cx) and cx.in_transaction is False


def test_collecte_ne_verrouille_pas_pendant_le_reseau(tmp_path, monkeypatch):
    """Pendant qu'une source est interrogée, un autre programme peut écrire dans la base."""
    import sqlite3
    chemin = tmp_path / "map.sqlite"
    collect.main(["--base", str(chemin), "--migrer", str(tmp_path / "rien.json"), "--hors-ligne"])
    vu = {}

    def source_lente(s, hors_ligne, *args):
        autre = sqlite3.connect(chemin, timeout=0)
        autre.execute("UPDATE articles SET maj = maj WHERE id = 1")   # demande le verrou d'écriture
        autre.commit()
        autre.close()
        vu["ecriture_possible"] = True
        return []
    monkeypatch.setattr(collect, "source_gdacs", source_lente)
    collect.main(["--base", str(chemin), "--hors-ligne"])
    assert vu.get("ecriture_possible")


def test_connexion_utilisable_depuis_un_autre_thread(client):
    """FastAPI ouvre la dépendance dans un thread et exécute la route dans un autre."""
    import threading
    gen = admin.connexion_ecriture()
    ouverte = {}
    t = threading.Thread(target=lambda: ouverte.update(cx=next(gen)))
    t.start()
    t.join()
    assert ouverte["cx"].execute("SELECT COUNT(*) FROM fil").fetchone()[0] > 0   # thread principal
    with pytest.raises(StopIteration):
        next(gen)   # fermeture depuis le thread principal
