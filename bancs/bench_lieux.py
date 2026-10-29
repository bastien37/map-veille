#!/usr/bin/env python3
"""Mesure de l'extraction de lieux sur le corpus annoté tests/lieux_annotes.json.

Precision : parmi les articles placés, combien le sont au bon endroit.
Rappel     : parmi les articles qui DEVAIENT être placés, combien l'ont été.
La precision prime : un point faux raconte une histoire fausse, un point manquant non.
"""
import json
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import lieux  # noqa: E402

cas = json.loads((RACINE / "tests" / "lieux_annotes.json").read_text(encoding="utf-8"))["cas"]
t0 = time.perf_counter()
gaz = lieux.charger_gazetteer()
t_charge = time.perf_counter() - t0

t0 = time.perf_counter()
resultats = [(c, lieux.extraire(c["titre"], c["resume"], gaz)) for c in cas]
t_extrait = time.perf_counter() - t0

vp = sum(1 for c, r in resultats if c["attendu"] and r and r["nom"] == c["attendu"])
fp = sum(1 for c, r in resultats if r and (not c["attendu"] or r["nom"] != c["attendu"]))
fn = sum(1 for c, r in resultats if c["attendu"] and not r)
vn = sum(1 for c, r in resultats if not c["attendu"] and not r)

print(f"gazetteer : {len(gaz['index'])} noms, chargé en {t_charge * 1000:.0f} ms")
print(f"extraction : {len(cas)} articles en {t_extrait * 1000:.1f} ms "
      f"({t_extrait / len(cas) * 1000:.2f} ms par article)\n")
print(f"  bien placés            {vp:>3}")
print(f"  mal placés             {fp:>3}   <- le chiffre qui compte")
print(f"  non placés à tort      {fn:>3}")
print(f"  non placés à raison    {vn:>3}")
print(f"\n  precision  {100 * vp / max(vp + fp, 1):5.1f} %")
print(f"  rappel     {100 * vp / max(vp + fn, 1):5.1f} %")

if fp or fn:
    print("\n  erreurs :")
    for c, r in resultats:
        obtenu = r["nom"] if r else None
        if obtenu != c["attendu"]:
            print(f"    {c['titre'][:58]:60} attendu {str(c['attendu']):16} obtenu {obtenu}")
