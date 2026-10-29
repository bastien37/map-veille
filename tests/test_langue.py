"""Tests du filtre par langue.

La langue vient de la source (config/flux.json), pas d'une détection sur le texte : un
flux de l'IFRI est en français, un flux de War on the Rocks en anglais, et c'est écrit dans
la configuration. L'article la porte, l'événement porte l'ensemble de celles de ses sources.
"""
import json
import sys
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


@pytest.fixture(scope="module")
def gaz():
    return lieux.charger_gazetteer()


@pytest.fixture(scope="module")
def ref():
    return collect.referentiels()


def ajouter(cx, gaz, ref, titre, source, langue, lien=None):
    a = collect.article(titre, lien or f"https://{source}.test/{abs(hash(titre)) % 10**8}",
                        base.maintenant_utc(), source, langue=langue)
    collect.trier(a, ref)
    collect.situer(a, gaz, ref=ref)
    a["zone"] = a["zone"] or "eur"
    a["theme"] = a["theme"] or "dip"
    etat = base.enregistrer(cx, a, collect.normaliser(titre), base.maintenant_utc(), None)
    cx.commit()
    return etat


@pytest.fixture
def cx(tmp_path, gaz, ref):
    c = base.ouvrir(tmp_path / "map.sqlite")
    ajouter(c, gaz, ref, "Inondations meurtrières à Lagos, des milliers de déplacés",
            "Le Monde", "fr")
    ajouter(c, gaz, ref, "Lagos : les inondations font des milliers de déplacés", "RFI", "fr")
    ajouter(c, gaz, ref, "Great power competition in the Indo-Pacific",
            "War on the Rocks", "en")
    ajouter(c, gaz, ref, "Nairobi summit opens on climate finance", "Reuters", "en")
    yield c
    c.close()


@pytest.fixture
def client(cx, monkeypatch, tmp_path):
    monkeypatch.setattr(serveur, "BASE", tmp_path / "map.sqlite")
    serveur._referentiel = None
    return TestClient(serveur.app)


# ---------- stockage ----------
def test_la_langue_de_la_source_est_enregistree(cx):
    langues = dict(cx.execute("SELECT source, langue FROM articles"))
    assert langues["Le Monde"] == "fr" and langues["War on the Rocks"] == "en"


def test_l_evenement_porte_les_langues_de_ses_sources(cx):
    lagos = cx.execute("SELECT * FROM evenements WHERE lieu = 'Lagos'").fetchone()
    assert lagos["nb_sources"] == 2 and lagos["langues"] == ",fr,"
    nairobi = cx.execute("SELECT * FROM evenements WHERE lieu = 'Nairobi'").fetchone()
    assert nairobi["langues"] == ",en,"


# Deux titres qui parlent du même fait dans les deux langues ET partagent assez de mots
# pour être regroupés : un nom propre et une date suffisent souvent.
BILINGUE_FR = "Sommet d'Oslo 2026 : la finance climat en question"
BILINGUE_EN = "Oslo 2026 summit puts climate finance in question"


def test_un_evenement_bilingue_porte_les_deux_langues(cx, gaz, ref):
    """Noms propres et chiffres survivent à la traduction : ces deux-là se regroupent."""
    ajouter(cx, gaz, ref, BILINGUE_FR, "Le Monde", "fr", lien="https://lm.test/oslo")
    ajouter(cx, gaz, ref, BILINGUE_EN, "Reuters", "en", lien="https://rt.test/oslo")
    e = cx.execute("SELECT * FROM evenements WHERE titre = ?", (BILINGUE_FR,)).fetchone()
    assert e is not None and e["nb_sources"] == 2
    assert set(e["langues"].strip(",").split(",")) == {"fr", "en"}


