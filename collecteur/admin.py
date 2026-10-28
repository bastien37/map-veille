#!/usr/bin/env python3
"""
MAP – Outil d'administration de la base SQLite.

    uv run collecteur/admin.py stats                     # chiffres clés et dernières collectes
    uv run collecteur/admin.py config                    # thèmes et sources issus de config/
    uv run collecteur/admin.py verifier-flux             # quelles sources répondent vraiment
    uv run collecteur/admin.py verifier-flux --decouvrir # et cherche l'adresse de celles qui échouent
    uv run collecteur/admin.py ia                        # ce que le modèle de langage a classé
    uv run collecteur/admin.py recap                     # récapitulatif du jour, thème par thème
    uv run collecteur/admin.py a-trier                   # articles que le tri n'a pas su classer
    uv run collecteur/admin.py classer 42 asi def        # classe l'article 42 à la main
    uv run collecteur/admin.py chercher "Taïwan"         # recherche dans les titres

Zones  : eur rus mena afr asi ame
Thèmes : voir config/themes.json (« admin.py config » les affiche)
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import base


def cmd_stats(cx, args):
    total = cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    fil = cx.execute("SELECT COUNT(*) FROM fil").fetchone()[0]
    a_trier = cx.execute("SELECT COUNT(*) FROM a_trier").fetchone()[0]
    manuels = cx.execute("SELECT COUNT(*) FROM articles WHERE manuel = 1").fetchone()[0]
    print(f"Articles : {total} au total, {fil} classés ({manuels} à la main), {a_trier} à trier\n")

    print("Articles classés par zone et par thème (toute la base) :")
    themes = list(base.THEMES)
    print(f"  {'':34}" + "".join(f"{t:>6}" for t in themes) + f"{'total':>7}")
    for code, nom in [*base.ZONES.items(), (None, "(sans zone — flux à thème imposé)")]:
        ou = "zone = ?" if code else "zone IS NULL"
        n = dict(cx.execute(f"SELECT theme, COUNT(*) FROM fil WHERE {ou} GROUP BY theme",
                            (code,) if code else ()).fetchall())
        print(f"  {nom:34}" + "".join(f"{n.get(t, 0):>6}" for t in themes) + f"{sum(n.values()):>7}")

    print("\nArticles par source :")
    for r in cx.execute("SELECT source, COUNT(*) n FROM articles GROUP BY source ORDER BY n DESC LIMIT 15"):
        print(f"  {r['n']:>5}  {r['source']}")

    print("\nDernières collectes :")
    for c in cx.execute("SELECT * FROM collectes ORDER BY id DESC LIMIT 5").fetchall():
        mode = " (hors ligne)" if c["hors_ligne"] else ""
        print(f"  n° {c['id']} – {c['debut'][:16].replace('T', ' ')} UTC{mode} : "
              f"{c['nb_lus']} lus, {c['nb_nouveaux']} nouveaux, {c['nb_doublons']} doublons, "
              f"{c['nb_supprimes']} supprimés")
        for s in cx.execute("SELECT * FROM collectes_sources WHERE collecte_id = ?", (c["id"],)):
            detail = f"{s['nb']} éléments" if s["statut"] == "ok" else s["message"]
            print(f"      [{s['statut']:6}] {s['source']} : {detail}")


def _afficher(lignes):
    for r in lignes:
        z, t = r["zone"] or "?", r["theme"] or "?"
        print(f"{r['id']:>6}  {r['date_pub'][:10]}  {z:>4} {t:>3}  {r['source'][:22]:22}  {r['titre']}")


def cmd_a_trier(cx, args):
    lignes = cx.execute("SELECT * FROM a_trier ORDER BY date_pub DESC LIMIT ?", (args.limite,)).fetchall()
    if not lignes:
        print("Rien à trier.")
        return
    print(f"{'id':>6}  {'date':10}  zone thè  {'source':22}  titre")
    _afficher(lignes)
    print("\nPour classer : uv run collecteur/admin.py classer <id> <zone> <thème>")


def cmd_config(cx, args):
    """Montre la configuration telle qu'elle vient d'être appliquée à la base.
    base.ouvrir() a déjà relu config/themes.json et config/flux.json : si un fichier est
    invalide, la commande s'arrête avant d'arriver ici, avec le message d'erreur."""
    print(f"Thèmes déclarés dans {base.THEMES_JSON.relative_to(base.RACINE)} :")
    print(f"  {'code':6}{'ordre':>6}  {'actif':7}{'mots-clés':>10}  nom")
    for t in base.THEMES_DECLARES:
        print(f"  {t['code']:6}{t['ordre']:>6}  {'oui' if t['actif'] else 'NON':7}"
              f"{len(t['mots_cles']):>10}  {t['nom']}")
    inactifs = [r["code"] for r in cx.execute("SELECT code FROM themes WHERE actif = 0")]
    if inactifs:
        print(f"  (thèmes désactivés encore présents en base : {', '.join(inactifs)})")

    print(f"\nAffichage du site, d'après {base.SITE_JSON.relative_to(base.RACINE)} :")
    try:
        for cle, valeur in base.charger_site().items():
            print(f"  {cle:20} {valeur if valeur is not None else '(toutes)'}")
    except ValueError as e:
        print(f"  ERREUR — {e}\n  Le site sert le réglage par défaut en attendant.")

    print(f"\nSources d'après {base.FLUX_JSON.relative_to(base.RACINE)} :")
    print(f"  {'nom':34}{'type':11}{'lg':4}{'fréq':>6}  {'thème':7}{'origine':9}actif")
    for s in base.lister_sources(cx):
        print(f"  {s['nom'][:33]:34}{s['type']:11}{s['langue']:4}"
              f"{s['frequence_minutes']:>5}m  {s['theme_impose'] or '—':7}"
              f"{s['origine']:9}{'oui' if s['actif'] else 'non'}")
    a_la_main = [s["nom"] for s in base.lister_sources(cx) if s["origine"] == "admin"]
    if a_la_main:
        print(f"\n  Reprises en main depuis l'administration (le fichier ne s'y applique plus) :"
              f"\n    {', '.join(a_la_main)}")


