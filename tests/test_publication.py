"""Tests de la règle de diffusion (D04, révisée en v15).

Un article est diffusé s'il a un thème, et une zone **ou** un thème imposé par sa source.
La règle d'origine exigeait zone ET thème ; elle datait de la carte des six zones, où un
article sans zone n'avait nulle part où s'afficher. Sur 1 372 articles réels, elle en
écartait un tiers — surtout des avis du CERT-FR et des notes d'instituts, qui ne nomment
aucun lieu.
"""
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


def poser(cx, titre, zone, theme, force=False, masque=False):
    base.enregistrer(cx, {"titre": titre, "lien": f"https://x.test/{abs(hash(titre)) % 10**9}",
                          "date": base.maintenant_utc(), "source": "Source", "resume": "",
                          "zone": zone, "theme": theme, "langue": "fr",
                          "theme_force": force},
                     titre.lower(), base.maintenant_utc(), None)
    if masque:
        ident = cx.execute("SELECT id FROM articles WHERE titre = ?", (titre,)).fetchone()[0]
        base.masquer(cx, ident, True, base.maintenant_utc())
    cx.commit()


@pytest.fixture
def cx(tmp_path):
    c = base.ouvrir(tmp_path / "map.sqlite")
    yield c
    c.close()


def dans(cx, vue):
    return {r["titre"] for r in cx.execute(f"SELECT titre FROM {vue}")}


# ---------- la règle ----------
def test_zone_et_theme_restent_diffuses(cx):
    poser(cx, "Frappes sur Kharkiv", "rus", "def")
    assert "Frappes sur Kharkiv" in dans(cx, "fil")


def test_theme_impose_sans_zone_est_maintenant_diffuse(cx):
    """Le cas qui motive le changement : un avis de sécurité ne nomme aucun lieu."""
    poser(cx, "Faille critique corrigée en urgence", None, "cyb", force=True)
    assert "Faille critique corrigée en urgence" in dans(cx, "fil")
    assert "Faille critique corrigée en urgence" not in dans(cx, "a_trier")


def test_un_flux_generaliste_sans_zone_reste_a_trier(cx):
    """Sans thème imposé ni zone, rien ne garantit que l'article ait sa place."""
    poser(cx, "Un titre sans lieu ni source thématique", None, "dip", force=False)
    assert "Un titre sans lieu ni source thématique" in dans(cx, "a_trier")
    assert "Un titre sans lieu ni source thématique" not in dans(cx, "fil")


def test_sans_theme_rien_n_est_diffuse(cx):
    """Le thème reste obligatoire, imposé ou non : c'est lui qui range l'article."""
    poser(cx, "Avec une zone mais sans thème", "eur", None, force=True)
    assert "Avec une zone mais sans thème" in dans(cx, "a_trier")
    assert "Avec une zone mais sans thème" not in dans(cx, "fil")


def test_un_article_masque_n_est_dans_aucune_vue(cx):
    poser(cx, "Masqué", "eur", "def", masque=True)
    assert "Masqué" not in dans(cx, "fil") and "Masqué" not in dans(cx, "a_trier")


def test_les_deux_vues_se_partagent_exactement_les_articles(cx):
    """« a_trier » est le complément de « fil » : aucun article ne peut tomber entre les deux."""
    poser(cx, "a", "eur", "def")
    poser(cx, "b", None, "cyb", force=True)
    poser(cx, "c", None, "dip")
    poser(cx, "d", "eur", None)
    poser(cx, "e", None, None)
    poser(cx, "f", "eur", "def", masque=True)
    fil, a_trier = dans(cx, "fil"), dans(cx, "a_trier")
    assert fil == {"a", "b"} and a_trier == {"c", "d", "e"}
    assert not (fil & a_trier)
    visibles = {r["titre"] for r in cx.execute("SELECT titre FROM articles WHERE masque = 0")}
    assert fil | a_trier == visibles


