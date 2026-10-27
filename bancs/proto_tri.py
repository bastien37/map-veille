#!/usr/bin/env python3
"""Prototype : tri par mots-cles en un seul balayage (automate de tokens)
au lieu de 487 regex executees article par article.
Verifie l'equivalence stricte avec collect.classer, puis mesure le gain.
"""
import re
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(Path(__file__).parent))
import collect  # noqa: E402

JETON = re.compile(r"[a-z0-9]+")


def compiler_rapide(regles: dict):
    """exact[mot] / prefixe[racine] / expr[n] -> liste de categories (avec repetition
    si le meme mot-cle apparait plusieurs fois, pour conserver le meme comptage)."""
    exact, prefixe, expr, nmax = {}, {}, {}, 1
    for cat, mots in regles.items():
        for mot in mots:
            m = collect.normaliser(mot).strip()
            if not m:
                continue
            if " " in m:
                n = m.count(" ") + 1
                nmax = max(nmax, n)
                expr.setdefault((n, m), []).append(cat)
            elif mot.endswith("*"):
                prefixe.setdefault(m, []).append(cat)
            else:
                exact.setdefault(m, []).append(cat)
    lmin = min((len(p) for p in prefixe), default=99)
    lmax = max((len(p) for p in prefixe), default=0)
    return {"exact": exact, "prefixe": prefixe, "expr": expr, "nmax": nmax,
            "lmin": lmin, "lmax": lmax}


def _balayer(texte, a, poids, scores, premiere, noter_position):
    jetons = [(m.group(), m.start()) for m in JETON.finditer(texte)]
    exact, prefixe, expr = a["exact"], a["prefixe"], a["expr"]
    lmin, lmax, nmax = a["lmin"], a["lmax"], a["nmax"]
    for i, (j, pos) in enumerate(jetons):
        pos += 1   # normaliser() prefixe le texte d'un espace -> meme offset que les regex
        for cat in exact.get(j, ()):
            scores[cat] = scores.get(cat, 0) + poids
            if noter_position and pos < premiere.get(cat, 10**9):
                premiere[cat] = pos
        for k in range(lmin, min(lmax, len(j)) + 1):   # mots-cles « racine* »
            for cat in prefixe.get(j[:k], ()):
                scores[cat] = scores.get(cat, 0) + poids
                if noter_position and pos < premiere.get(cat, 10**9):
                    premiere[cat] = pos
        if expr:
            for n in range(2, nmax + 1):
                if i + n > len(jetons):
                    break
                suite = " ".join(x[0] for x in jetons[i:i + n])
                for cat in expr.get((n, suite), ()):
                    scores[cat] = scores.get(cat, 0) + poids
                    if noter_position and pos < premiere.get(cat, 10**9):
                        premiere[cat] = pos


def classer_rapide(titre, resume, a):
    scores, premiere = {}, {}
    _balayer(collect.normaliser(titre), a, collect.POIDS_TITRE, scores, premiere, True)
    _balayer(collect.normaliser(resume), a, collect.POIDS_RESUME, scores, premiere, False)
    if not scores:
        return None
    return sorted(scores, key=lambda c: (-scores[c], premiere.get(c, 10**6)))[0]


if __name__ == "__main__":
    import json
    regles = json.loads((RACINE / "collecteur" / "regles.json").read_text(encoding="utf-8"))
    ancien = {f: collect.compiler(regles[f]) for f in ("zones", "themes")}
    nouveau = {f: compiler_rapide(regles[f]) for f in ("zones", "themes")}

    # jeu d'essai : les articles du serveur de flux local + quelques cas limites
    import serveur_flux
    textes = []
    for i in range(60):
        for a in collect.lire_rss(serveur_flux.flux(i), "m"):
            textes.append((a["titre"], a["resume"]))
    textes += [("Union europeenne : Bruxelles et le parlement europeen", "europeennes"),
               ("", ""), ("UE", "ue ue ue"), ("Rien du tout ici", "voiture velo"),
               ("Guerre en Ukraine, missiles et drones", "armee russe frappe Kharkiv")]
    print(f"{len(textes)} textes d'essai")

    ecarts = 0
    for t, r in textes:
        for f in ("zones", "themes"):
            if collect.classer(t, r, ancien[f]) != classer_rapide(t, r, nouveau[f]):
                ecarts += 1
                if ecarts < 4:
                    print("  ECART:", f, repr(t[:70]),
                          collect.classer(t, r, ancien[f]), "!=", classer_rapide(t, r, nouveau[f]))
    print("equivalence stricte :", "OK" if ecarts == 0 else f"{ecarts} ecarts")

    for nom, fn, tab in (("actuel (487 regex/article)", collect.classer, ancien),
                         ("prototype (1 balayage)", classer_rapide, nouveau)):
        t0 = time.perf_counter()
        for t, r in textes:
            for f in ("zones", "themes"):
                fn(t, r, tab[f])
        d = time.perf_counter() - t0
        print(f"  {nom:<30} {d:6.2f} s  ({1000*d/len(textes):.2f} ms/article)")