def _note_verifiee(ancienne: str, n: int, quand: str) -> str:
    """Remplace l'avertissement « adresse non vérifiée » par le constat de la vérification.

    Le reste de la note — ce qu'est la source, à quoi elle sert — est gardé : seule la
    phrase d'avertissement, qui n'a plus lieu d'être, disparaît.
    """
    gardees = [phrase.strip() for phrase in (ancienne or "").split(". ")
               if phrase.strip() and "VÉRIFIÉE" not in phrase and "confirmer" not in phrase
               and "verifier-flux" not in phrase and "réseau sortant" not in phrase]
    constat = f"Adresse vérifiée le {quand} : {n} articles lus"
    return ". ".join([*gardees, constat]) + "."


def _ecrire_flux(modifs: dict, quand: str) -> int:
    """Reporte dans config/flux.json les adresses trouvées et les vérifications réussies.

    modifs : nom de source -> {"url": …} et/ou {"articles": n}. Le fichier est réécrit
    entièrement, commentaire de tête compris ; les sources absentes de `modifs` ne bougent pas.
    """
    contenu = json.loads(base.FLUX_JSON.read_text(encoding="utf-8"))
    touches = 0
    for f in contenu.get("flux", []):
        m = modifs.get(f.get("nom"))
        if not m:
            continue
        if m.get("url"):
            f["url"] = m["url"]
        if m.get("articles") is not None:
            f["notes"] = _note_verifiee(f.get("notes", ""), m["articles"], quand)
        touches += 1
    base.FLUX_JSON.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    return touches