def test_la_purge_recalcule_aussi_les_langues(cx):
    cx.execute("UPDATE evenements SET langues = ',xx,'")
    cx.commit()
    base.purger(cx, base.maintenant_utc())
    assert cx.execute("SELECT langues FROM evenements WHERE lieu = 'Lagos'").fetchone()[0] == ",fr,"


def test_masquer_retire_la_langue_de_l_evenement(cx):
    ident = cx.execute("SELECT id FROM articles WHERE source = 'Reuters'").fetchone()[0]
    base.masquer(cx, ident, True, base.maintenant_utc())
    e = cx.execute("SELECT * FROM evenements WHERE lieu = 'Nairobi'").fetchone()
    assert e["nb_sources"] == 0 and e["langues"] == ""


# ---------- clause SQL ----------
def test_langue_sql():
    assert base.langue_sql(None) == ("", [])
    assert base.langue_sql("fr") == (" AND langue = ?", ["fr"])
    sql, p = base.langue_sql("fr", "e.")
    assert "instr(e.langues, ?)" in sql and p == [",fr,"]


def test_les_virgules_encadrantes_evitent_les_faux_positifs():
    """« ,fr, » ne doit pas se reconnaître dans « ,fra, » ni « ,en, » dans « ,eng, »."""
    _, p = base.langue_sql("en", "e.")
    assert ",eng," .find(p[0]) == -1
    assert ",en,fr,".find(p[0]) == 0


# ---------- API ----------
def test_api_filtre_les_articles(client):
    tout = client.get("/api/articles").json()
    fr = client.get("/api/articles", params={"langue": "fr"}).json()
    en = client.get("/api/articles", params={"langue": "en"}).json()
    assert fr["total"] + en["total"] == tout["total"]
    assert all("Lagos" in a["titre"] or "ordre" in a["titre"] for a in fr["articles"])
    assert {a["titre"] for a in en["articles"]} == {
        "Great power competition in the Indo-Pacific",
        "Nairobi summit opens on climate finance"}


def test_api_filtre_la_carte(client):
    fr = client.get("/api/evenements", params={"langue": "fr"}).json()
    en = client.get("/api/evenements", params={"langue": "en"}).json()
    assert [e["lieu"] for e in fr["evenements"]] == ["Lagos"]
    assert [e["lieu"] for e in en["evenements"]] == ["Nairobi"]


def test_api_filtre_la_synthese(client):
    tout = client.get("/api/synthese").json()["total"]
    fr = client.get("/api/synthese", params={"langue": "fr"}).json()["total"]
    en = client.get("/api/synthese", params={"langue": "en"}).json()["total"]
    assert fr + en == tout and fr > 0 and en > 0


@pytest.mark.parametrize("route", ["/api/articles", "/api/evenements", "/api/synthese"])
def test_langue_inconnue_refusee(client, route):
    assert client.get(route, params={"langue": "es"}).status_code == 422
    assert client.get(route, params={"langue": ""}).status_code == 200
    assert client.get(route, params={"langue": "toutes"}).status_code == 200


def test_l_article_mis_en_avant_est_dans_la_langue_demandee(client, cx, gaz, ref):
    """Un événement bilingue filtré en français ne doit pas afficher son titre anglais."""
    ajouter(cx, gaz, ref, BILINGUE_FR, "Le Monde", "fr", lien="https://lm.test/oslo")
    ajouter(cx, gaz, ref, BILINGUE_EN, "Reuters", "en", lien="https://rt.test/oslo")
    titres_fr = {a["titre"] for a in
                 client.get("/api/articles", params={"langue": "fr"}).json()["articles"]}
    titres_en = {a["titre"] for a in
                 client.get("/api/articles", params={"langue": "en"}).json()["articles"]}
    assert BILINGUE_FR in titres_fr and BILINGUE_EN not in titres_fr
    assert BILINGUE_EN in titres_en and BILINGUE_FR not in titres_en


