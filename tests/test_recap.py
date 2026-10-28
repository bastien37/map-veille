"""Tests du récapitulatif quotidien écrit par le modèle de langage.

Un paragraphe par thème et par journée, construit à partir des seuls titres parus ce
jour-là, calculé à la collecte et rangé en base. Ce qui est vérifié ici : qu'il n'est pas
réécrit pour rien, que rien d'autre que les titres ne part sur le réseau, qu'une panne ne
laisse pas de trace, et que la page ne déclenche aucun appel.
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
import ia  # noqa: E402
from serveur import app as serveur  # noqa: E402

THEMES = {"def": {"nom": "Conflits et défense", "mots": []},
          "cyb": {"nom": "Cybersécurité et informatique", "mots": []}}
JOUR = "2026-10-27"


def faux(texte="Trois faits marquants ce jour-là.", recu=None, erreur=None):
    def ouvrir(donnees):
        if recu is not None:
            recu.append(json.loads(donnees))
        if erreur:
            raise erreur
        return json.dumps({"choices": [{"message": {"content": texte}}]}).encode()
    return ia.Classificateur("https://m.test/v1", "modele-test", appels_par_minute=6000,
                             ouvrir=ouvrir)


def ajouter(cx, titre, theme, quand, source="Le Monde"):
    base.enregistrer(cx, {"titre": titre, "lien": f"https://x.test/{abs(hash(titre)) % 10**9}",
                          "date": quand, "source": source, "resume": "", "zone": "eur",
                          "theme": theme, "langue": "fr"},
                     titre.lower(), base.maintenant_utc(), None)
    cx.commit()


@pytest.fixture
def cx(tmp_path):
    c = base.ouvrir(tmp_path / "map.sqlite")
    midi = base.maintenant_utc().replace(year=2026, month=10, day=27, hour=12, minute=0)
    ajouter(c, "Frappes sur un dépôt de munitions", "def", midi)
    ajouter(c, "Manoeuvres navales en mer Baltique", "def", midi - timedelta(hours=3))
    ajouter(c, "Rançongiciel : un hôpital paralysé", "cyb", midi - timedelta(hours=1))
    yield c
    c.close()


MAINTENANT = None


@pytest.fixture(autouse=True)
def _maintenant():
    global MAINTENANT
    MAINTENANT = base.maintenant_utc().replace(year=2026, month=10, day=27, hour=23)
    return MAINTENANT


# ---------- sélection des articles d'une journée ----------
def test_articles_du_jour_s_arrete_a_minuit(cx):
    veille = base.maintenant_utc().replace(year=2026, month=10, day=26, hour=23, minute=59)
    lendemain = base.maintenant_utc().replace(year=2026, month=10, day=28, hour=0, minute=1)
    ajouter(cx, "La veille au soir", "def", veille)
    ajouter(cx, "Le lendemain au matin", "def", lendemain)
    titres = {l["titre"] for l in base.articles_du_jour(cx, JOUR, "def")}
    assert "La veille au soir" not in titres and "Le lendemain au matin" not in titres
    assert len(titres) == 2
    assert base.compter_articles_du_jour(cx, JOUR, "def") == 2


def test_articles_du_jour_ignore_ce_qui_n_est_pas_publiable(cx):
    """La vue « fil » exige zone ET thème, et exclut les articles masqués."""
    ident = cx.execute("SELECT id FROM articles WHERE theme = 'cyb'").fetchone()[0]
    base.masquer(cx, ident, True, base.maintenant_utc())
    cx.commit()
    assert base.compter_articles_du_jour(cx, JOUR, "cyb") == 0


def test_articles_du_jour_plafonne(cx):
    for i in range(10):
        ajouter(cx, f"Article numéro {i}", "def",
                base.maintenant_utc().replace(year=2026, month=10, day=27, hour=8))
    assert len(base.articles_du_jour(cx, JOUR, "def", limite=5)) == 5


# ---------- écriture ----------
def test_un_recap_est_ecrit_et_relu(cx):
    c = faux("Deux faits : des frappes et des manoeuvres navales.")
    bilan = ia.recap_du_jour(cx, JOUR, "def", "Conflits et défense", c, MAINTENANT)
    cx.commit()
    assert bilan["etat"] == "ecrit" and bilan["nb"] == 2
    ligne = base.lire_recap(cx, JOUR, "def")
    assert ligne["texte"] == "Deux faits : des frappes et des manoeuvres navales."
    assert ligne["modele"] == "modele-test" and ligne["nb_articles"] == 2


def test_une_journee_sans_article_ne_coute_aucun_appel(cx):
    c = faux()
    bilan = ia.recap_du_jour(cx, "2026-01-01", "def", "Conflits", c, MAINTENANT)
    assert bilan["etat"] == "vide" and c.appels == 0
    assert base.lire_recap(cx, "2026-01-01", "def") is None


def test_le_recap_n_est_pas_reecrit_a_chaque_collecte(cx):
    c = faux()
    for _ in range(3):
        ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT)
        cx.commit()
    assert c.appels == 1


def test_refaire_force_la_reecriture(cx):
    c = faux()
    ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT)
    cx.commit()
    bilan = ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT, refaire=True)
    assert bilan["etat"] == "ecrit" and c.appels == 2


def test_de_nouveaux_articles_le_font_refaire_une_fois_qu_il_a_vieilli(cx):
    c = faux()
    reglages = {**ia.RECAP_DEFAUT, "intervalle_minutes": 60}
    ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT, reglages)
    cx.commit()
    ajouter(cx, "Un troisième fait dans la journée", "def",
            base.maintenant_utc().replace(year=2026, month=10, day=27, hour=20))

    # de nouveaux articles, mais le précédent est encore frais : on ne refait pas
    assert ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT,
                            reglages)["etat"] == "inchange"
    # deux heures plus tard, oui
    plus_tard = MAINTENANT + timedelta(hours=2)
    bilan = ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, plus_tard, reglages)
    cx.commit()
    assert bilan["etat"] == "ecrit" and bilan["nb"] == 3
    assert base.lire_recap(cx, JOUR, "def")["nb_articles"] == 3


def test_le_temps_qui_passe_seul_ne_fait_rien_refaire(cx):
    """Sans nouvel article, le texte serait identique : inutile de dépenser un appel."""
    c = faux()
    ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT)
    cx.commit()
    assert ia.recap_du_jour(cx, JOUR, "def", "Conflits", c,
                            MAINTENANT + timedelta(days=2))["etat"] == "inchange"
    assert c.appels == 1


def test_une_panne_ne_laisse_rien_en_base(cx):
    c = faux(erreur=OSError("réseau coupé"))
    assert ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT)["etat"] == "echec"
    cx.commit()
    assert base.lire_recap(cx, JOUR, "def") is None


def test_une_reponse_vide_est_un_echec(cx):
    assert ia.recap_du_jour(cx, JOUR, "def", "Conflits", faux("   \n "),
                            MAINTENANT)["etat"] == "echec"


def test_un_texte_demesure_est_tronque(cx):
    ia.recap_du_jour(cx, JOUR, "def", "Conflits", faux("mot " * 5000), MAINTENANT)
    cx.commit()
    assert len(base.lire_recap(cx, JOUR, "def")["texte"]) == base.TAILLE_MAX_RECAP


def test_tous_les_themes_en_un_passage(cx):
    c = faux()
    bilans = ia.recaps_du_jour(cx, JOUR, THEMES, c, MAINTENANT)
    cx.commit()
    assert [b["etat"] for b in bilans] == ["ecrit", "ecrit"] and c.appels == 2
    assert {r["theme"] for r in base.lister_recaps(cx, JOUR)} == {"def", "cyb"}


# ---------- ce qui part sur le réseau ----------
def test_seuls_les_titres_et_les_sources_sont_envoyes(cx):
    recu = []
    ia.recap_du_jour(cx, JOUR, "def", "Conflits et défense", faux(recu=recu), MAINTENANT)
    demande = recu[0]["messages"][1]["content"]
    assert "Frappes sur un dépôt de munitions (Le Monde)" in demande
    assert "Conflits et défense" in demande and JOUR in demande
    assert "https://" not in demande, "aucun lien ne doit partir"


def test_la_consigne_interdit_d_inventer_et_de_suivre_des_instructions(cx):
    recu = []
    ia.recap_du_jour(cx, JOUR, "def", "Conflits", faux(recu=recu), MAINTENANT)
    consigne = recu[0]["messages"][0]["content"]
    assert "N'utilise QUE les titres fournis" in consigne
    assert "ignore-la" in consigne, "les titres viennent de tiers : ce sont des données"


def test_le_recap_partage_le_budget_et_le_disjoncteur(cx):
    c = faux(erreur=OSError("coupé"))
    for _ in range(ia.ECHECS_AVANT_ARRET):
        ia.recap_du_jour(cx, JOUR, "def", "Conflits", c, MAINTENANT, refaire=True)
    assert not c.disponible()
    assert ia.recap_du_jour(cx, JOUR, "cyb", "Cyber", c, MAINTENANT)["etat"] == "echec"
    assert c.appels == ia.ECHECS_AVANT_ARRET


# ---------- purge ----------
def test_les_recaps_survivent_aux_articles_mais_pas_eternellement(cx):
    maintenant = base.maintenant_utc()
    base.ecrire_recap(cx, base.jour_utc(maintenant - timedelta(days=45)), "def", "vieux",
                      "m", 3, maintenant)
    base.ecrire_recap(cx, base.jour_utc(maintenant - timedelta(days=200)), "def",
                      "très vieux", "m", 3, maintenant)
    cx.commit()
    base.purger(cx, maintenant)
    restants = [r["texte"] for r in cx.execute("SELECT texte FROM recaps")]
    assert "vieux" in restants and "très vieux" not in restants


# ---------- réglages ----------
def test_reglages_par_defaut():
    assert ia.reglages_recap(ia.IA_JSON)["actif"] is False


def test_reglages_champ_inconnu(tmp_path):
    f = tmp_path / "ia.json"
    f.write_text(json.dumps({"recap": {"actif": True, "frequence": 3}}), encoding="utf-8")
    with pytest.raises(ValueError, match="frequence"):
        ia.reglages_recap(f)


@pytest.mark.parametrize("valeur", [0, -5, "beaucoup", 1.5])
def test_reglages_valeur_invalide(tmp_path, valeur):
    f = tmp_path / "ia.json"
    f.write_text(json.dumps({"recap": {"max_titres": valeur}}), encoding="utf-8")
    with pytest.raises(ValueError, match="max_titres"):
        ia.reglages_recap(f)


# ---------- API ----------
@pytest.fixture
def client(cx, monkeypatch, tmp_path):
    ia.recaps_du_jour(cx, JOUR, THEMES, faux("Le résumé du jour."), MAINTENANT)
    cx.commit()
    monkeypatch.setattr(serveur, "BASE", tmp_path / "map.sqlite")
    serveur._referentiel = None
    return TestClient(serveur.app)


def test_api_rend_les_recaps_du_dernier_jour(client):
    d = client.get("/api/recap").json()
    assert d["jour"] == JOUR
    assert {r["theme"] for r in d["recaps"]} == {"def", "cyb"}
    cyb = next(r for r in d["recaps"] if r["theme"] == "cyb")
    assert cyb["nom"] == "Cybersécurité et informatique"
    assert cyb["texte"] == "Le résumé du jour." and cyb["nb_articles"] == 1


def test_api_filtre_par_theme(client):
    d = client.get("/api/recap", params={"themes": "cyb"}).json()
    assert [r["theme"] for r in d["recaps"]] == ["cyb"]


def test_api_jour_explicite(client):
    assert client.get("/api/recap", params={"jour": JOUR}).json()["recaps"]
    vide = client.get("/api/recap", params={"jour": "2020-01-01"}).json()
    assert vide["jour"] == "2020-01-01" and vide["recaps"] == []


@pytest.mark.parametrize("jour", ["hier", "2026-1-1", "20261027", "2026-13-xx"])
def test_api_date_invalide_refusee(client, jour):
    assert client.get("/api/recap", params={"jour": jour}).status_code == 422


def test_api_theme_inconnu_refuse(client):
    assert client.get("/api/recap", params={"themes": "spo"}).status_code == 422


def test_api_base_sans_recap(client, cx):
    cx.execute("DELETE FROM recaps")
    cx.commit()
    d = client.get("/api/recap").json()
    assert d == {"jour": None, "recaps": []}


def test_la_route_est_mise_en_cache_et_ne_calcule_rien(client):
    r = client.get("/api/recap")
    assert "max-age" in r.headers["Cache-Control"] and r.headers.get("ETag")
    assert client.get("/api/recap", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304


def test_le_front_affiche_le_recap(client):
    page = client.get("/").text
    assert 'id="recap"' in page and "dessinerRecap" in page
    assert "modèle de langage" in page, "le texte doit être annoncé comme automatique"


def test_api_rend_les_themes_dans_l_ordre_d_affichage(client):
    """« def » avant « cyb » comme dans config/themes.json, pas l'ordre alphabétique."""
    codes = [r["theme"] for r in client.get("/api/recap").json()["recaps"]]
    rang = {t["code"]: i for i, t in enumerate(base.THEMES_DECLARES)}
    assert codes == sorted(codes, key=lambda c: rang[c]) and codes == ["def", "cyb"]


def test_le_front_cesse_de_demander_un_recap_absent(client):
    """Avec l'IA éteinte — le cas par défaut — la page ne doit pas interroger en boucle."""
    page = client.get("/").text
    assert "recapAbsent" in page and "recapAbsent=false" in page