def cmd_verifier_flux(cx, args):
    """Interroge chaque source sans rien enregistrer, et dit laquelle répond.

    À lancer après avoir ajouté des flux dans config/flux.json : une adresse se devine mal,
    et une source muette ne se remarque autrement qu'au bout de quelques collectes.

    --decouvrir va plus loin : pour chaque source qui échoue, il demande au site lui-même
    où est son flux (annonce <link rel="alternate"> de la page d'accueil), puis essaie les
    chemins conventionnels. --ecrire reporte le résultat dans config/flux.json.
    """
    import collect
    sources = [s for s in base.lister_sources(cx) if args.toutes or s["actif"]]
    if args.nom:
        motif = args.nom.lower()
        sources = [s for s in sources if motif in s["nom"].lower()]
    if not sources:
        print("Aucune source à vérifier.")
        return
    ref = collect.referentiels()
    print(f"Vérification de {len(sources)} sources (rien n'est enregistré)…\n")
    print(f"  {'':6}{'source':34}{'lg':4}{'articles':>9}  détail")

    def essayer(s):
        conf = base.config_collecte(s)
        conf["etag"] = conf["derniere_modif"] = None   # on veut le contenu, pas un 304
        debut = time.perf_counter()
        try:
            lot = collect.collecter_source(conf, False, ref)
        except Exception as e:
            return s, None, f"{type(e).__name__}: {e}", time.perf_counter() - debut
        return s, lot, None, time.perf_counter() - debut

    bons, muets, casses, modifs = [], [], [], {}
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
        for s, lot, erreur, duree in pool.map(essayer, sources):
            if erreur:
                casses.append((s, erreur))
                print(f"  {'NON':6}{s['nom'][:33]:34}{s['langue']:4}{'—':>9}  {erreur[:70]}")
                continue
            utiles = [a for a in lot if a["titre"] and a["lien"].startswith("http")]
            dates = [a["date"] for a in utiles if a["date"]]
            recent = max(dates).strftime("%d/%m/%Y") if dates else "sans date"
            if not utiles:
                muets.append(s)
                print(f"  {'vide':6}{s['nom'][:33]:34}{s['langue']:4}{0:>9}  "
                      f"répond en {duree:.1f} s mais ne donne aucun article exploitable")
            else:
                bons.append(s)
                modifs[s["nom"]] = {"articles": len(utiles)}
                print(f"  {'ok':6}{s['nom'][:33]:34}{s['langue']:4}{len(utiles):>9}  "
                      f"{duree:.1f} s, plus récent le {recent}")

    print(f"\n{len(bons)} sources répondent, {len(muets)} vides, {len(casses)} en erreur.")
    a_revoir = [s for s, _ in casses] + muets
    trouves = _decouvrir(a_revoir, modifs) if args.decouvrir and a_revoir else []

    if args.ecrire:
        if not modifs:
            print("\nRien à écrire dans config/flux.json.")
        else:
            n = _ecrire_flux(modifs, base.maintenant_utc().strftime("%d/%m/%Y"))
            print(f"\nconfig/flux.json : {n} sources mises à jour "
                  f"({len(trouves)} adresses corrigées). Relancer une collecte pour l'appliquer.")
            a_la_main = [s["nom"] for s in a_revoir
                         if s["origine"] == "admin" and s["nom"] in modifs]
            if a_la_main:
                print("  Attention : ces sources ont été reprises en main depuis "
                      f"l'administration, le fichier ne s'y applique plus : {', '.join(a_la_main)}")
    elif a_revoir:
        print("\nPour chacune : corriger l'adresse dans config/flux.json, ou la désactiver")
        print("depuis l'administration (onglet Sources), ou la retirer du fichier — une")
        print("source retirée est désactivée, ses articles déjà collectés restent.")
        if not args.decouvrir:
            print("Ou laisser la commande chercher l'adresse : --decouvrir, puis --ecrire.")
        for s in a_revoir:
            if s["nom"] not in modifs:
                print(f"  à revoir : {s['nom']} — {s['url']}")


def _decouvrir(sources, modifs: dict) -> list:
    """Cherche une adresse de flux pour chaque source qui a échoué. Renseigne `modifs`."""
    import collect
    # seules les sources RSS ont une adresse à découvrir : GDACS et ReliefWeb sont des API
    # dont l'adresse est construite par le code, pas lue dans la configuration.
    sources = [s for s in sources if s["type"] == "rss" and s["url"]]
    if not sources:
        print("\nAucune source RSS à chercher (les API n'ont pas d'adresse configurable).")
        return []
    print(f"\nRecherche d'une adresse pour {len(sources)} sources…")

    def chercher(s):
        journal = []
        try:
            return s, collect.decouvrir_flux(s["url"], journal=journal), journal, None
        except Exception as e:
            return s, None, journal, e

    trouves = []
    # quatre à la fois : la découverte interroge plusieurs fois le même site, et le verrou
    # par domaine de collect.http() y sérialise déjà les requêtes
    with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
        for s, trouve, journal, erreur in pool.map(chercher, sources):
            if trouve:
                trouves.append((s, trouve))
                modifs.setdefault(s["nom"], {}).update(url=trouve["url"],
                                                       articles=trouve["articles"])
                via = "annoncée par le site" if trouve["annonce"] else "chemin conventionnel"
                print(f"  TROUVÉ  {s['nom']}\n            {trouve['url']}"
                      f"\n            {trouve['articles']} articles, {via}")
            else:
                detail = f" ({type(erreur).__name__})" if erreur else ""
                print(f"  rien    {s['nom']} — {len(journal)} adresses essayées{detail}")
    print(f"\n{len(trouves)} adresses trouvées sur {len(sources)}.")
    if trouves:
        print("Relancer avec --ecrire pour les reporter dans config/flux.json.")
    return trouves