def test_le_referentiel_annonce_les_langues(client):
    langues = client.get("/api/referentiel").json()["langues"]
    assert [l["code"] for l in langues] == list(base.LANGUES)
    assert {l["nom"] for l in langues} == {"Français", "Anglais"}


def test_le_front_a_un_selecteur_de_langue(client):
    page = client.get("/").text
    assert 'id="langues"' in page and '"map.langue"' in page
    assert "setLangue" in page


# ---------- limite connue ----------
def test_deux_langues_font_deux_evenements(cx, gaz, ref):
    """Limite assumée : le regroupement compare des MOTS.

    Quand les deux titres partagent un nom propre et une date, ils se regroupent (voir
    test_un_evenement_bilingue_porte_les_deux_langues). Quand ils ne partagent que le nom
    du lieu, non : « Inondations meurtrières à Lagos » et « Deadly floods in Lagos » font
    deux points au même endroit. Les réunir demanderait de la traduction ou des plongements
    multilingues, ce que ce projet ne fait pas. Le filtre par langue est la façon de n'en
    voir qu'un."""
    ajouter(cx, gaz, ref, "Deadly floods in Lagos as the toll rises", "Reuters", "en",
            lien="https://reuters.test/lagos")
    lagos = cx.execute("SELECT langues, nb_sources FROM evenements WHERE lieu = 'Lagos' "
                       "ORDER BY nb_sources DESC").fetchall()
    assert len(lagos) == 2, "comportement attendu aujourd'hui, pas un objectif"
    assert {l["langues"] for l in lagos} == {",fr,", ",en,"}


# ---------- langue d'ouverture (config/site.json) ----------
def ecrire_site(tmp_path, **champs):
    f = tmp_path / "site.json"
    f.write_text(json.dumps(champs, ensure_ascii=False), encoding="utf-8")
    return f


def test_le_fichier_livre_ouvre_sur_le_francais():
    assert base.charger_site()["langue_par_defaut"] == "fr"


def test_fichier_absent_vaut_toutes_les_langues(tmp_path):
    assert base.charger_site(tmp_path / "rien.json")["langue_par_defaut"] is None


@pytest.mark.parametrize("valeur, attendu", [("fr", "fr"), ("en", "en"),
                                             (None, None), ("", None), ("toutes", None)])
def test_valeurs_acceptees(tmp_path, valeur, attendu):
    f = ecrire_site(tmp_path, langue_par_defaut=valeur)
    assert base.charger_site(f)["langue_par_defaut"] == attendu


def test_langue_inconnue_refusee(tmp_path):
    with pytest.raises(ValueError, match="langue_par_defaut"):
        base.charger_site(ecrire_site(tmp_path, langue_par_defaut="klingon"))


def test_reglage_inconnu_refuse(tmp_path):
    with pytest.raises(ValueError, match="réglages inconnus"):
        base.charger_site(ecrire_site(tmp_path, couleur="rouge"))


def test_l_api_annonce_la_langue_d_ouverture(client):
    assert client.get("/api/referentiel").json()["langue_par_defaut"] == "fr"


def test_un_reglage_casse_ne_rend_pas_le_site_inaccessible(client, monkeypatch, tmp_path):
    """Une faute de frappe dans un réglage d'affichage ne doit pas renvoyer 500 à tous."""
    monkeypatch.setattr(base, "SITE_JSON", ecrire_site(tmp_path, langue_par_defaut="klingon"))
    serveur._referentiel = None
    r = client.get("/api/referentiel")
    assert r.status_code == 200 and r.json()["langue_par_defaut"] is None
    serveur._referentiel = None


def test_le_front_applique_la_langue_d_ouverture(client):
    page = client.get("/").text
    assert "LANGUE_DEFAUT" in page
    # un choix déjà fait par le visiteur, « Toutes » compris, l'emporte sur la configuration
    assert "lireMemoire(CLE_LANGUE, undefined)" in page
    assert "if(fLangue===undefined)fLangue=LANGUE_DEFAUT" in page
