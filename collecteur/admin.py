#!/usr/bin/env python3
"""
MAP – Outil d'administration de la base SQLite.

    uv run collecteur/admin.py stats                     # chiffres clés et dernières collectes
    uv run collecteur/admin.py config                    # thèmes et sources issus de config/
    uv run collecteur/admin.py verifier-flux             # quelles sources répondent vraiment
    uv run collecteur/admin.py a-trier                   # articles que le tri n'a pas su classer
    uv run collecteur/admin.py classer 42 asi def        # classe l'article 42 à la main
    uv run collecteur/admin.py chercher "Taïwan"         # recherche dans les titres

Zones  : eur rus mena afr asi ame
Thèmes : voir config/themes.json (« admin.py config » les affiche)
"""
import argparse
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
    for code, nom in base.ZONES.items():
        n = dict(cx.execute("SELECT theme, COUNT(*) FROM fil WHERE zone = ? GROUP BY theme", (code,)).fetchall())
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


def cmd_verifier_flux(cx, args):
    """Interroge chaque source sans rien enregistrer, et dit laquelle répond.

    À lancer après avoir ajouté des flux dans config/flux.json : une adresse se devine mal,
    et une source muette ne se remarque autrement qu'au bout de quelques collectes.
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

    bons, muets, casses = [], [], []
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
                print(f"  {'ok':6}{s['nom'][:33]:34}{s['langue']:4}{len(utiles):>9}  "
                      f"{duree:.1f} s, plus récent le {recent}")

    print(f"\n{len(bons)} sources répondent, {len(muets)} vides, {len(casses)} en erreur.")
    if casses or muets:
        print("\nPour chacune : corriger l'adresse dans config/flux.json, ou la désactiver")
        print("depuis l'administration (onglet Sources), ou la retirer du fichier — une")
        print("source retirée est désactivée, ses articles déjà collectés restent.")
        for s, _ in casses:
            print(f"  à revoir : {s['nom']} — {s['url']}")
        for s in muets:
            print(f"  à revoir : {s['nom']} — {s['url']}")


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
     "a-trier": cmd_a_trier, "chercher": cmd_chercher,
     "classer": cmd_classer}[args.commande](cx, args)
    cx.close()


if __name__ == "__main__":
    main()