def cmd_ia(cx, args):
    """État du classement par modèle de langage : configuration, cache, et ce qu'il a changé."""
    import ia
    try:
        classificateur = ia.charger_classificateur()
    except ValueError as e:
        print(f"config/ia.json : {e}\n")
        classificateur = None
    if classificateur:
        print(f"Actif — {classificateur.modele} sur {classificateur.url}")
        print(f"  déclenché « {ia.quand_demander()} », au plus "
              f"{classificateur.budget} appels par collecte, "
              f"{60 / classificateur.intervalle:.0f} par minute\n")
    else:
        print("Inactif : les mots-clés de config/themes.json classent seuls.")
        print("Pour l'activer, voir le commentaire en tête de config/ia.json.\n")

    total, sans = cx.execute(
        "SELECT COUNT(*), COALESCE(SUM(theme IS NULL), 0) FROM themes_ia").fetchone()
    if not total:
        print("Aucune question posée pour l'instant (la table themes_ia est vide).")
        return
    print(f"Cache : {total} titres soumis, dont {sans} que le modèle n'a pas su classer.")
    print("Un titre déjà soumis n'est jamais resoumis, même après redémarrage.\n")

    print("Thèmes attribués par le modèle (articles encore en base) :")
    lignes = cx.execute("SELECT theme, COUNT(*) n FROM articles WHERE theme_ia = 1 "
                        "GROUP BY theme ORDER BY n DESC").fetchall()
    if not lignes:
        print("  aucun — le modèle n'a rien corrigé, ou ces articles ont été purgés.")
    for r in lignes:
        print(f"  {r['n']:>5}  {base.THEMES.get(r['theme'], r['theme'] or '?')}")

    print(f"\n{args.limite} derniers articles classés par le modèle :")
    recents = cx.execute("SELECT * FROM articles WHERE theme_ia = 1 "
                         "ORDER BY ajout DESC LIMIT ?", (args.limite,)).fetchall()
    if recents:
        print(f"{'id':>6}  {'date':10}  thè  {'source':22}  titre")
        for r in recents:
            print(f"{r['id']:>6}  {r['date_pub'][:10]}  {r['theme'] or '?':>3}  "
                  f"{r['source'][:22]:22}  {r['titre'][:70]}")
        print("\nUn classement qui vous paraît faux se corrige à la main :")
        print("  uv run collecteur/admin.py classer <id> <zone> <thème>")
        print("Un article classé à la main n'est plus jamais modifié, ni par le tri ni par l'IA.")
    else:
        print("  aucun.")


def cmd_recap(cx, args):
    """Montre — et, si le modèle est actif, écrit — le récapitulatif d'une journée.

    Sans modèle configuré la commande reste utile : elle affiche ce qui est déjà en base.
    """
    import ia
    jour = args.jour or base.jour_utc(base.maintenant_utc())
    if len(jour) != 10 or jour[4] != "-" or jour[7] != "-":
        sys.exit(f"--jour attend une date AAAA-MM-JJ, reçu « {jour} ».")
    themes = ia.rubriques(base.THEMES_DECLARES)
    if args.theme:
        if args.theme not in themes:
            sys.exit(f"Thème inconnu : {args.theme}. Actifs : {' '.join(themes)}")
        themes = {args.theme: themes[args.theme]}

    try:
        classificateur = ia.charger_classificateur()
    except ValueError as e:
        print(f"config/ia.json : {e}\n")
        classificateur = None
    if classificateur:
        bilans = ia.recaps_du_jour(cx, jour, themes, classificateur, base.maintenant_utc(),
                                   ia.reglages_recap(), args.refaire)
        cx.commit()
        ecrits = [b for b in bilans if b["etat"] == "ecrit"]
        rates = [b for b in bilans if b["etat"] == "echec"]
        print(f"{len(ecrits)} récapitulatifs écrits, "
              f"{len([b for b in bilans if b['etat'] == 'inchange'])} inchangés, "
              f"{len([b for b in bilans if b['etat'] == 'vide'])} sans article, "
              f"{len(rates)} en échec.\n")
        if rates and classificateur.derniere_erreur:
            print(f"Dernière erreur : {classificateur.derniere_erreur}\n")
    else:
        print("Modèle de langage inactif : affichage de ce qui est déjà en base.")
        print("Pour en écrire de nouveaux, voir config/ia.json.\n")

    lignes = base.lister_recaps(cx, jour, list(themes))
    if not lignes:
        print(f"Aucun récapitulatif pour le {jour}.")
        return
    print(f"Récapitulatif du {jour}\n" + "=" * (len(jour) + 18))
    for r in lignes:
        print(f"\n## {base.THEMES.get(r['theme'], r['theme'])}  "
              f"({r['nb_articles']} articles, {r['modele']}, "
              f"maj {r['maj'][:16].replace('T', ' ')} UTC)")
        for paragraphe in r["texte"].split("\n"):
            print("  " + paragraphe.strip() if paragraphe.strip() else "")
    print("\nTexte écrit par un modèle de langage à partir des seuls titres du jour.")


