"""Tests du regroupement des articles en événements, et des routes de carte.

Changement de comportement assumé : un deuxième article sur le même fait n'est plus jeté.
Il est conservé et rattaché au même événement, qui ne fait qu'un point sur la carte avec
toutes ses sources. Le risque à surveiller n'est donc plus la perte d'articles, mais la
fusion à tort : deux faits distincts réunis sous un seul point.
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(RACINE))
import base  # noqa: E402
import collect  # noqa: E402
import lieux  # noqa: E402
from serveur import app as serveur  # noqa: E402

PAIRES = json.loads((RACINE / "tests" / "regroupement_annote.json").read_text(encoding="utf-8"))["paires"]


@pytest.fixture(scope="module")
def gaz():
    return lieux.charger_gazetteer()


@pytest.fixture(scope="module")
def ref():
    return collect.referentiels()


@pytest.fixture
def cx(tmp_path):
    c = base.ouvrir(tmp_path / "map.sqlite")
    yield c
    c.close()


def ajouter(cx, gaz, ref, titre, source="M", quand=None, lien=None, resume=""):
    """Ajoute un article comme le ferait une collecte.

    zone et thème sont complétés d'office si le tri par mots-clés ne les trouve pas : la vue
    « fil », et donc le compteur de sources de l'événement, ne retient que les articles
    classés (D04). Sans cela les tests mesureraient le tri, pas le regroupement.
    """
    a = collect.article(titre, lien or f"https://{source}.test/{abs(hash(titre + source)) % 10**8}",
                        quand or base.maintenant_utc(), source, resume)
    collect.trier(a, ref)
    collect.situer(a, gaz)
    a["zone"] = a["zone"] or "afr"
    a["theme"] = a["theme"] or "cli"
    etat = base.enregistrer(cx, a, collect.normaliser(titre), base.maintenant_utc(), None)
    cx.commit()
    return etat


# ---------- outils de comparaison ----------
def test_similarite():
    assert base.similarite(frozenset("abc"), frozenset("abc")) == 1.0
    assert base.similarite(frozenset("ab"), frozenset("cd")) == 0.0
    assert base.similarite(frozenset(), frozenset("ab")) == 0.0
    assert base.similarite(frozenset("abc"), frozenset("abd")) == pytest.approx(0.5)


def test_jetons_titre_ecarte_les_mots_vides():
    assert base.jetons_titre(" le sommet de l union europeenne a bruxelles ") == \
        frozenset({"sommet", "union", "europeenne", "bruxelles"})


@pytest.mark.parametrize("a, b, km", [
    ((48.85, 2.35), (48.85, 2.35), 0),
    ((48.85, 2.35), (51.51, -0.13), 344),     # Paris – Londres
    ((48.85, 2.35), (-33.87, 151.21), 16960),  # Paris – Sydney
])
def test_distance_km(a, b, km):
    assert base.distance_km(*a, *b) == pytest.approx(km, abs=km * 0.02 + 1)


# ---------- regroupement ----------
@pytest.mark.parametrize("paire", PAIRES, ids=lambda p: p["a"][:40])
def test_corpus_de_paires(cx, gaz, ref, paire):
    """Sur le corpus annoté, aucune fusion à tort n'est tolérée ; une séparation à tort l'est."""
    ajouter(cx, gaz, ref, paire["a"], "A")
    ajouter(cx, gaz, ref, paire["b"], "B")
    n = cx.execute("SELECT COUNT(*) FROM evenements").fetchone()[0]
    if not paire["ensemble"]:
        assert n == 2, "deux faits distincts ont été réunis sous un seul point"
    else:
        assert n in (1, 2)   # une séparation à tort est acceptable, pas une fusion


def test_aucune_fusion_a_tort_sur_tout_le_corpus(tmp_path, gaz, ref):
    fusions_a_tort, separations_a_tort, bonnes = 0, 0, 0
    for i, p in enumerate(PAIRES):
        c = base.ouvrir(tmp_path / f"p{i}.sqlite")
        ajouter(c, gaz, ref, p["a"], "A")
        ajouter(c, gaz, ref, p["b"], "B")
        groupe = c.execute("SELECT COUNT(*) FROM evenements").fetchone()[0] == 1
        c.close()
        if p["ensemble"] and groupe:
            bonnes += 1
        elif p["ensemble"]:
            separations_a_tort += 1
        elif groupe:
            fusions_a_tort += 1
    attendues = sum(1 for p in PAIRES if p["ensemble"])
    assert fusions_a_tort == 0
    assert bonnes >= attendues - 1, f"{separations_a_tort} séparations à tort"


def test_les_sources_ne_sont_plus_perdues(cx, gaz, ref):
    titre = "Inondations meurtrières à Lagos, des milliers de déplacés"
    assert ajouter(cx, gaz, ref, titre, "Le Monde") == "nouveau"
    assert ajouter(cx, gaz, ref, titre, "RFI") == "regroupe"
    assert ajouter(cx, gaz, ref, titre, "France 24") == "regroupe"
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3
    e = cx.execute("SELECT * FROM evenements").fetchone()
    assert e["nb_sources"] == 3 and e["lieu"] == "Lagos"


def test_le_meme_lien_reste_un_seul_article(cx, gaz, ref):
    titre = "Séisme au Japon"
    ajouter(cx, gaz, ref, titre, "A", lien="https://x.test/1")
    assert ajouter(cx, gaz, ref, titre, "A", lien="https://x.test/1") == "inchange"
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_hors_fenetre_de_temps_deux_evenements(cx, gaz, ref):
    titre = "Inondations meurtrières à Lagos"
    maintenant = base.maintenant_utc()
    ajouter(cx, gaz, ref, titre, "A", quand=maintenant)
    ajouter(cx, gaz, ref, titre, "B", quand=maintenant - base.FENETRE_REGROUPEMENT - timedelta(hours=2))
    assert cx.execute("SELECT COUNT(*) FROM evenements").fetchone()[0] == 2


def test_meme_titre_lieux_eloignes_deux_evenements(cx, gaz, ref):
    """Un titre identique sur deux continents reste deux événements."""
    ajouter(cx, gaz, ref, "Manifestation massive à Tokyo", "A")
    ajouter(cx, gaz, ref, "Manifestation massive à Lima", "B")
    assert cx.execute("SELECT COUNT(*) FROM evenements").fetchone()[0] == 2


def test_la_purge_rattrape_un_compteur_devenu_faux(cx, gaz, ref):
    """Filet de sécurité : le compteur est tenu à jour à chaque écriture, mais une écriture
    directe en base pourrait le laisser faux. La purge, lancée à chaque collecte, recompte
    tout."""
    titre = "Inondations meurtrières à Lagos"
    ajouter(cx, gaz, ref, titre, "A")
    ajouter(cx, gaz, ref, titre, "B")
    cx.execute("UPDATE evenements SET nb_sources = 99")      # compteur faussé à la main
    cx.commit()
    base.purger(cx, base.maintenant_utc())
    assert cx.execute("SELECT nb_sources FROM evenements").fetchone()[0] == 2


def test_un_evenement_sans_source_disparait_a_la_purge(cx, gaz, ref):
    vieux = base.maintenant_utc() - base.DUREE_FIL - timedelta(days=1)
    a = collect.article("Vieille dépêche à Lagos", "https://x.test/1", vieux, "A")
    collect.trier(a, ref)
    collect.situer(a, gaz)
    cx.execute("""INSERT INTO evenements (titre, titre_norm, date_debut, date_fin, nb_sources, maj)
                  VALUES ('Vieux', 'vieux', ?, ?, 1, ?)""",
               (vieux.isoformat(), vieux.isoformat(), vieux.isoformat()))
    eid = cx.execute("SELECT id FROM evenements").fetchone()[0]
    cx.execute("""INSERT INTO articles (lien, titre, titre_norm, source, date_pub, ajout, maj,
                                        evenement_id)
                  VALUES ('https://x.test/9', 'Vieux', 'vieux', 'A', ?, ?, ?, ?)""",
               (vieux.isoformat(), vieux.isoformat(), vieux.isoformat(), eid))
    cx.commit()
    base.purger(cx, base.maintenant_utc())
    assert cx.execute("SELECT COUNT(*) FROM evenements").fetchone()[0] == 0


# ---------- routes de carte ----------
@pytest.fixture
def client(tmp_path, monkeypatch, gaz, ref):
    chemin = tmp_path / "map.sqlite"
    c = base.ouvrir(chemin)
    for titre, source in [
            ("Inondations meurtrières à Lagos, des milliers de déplacés", "Le Monde"),
            ("Lagos : les inondations font des milliers de déplacés", "RFI"),
            ("Séisme de magnitude 6,8 au large du Japon", "France 24"),
            ("Le sommet de l'Union européenne s'ouvre à Bruxelles", "AFP")]:
        ajouter(c, gaz, ref, titre, source)
    c.execute("UPDATE evenements SET zone = COALESCE(zone, 'afr'), theme = COALESCE(theme, 'cli')")
    c.commit()
    c.close()
    monkeypatch.setattr(serveur, "BASE", chemin)
    return TestClient(serveur.app)


def test_route_evenements(client):
    r = client.get("/api/evenements").json()
    assert r["total"] >= 3
    lagos = next(e for e in r["evenements"] if e["lieu"] == "Lagos")
    assert lagos["nb_sources"] == 2        # Le Monde et RFI sur un seul point
    assert -90 <= lagos["lat"] <= 90 and -180 <= lagos["lon"] <= 180


def test_route_evenements_filtre_par_bbox(client):
    tout = client.get("/api/evenements").json()["total"]
    afrique = client.get("/api/evenements", params={"bbox": "-18,-35,52,15"}).json()
    assert 0 < afrique["total"] < tout
    assert all(-35 <= e["lat"] <= 15 and -18 <= e["lon"] <= 52 for e in afrique["evenements"])


@pytest.mark.parametrize("bbox", ["pas-une-bbox", "1,2,3", "0,0,0,200", "0,100,0,10"])
def test_bbox_invalide_refusee(client, bbox):
    assert client.get("/api/evenements", params={"bbox": bbox}).status_code == 422


def test_bbox_qui_franchit_l_antimeridien(client):
    """Une fenêtre Pacifique (ouest > est) se lit en deux morceaux, sans erreur."""
    assert client.get("/api/evenements", params={"bbox": "170,-20,-170,20"}).status_code == 200


def test_route_un_evenement_liste_ses_sources(client):
    lagos = next(e for e in client.get("/api/evenements").json()["evenements"]
                 if e["lieu"] == "Lagos")
    d = client.get(f"/api/evenements/{lagos['id']}").json()
    assert d["nb_sources"] == 2
    assert {s["source"] for s in d["sources"]} == {"Le Monde", "RFI"}
    assert all(s["lien"].startswith("http") and s["titre"] and s["date"] for s in d["sources"])


def test_evenement_inconnu(client):
    assert client.get("/api/evenements/999999").status_code == 404


def test_un_evenement_sans_source_publiable_est_invisible(client, tmp_path):
    """Masquer toutes les sources d'un événement doit le retirer de la carte."""
    lagos = next(e for e in client.get("/api/evenements").json()["evenements"]
                 if e["lieu"] == "Lagos")
    cx = base.ouvrir(serveur.BASE)
    for (ident,) in cx.execute("SELECT id FROM articles WHERE evenement_id = ?",
                               (lagos["id"],)).fetchall():
        base.masquer(cx, ident, True, base.maintenant_utc())
    cx.close()
    assert all(e["id"] != lagos["id"] for e in client.get("/api/evenements").json()["evenements"])
    assert client.get(f"/api/evenements/{lagos['id']}").status_code == 404