# ---------- le drapeau vient de la source ----------
def test_le_drapeau_est_pose_par_la_collecte(cx, tmp_path):
    """collecter_toutes() marque les articles des flux à thème imposé."""
    chemin = tmp_path / "collecte.sqlite"
    collect.main(["--base", str(chemin), "--hors-ligne"])      # GDACS impose « cli »
    c2 = base.ouvrir(chemin)
    try:
        lignes = c2.execute("SELECT theme, theme_force, zone FROM articles").fetchall()
        assert lignes and all(r["theme_force"] == 1 and r["theme"] == "cli" for r in lignes)
        # et ils sont diffusés, zone ou pas
        assert c2.execute("SELECT COUNT(*) FROM fil").fetchone()[0] == len(lignes)
    finally:
        c2.close()


def test_classer_a_la_main_diffuse_toujours(cx):
    poser(cx, "À classer", None, None)
    ident = cx.execute("SELECT id FROM articles").fetchone()[0]
    base.classer_a_la_main(cx, ident, "asi", "def", base.maintenant_utc())
    cx.commit()
    assert "À classer" in dans(cx, "fil")


# ---------- migration d'une base existante ----------
def test_la_migration_rattrape_les_articles_deja_en_base(tmp_path):
    """Un article déjà connu n'est jamais réécrit : sans rattrapage, un tiers du fonds
    resterait invisible pour toujours."""
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    nom_impose = next(s["nom"] for s in base.lister_sources(cx) if s["theme_impose"])
    cx.execute("INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme, "
               "ajout, maj) VALUES ('https://x.test/1', 'Ancien', 'ancien', ?, ?, NULL, 'cli', "
               "?, ?)", (nom_impose, base.maintenant_utc().isoformat(),
                         base.maintenant_utc().isoformat(), base.maintenant_utc().isoformat()))
    cx.execute("INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme, "
               "ajout, maj) VALUES ('https://x.test/2', 'Généraliste', 'generaliste', "
               "'Le Monde', ?, NULL, 'dip', ?, ?)",
               (base.maintenant_utc().isoformat(), base.maintenant_utc().isoformat(),
                base.maintenant_utc().isoformat()))
    # on remet la base dans l'état d'avant v15 : les vues d'abord, sinon SQLite refuse de
    # retirer une colonne à laquelle elles font référence. La migration les recrée.
    cx.execute("DROP VIEW fil")
    cx.execute("DROP VIEW a_trier")
    cx.execute("ALTER TABLE articles DROP COLUMN theme_force")
    cx.execute("PRAGMA user_version = 14")
    cx.commit()
    cx.close()

    cx = base.ouvrir(chemin)      # déclenche la migration
    try:
        assert base.a_jour(cx)
        drapeaux = dict(cx.execute("SELECT titre, theme_force FROM articles"))
        assert drapeaux == {"Ancien": 1, "Généraliste": 0}
        assert dans(cx, "fil") == {"Ancien"}
        assert dans(cx, "a_trier") == {"Généraliste"}
    finally:
        cx.close()


# ---------- côté API ----------
@pytest.fixture
def client(cx, monkeypatch, tmp_path):
    poser(cx, "Avis de sécurité sans lieu", None, "cyb", force=True)
    poser(cx, "Frappes sur Kharkiv", "rus", "def")
    monkeypatch.setattr(serveur, "BASE", tmp_path / "map.sqlite")
    serveur._referentiel = None
    return TestClient(serveur.app)


def test_l_api_sert_les_articles_sans_zone(client):
    titres = {a["titre"] for a in
              client.get("/api/articles", params={"grouper": False}).json()["articles"]}
    assert titres == {"Avis de sécurité sans lieu", "Frappes sur Kharkiv"}


def test_un_filtre_par_zone_les_laisse_de_cote(client):
    """Demander une zone, c'est demander les articles qui en ont une."""
    titres = {a["titre"] for a in client.get(
        "/api/articles", params={"zone": "rus", "grouper": False}).json()["articles"]}
    assert titres == {"Frappes sur Kharkiv"}


def test_la_synthese_compte_les_articles_sans_zone_a_part(client):
    d = client.get("/api/synthese", params={"zones": 1}).json()
    assert d["total"] == 2
    assert d["sans_zone"] == {"total": 1, "nouveaux": 1}
    assert d["zones"]["rus"]["total"] == 1
    assert set(d["zones"]) == set(base.ZONES), "pas de case « None » dans les six zones"
