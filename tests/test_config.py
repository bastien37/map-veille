"""Tests de la configuration : config/themes.json et config/flux.json.

Les thèmes et les flux ne sont plus codés en dur. Ces tests vérifient que les fichiers
livrés sont valides, que les erreurs de saisie sont rattrapées avec un message lisible,
et que l'application à la base respecte la règle annoncée : le fichier décrit les flux,
l'administration décide lesquels tournent.
"""
import json
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base  # noqa: E402
import collect  # noqa: E402


def ecrire(chemin: Path, cle: str, contenu) -> Path:
    chemin.write_text(json.dumps({cle: contenu}, ensure_ascii=False), encoding="utf-8")
    return chemin


# ---------- les fichiers livrés ----------
def test_les_fichiers_livres_sont_valides():
    themes = base.charger_themes()
    codes = [t["code"] for t in themes]
    assert len(codes) == len(set(codes)) >= 5
    assert codes == [t["code"] for t in sorted(themes, key=lambda x: (x["ordre"], x["code"]))]
    assert all(t["mots_cles"] and t["nom"] for t in themes)
    flux = base.lire_config(base.FLUX_JSON, "flux")
    assert {f["nom"] for f in flux} >= {"Le Monde", "France 24", "RFI", "franceinfo"}
    assert all(f.get("langue") in base.LANGUES for f in flux)
    assert all(f.get("theme") in (None, *base.THEMES) for f in flux)


def test_le_theme_geopolitique_et_ses_flux_sont_livres():
    """Thème demandé pour la veille stratégique, avec ses sources de think tanks."""
    assert "geo" in base.THEMES
    flux = base.lire_config(base.FLUX_JSON, "flux")
    geo = [f for f in flux if f.get("theme") == "geo"]
    assert {f["nom"] for f in geo} >= {"IFRI", "IRSEM", "Le Grand Continent",
                                       "Revue Défense Nationale", "The National Interest"}
    # ces sources publient quelques fois par semaine : les interroger toutes les heures
    # n'aurait aucun sens
    assert all(f["frequence_minutes"] >= 180 for f in geo)
    assert {f["langue"] for f in geo} == {"fr", "en"}
    # chaque adresse non testée doit le dire : personne ne doit croire qu'elles sont sûres
    assert all("VÉRIFIÉE" in f.get("notes", "") or "confirmer" in f.get("notes", "")
               for f in geo)


def test_les_mots_cles_geo_ne_devorent_pas_les_autres_themes():
    """« geo » marque le registre d'analyse, pas les faits : un missile reste « def »."""
    tables = collect.referentiels()["themes"]
    assert collect.classer("Ukraine : frappes de missiles sur Kharkiv", "", tables) == "def"
    assert collect.classer("Le président reçoit l'ambassadeur de Chine", "", tables) == "dip"
    assert collect.classer("Séisme au Japon, alerte au tsunami", "", tables) == "cli"
    for titre in ("La rivalité systémique entre Washington et Pékin",
                  "Vers un ordre international multipolaire ?",
                  "Great power competition in the Indo-Pacific",
                  "Autonomie stratégique européenne : où en est-on ?"):
        assert collect.classer(titre, "", tables) == "geo", titre


def test_les_themes_ne_sont_plus_codes_en_dur():
    source = (RACINE / "collecteur" / "base.py").read_text(encoding="utf-8")
    assert "Conflits et défense" not in source
    assert "Climat et catastrophes naturelles" not in source
    regles = json.loads((RACINE / "collecteur" / "regles.json").read_text(encoding="utf-8"))
    assert "themes" not in regles        # les mots-clés ont déménagé avec les thèmes
    assert "zones" in regles


def test_les_mots_cles_des_themes_servent_bien_au_tri():
    tables = collect.referentiels()["themes"]
    assert collect.classer("Séisme de magnitude 6,8 au Japon", "", tables) == "cli"
    assert collect.classer("Ukraine : frappes de missiles", "", tables) == "def"


