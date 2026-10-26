#!/usr/bin/env python3
"""
MAP – Outil d'administration de la base SQLite.

    uv run collecteur/admin.py stats                     # chiffres clés et dernières collectes
    uv run collecteur/admin.py a-trier                   # articles que le tri n'a pas su classer
    uv run collecteur/admin.py classer 42 asi def        # classe l'article 42 à la main
    uv run collecteur/admin.py chercher "Taïwan"         # recherche dans les titres

Zones  : eur rus mena afr asi ame
Thèmes : def dip eco tec cli
"""
import argparse
import sys

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
              f"{c['nb_lus']} lus, {c['nb_nouveaux']} nouveaux, {c['nb_doublons']} doublons")
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
    {"stats": cmd_stats, "a-trier": cmd_a_trier, "chercher": cmd_chercher,
     "classer": cmd_classer}[args.commande](cx, args)
    cx.close()


if __name__ == "__main__":
    main()