def cmd_chercher(cx, args):
    motif = f"%{base_norm(args.texte)}%"
    lignes = cx.execute("SELECT * FROM articles WHERE titre_norm LIKE ? ORDER BY date_pub DESC LIMIT ?",
                        (motif, args.limite)).fetchall()
    _afficher(lignes) if lignes else print("Aucun article trouvé.")


def cmd_classer(cx, args):
    try:
        ok = base.classer_a_la_main(cx, args.id, args.zone, args.theme, base.maintenant_utc())
    except ValueError as e:
        sys.exit(str(e) + f"\nZones : {' '.join(base.ZONES)}\nThèmes : {' '.join(base.THEMES)}")
    if not ok:
        sys.exit(f"Aucun article n° {args.id}.")
    print(f"Article {args.id} classé en {base.ZONES[args.zone]} / {base.THEMES[args.theme]}. "
          "Le site l'affiche immédiatement.")


def base_norm(texte):
    from collect import normaliser
    return normaliser(texte).strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Administration de la base MAP")
    ap.add_argument("--base", default=str(base.BASE))
    sous = ap.add_subparsers(dest="commande", required=True)
    sous.add_parser("stats", help="chiffres clés et dernières collectes")
    sous.add_parser("config", help="thèmes et sources tels que la configuration les a posés")
    p = sous.add_parser("verifier-flux",
                        help="interroge chaque source et dit laquelle répond (rien n'est enregistré)")
    p.add_argument("--toutes", action="store_true", help="y compris les sources désactivées")
    p.add_argument("--nom", help="ne vérifier que les sources dont le nom contient ce texte")
    p.add_argument("--decouvrir", action="store_true",
                   help="pour les sources qui échouent, demander au site où est son flux")
    p.add_argument("--ecrire", action="store_true",
                   help="reporter les adresses trouvées et les vérifications dans config/flux.json")
    p = sous.add_parser("ia", help="état du classement par modèle de langage")
    p.add_argument("--limite", type=int, default=15)
    p = sous.add_parser("recap", help="récapitulatif quotidien par thème")
    p.add_argument("--jour", help="journée à résumer, AAAA-MM-JJ (défaut : aujourd'hui, UTC)")
    p.add_argument("--theme", help="un seul thème")
    p.add_argument("--refaire", action="store_true",
                   help="réécrire même si un récapitulatif existe déjà")
    p = sous.add_parser("a-trier", help="articles non classés")
    p.add_argument("--limite", type=int, default=50)
    p = sous.add_parser("chercher", help="recherche dans les titres")
    p.add_argument("texte")
    p.add_argument("--limite", type=int, default=30)
    p = sous.add_parser("classer", help="classer un article à la main")
    p.add_argument("id", type=int)
    p.add_argument("zone", choices=list(base.ZONES))
    p.add_argument("theme", choices=list(base.THEMES))
    args = ap.parse_args(argv)

    cx = base.ouvrir(args.base)
    {"stats": cmd_stats, "config": cmd_config, "verifier-flux": cmd_verifier_flux,
     "ia": cmd_ia, "recap": cmd_recap, "a-trier": cmd_a_trier, "chercher": cmd_chercher,
     "classer": cmd_classer}[args.commande](cx, args)
    cx.close()


if __name__ == "__main__":
    main()