# ---------- erreurs de saisie ----------
@pytest.mark.parametrize("themes, message", [
    ([], "aucun thème"),
    ([{"code": "AB", "nom": "x"}], "Code de thème invalide"),
    ([{"code": "a b", "nom": "x"}], "Code de thème invalide"),
    ([{"code": "ab", "nom": ""}], "n'a pas de nom"),
    ([{"code": "ab", "nom": "x"}, {"code": "ab", "nom": "y"}], "deux fois"),
    ([{"code": "ab", "nom": "x", "mots_cles": "guerre"}], "liste de textes"),
])
def test_themes_invalides_refuses(tmp_path, themes, message):
    with pytest.raises(ValueError, match=message):
        base.charger_themes(ecrire(tmp_path / "themes.json", "themes", themes))


def test_fichier_absent_ou_illisible(tmp_path):
    with pytest.raises(FileNotFoundError, match="absent"):
        base.charger_themes(tmp_path / "rien.json")
    mauvais = tmp_path / "themes.json"
    mauvais.write_text("{ceci n'est pas du JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="pas un JSON valide"):
        base.charger_themes(mauvais)


def test_themes_tries_par_ordre(tmp_path):
    themes = base.charger_themes(ecrire(tmp_path / "t.json", "themes", [
        {"code": "zzz", "nom": "Dernier", "ordre": 5},
        {"code": "aaa", "nom": "Premier", "ordre": 1}]))
    assert [t["code"] for t in themes] == ["aaa", "zzz"]


# ---------- application des flux à la base ----------
@pytest.fixture
def cx(tmp_path):
    c = base.ouvrir(tmp_path / "map.sqlite")
    yield c
    c.close()


def test_flux_livres_installes_a_la_creation(cx):
    noms = {s["nom"] for s in base.lister_sources(cx)}
    assert "Le Monde" in noms and "GDACS (ONU / Commission européenne)" in noms
    lemonde = next(s for s in base.lister_sources(cx) if s["nom"] == "Le Monde")
    assert lemonde["langue"] == "fr" and lemonde["frequence_minutes"] == 60
    assert lemonde["origine"] == "config"
    gdacs = next(s for s in base.lister_sources(cx) if s["type"] == "gdacs")
    assert gdacs["theme_impose"] == "cli"


def test_nouveau_flux_ajoute_adresse_mise_a_jour(cx, tmp_path):
    fichier = ecrire(tmp_path / "flux.json", "flux", [
        {"nom": "Le Monde", "type": "rss", "url": "https://nouvelle.test/rss",
         "langue": "en", "frequence_minutes": 15, "theme": "eco"},
        {"nom": "Nouveau média", "type": "rss", "url": "https://nouveau.test/rss"}])
    bilan = base.appliquer_flux(cx, fichier)
    cx.commit()
    assert bilan["ajoutes"] == 1 and bilan["modifies"] == 1
    lemonde = next(s for s in base.lister_sources(cx) if s["nom"] == "Le Monde")
    assert lemonde["url"] == "https://nouvelle.test/rss"
    assert lemonde["langue"] == "en" and lemonde["frequence_minutes"] == 15
    assert lemonde["theme_impose"] == "eco"


def test_flux_retire_du_fichier_est_desactive_pas_supprime(cx, tmp_path):
    fichier = ecrire(tmp_path / "flux.json", "flux",
                     [{"nom": "Le Monde", "type": "rss", "url": "https://x.test/rss"}])
    bilan = base.appliquer_flux(cx, fichier)
    cx.commit()
    assert "France 24" in bilan["desactives"]
    f24 = next(s for s in base.lister_sources(cx) if s["nom"] == "France 24")
    assert f24["actif"] is False      # toujours là, ses articles aussi


def test_le_fichier_ne_repose_pas_actif(cx, tmp_path):
    """Couper une source depuis l'administration doit tenir malgré « actif: true »."""
    ident = next(s["id"] for s in base.lister_sources(cx) if s["nom"] == "Le Monde")
    base.modifier_source(cx, ident, {"actif": False})
    fichier = ecrire(tmp_path / "flux.json", "flux", [
        {"nom": "Le Monde", "type": "rss",
         "url": "https://www.lemonde.fr/international/rss_full.xml", "actif": True}])
    base.appliquer_flux(cx, fichier)
    cx.commit()
    assert base.lire_source(cx, ident)["actif"] is False


def test_source_reprise_en_main_n_est_plus_ecrasee(cx, tmp_path):
    ident = next(s["id"] for s in base.lister_sources(cx) if s["nom"] == "Le Monde")
    base.modifier_source(cx, ident, {"url": "https://choisie-a-la-main.test/rss"})
    assert base.lire_source(cx, ident)["origine"] == "admin"
    fichier = ecrire(tmp_path / "flux.json", "flux", [
        {"nom": "Le Monde", "type": "rss", "url": "https://du-fichier.test/rss"}])
    bilan = base.appliquer_flux(cx, fichier)
    cx.commit()
    assert bilan["a_la_main"] == ["Le Monde"]
    assert base.lire_source(cx, ident)["url"] == "https://choisie-a-la-main.test/rss"


def test_activer_une_source_ne_la_reprend_pas_en_main(cx):
    ident = next(s["id"] for s in base.lister_sources(cx) if s["nom"] == "Le Monde")
    base.modifier_source(cx, ident, {"actif": False})
    assert base.lire_source(cx, ident)["origine"] == "config"


def test_flux_declare_deux_fois_refuse(cx, tmp_path):
    fichier = ecrire(tmp_path / "flux.json", "flux", [
        {"nom": "Doublon", "type": "rss", "url": "https://a.test/rss"},
        {"nom": "Doublon", "type": "rss", "url": "https://b.test/rss"}])
    with pytest.raises(ValueError, match="deux fois"):
        base.appliquer_flux(cx, fichier)


@pytest.mark.parametrize("flux, message", [
    ({"nom": "X", "type": "rss", "url": "https://x.test/f", "langue": "klingon"}, "Langue inconnue"),
    ({"nom": "X", "type": "rss", "url": "https://x.test/f", "frequence_minutes": 0}, "fréquence"),
    ({"nom": "X", "type": "rss", "url": "https://x.test/f", "frequence_minutes": 99999}, "fréquence"),
    ({"nom": "X", "type": "rss", "url": "https://x.test/f", "theme": "inconnu"}, "Thème imposé inconnu"),
    ({"nom": "X", "type": "rss", "url": "pas-une-adresse"}, "adresse"),
])
def test_flux_invalide_refuse(cx, tmp_path, flux, message):
    with pytest.raises(ValueError, match=message):
        base.appliquer_flux(cx, ecrire(tmp_path / "flux.json", "flux", [flux]))


# ---------- thèmes appliqués à la base ----------
def test_theme_retire_de_la_configuration_est_desactive(cx, monkeypatch):
    cx.execute("INSERT INTO themes (code, nom, actif, ordre) VALUES ('old', 'Ancien', 1, 99)")
    base.appliquer_themes(cx)
    cx.commit()
    ligne = cx.execute("SELECT actif FROM themes WHERE code = 'old'").fetchone()
    assert ligne["actif"] == 0        # désactivé, pas supprimé


def test_modifier_la_configuration_declenche_une_migration(cx):
    """a_jour() doit redevenir faux si un nom de thème change dans la configuration."""
    assert base.a_jour(cx)
    cx.execute("UPDATE themes SET nom = 'Autre nom' WHERE code = 'def'")
    cx.commit()
    assert not base.a_jour(cx)


# ---------- fréquence de rafraîchissement ----------
def test_source_pas_encore_due(cx):
    from datetime import timedelta
    maintenant = base.maintenant_utc()
    s = {"frequence_minutes": 60, "dernier_succes": None}
    assert base.source_a_rafraichir(s, maintenant)
    s["dernier_succes"] = (maintenant - timedelta(minutes=10)).isoformat()
    assert not base.source_a_rafraichir(s, maintenant)
    s["dernier_succes"] = (maintenant - timedelta(minutes=61)).isoformat()
    assert base.source_a_rafraichir(s, maintenant)
    s["frequence_minutes"] = 5
    s["dernier_succes"] = (maintenant - timedelta(minutes=10)).isoformat()
    assert base.source_a_rafraichir(s, maintenant)
