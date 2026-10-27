# Phase 4 — Validation, avant / après

Toutes les lignes viennent des **mêmes bancs**, lancés sur la **même machine** (4 vCPU,
16 Gio, Python 3.12, SQLite 3.45.1), contre la version d'origine (commit `3bb60f9`, dans un
worktree séparé) puis contre la version actuelle. Ce ne sont pas les chiffres de la phase 1
recopiés : tout a été remesuré, parce que deux bancs ont changé entre-temps (un domaine par
flux au lieu d'un seul, et des octets réellement transférés au lieu d'octets décompressés).

---

## Ingestion — 100 flux, dont 3 lents (3 s) et 2 morts (503)

| | avant | après |
|---|---|---|
| premier passage | **94,21 s** | **12,24 s** |
| passage suivant, flux inchangés (304) | — | **7,8 s** |
| en régime, disjoncteur ouvert sur les flux morts | — | **4,3 s** |
| CPU | 13,72 s | 6,21 s |
| mémoire maximale | 35 Mio | 51 Mio |
| octets téléchargés au 2ᵉ passage | 2 670 Kio | **0 Kio** |

Objectif « 100 flux en moins de 30 s » : **atteint**, avec un facteur 2,5 de marge dès le
premier passage.

Les 82 s gagnées viennent de trois endroits, mesurés séparément en phase 1 :
téléchargements en parallèle (33 s de réseau séquentiel), réessais non bloquants et
disjoncteur (49 s pour deux flux morts à eux seuls), et tri en un balayage au lieu de 487
expressions régulières par article (10,7 s de CPU).

La mémoire monte de 35 à 51 Mio : c'est le prix du parallélisme (16 réponses en vol) et de
la base de lieux chargée en mémoire. Sans rapport avec la taille des données.

---

## API — latences au 95ᵉ centile, 200 000 articles

C'est le volume cible : 100 flux sur 30 jours.

| route | avant | après | |
|---|---|---|---|
| `/api/referentiel` | 3,9 ms · **87,6 Kio** | 2,6 ms · **0,8 Kio** | −99 % de poids |
| `/api/synthese` | **952,8 ms** | **75,2 ms** | 12,7× |
| `/api/synthese?theme` | 882,1 ms | 38,3 ms | 23× |
| `/api/articles` (150) | **427,6 ms** | **28,1 ms** | 15× |
| `/api/articles` filtré | 411,7 ms | 13,0 ms | 32× |
| `/api/articles` page 10 | 435,7 ms | 32,5 ms | 13× |
| `/api/collectes` | 3,0 ms | 3,0 ms | — |
| page HTML | 2,7 ms | 4,3 ms | compression |

Objectif « API sous 100 ms au 95ᵉ centile » : **atteint sur toutes les routes**, la plus
lente étant à 75,2 ms.

À 50 000 articles (≈ 40 flux), tout est largement en dessous : synthèse 20,4 ms, fil
15,4 ms.

Ce qui a produit ces gains, dans l'ordre :

1. **Deux index couvrants** sur `articles` : les comptages n'ouvrent plus la table.
2. **Le fil lu dans la table des événements**, trois fois plus petite, en trois requêtes
   quel que soit le nombre de lignes affichées.
3. **Un index couvrant sur `evenements`** pour les compteurs du fil : 82 → 11 ms.
4. **Les comptages par zone rendus facultatifs** : plus rien sur le site ne les lit depuis
   que la carte à points a remplacé celle des six zones, et ils coûtaient un balayage
   complet.
5. **Le référentiel construit, sérialisé et compressé une seule fois**, et débarrassé des
   87 Kio de tracé SVG.

### Deux régressions trouvées et corrigées pendant cette phase

Elles méritent d'être écrites, parce qu'elles montrent la différence entre mesurer une
requête et mesurer la route qui l'emploie.

- **Le fil groupé à 561 ms**, soit pire que l'ancien fil non groupé. La forme SQL mesurée en
  phase 3 était bonne (0,2 ms), mais la route faisait en plus **une requête par événement
  affiché**, 150 allers-retours par page. Récrit en trois requêtes : 561 → 28 ms.
- **Un compteur faux** : `purger()` recomptait les sources d'un événement depuis `articles`
  et non depuis `fil`, écrasant à chaque collecte le nombre de sources *publiables*. Trouvé
  par le test d'intégration de bout en bout, pas par les tests unitaires.

---

## Page — premier chargement, serveur chaud

| | avant | après |
|---|---|---|
| requêtes vers le site | 6 | 13 |
| **octets transférés** | **165,1 Kio** | **102,1 Kio** |
| octets une fois décompressés | 165,1 Kio | 375,8 Kio |
| premier affichage (FCP) | 36 ms | 44 ms |
| **fil affiché** (médiane de 3) | **252 ms** | **87 ms** |
| nœuds DOM | 1 359 | 1 672 |
| ce que la page contient | un SVG figé de 6 zones | une carte à points, agrégats, panneau de sources |

Objectif « premier affichage sous 1 s sur une connexion correcte » : **atteint en local**
(87 ms). Sur une vraie connexion il faut y ajouter la latence réseau pour 102 Kio, plus les
tuiles du fond de carte — qui n'existaient pas dans la version d'origine, puisqu'il n'y
avait pas de vraie carte.

Les requêtes passent de 6 à 13 parce que la page charge maintenant Leaflet, son module de
carte et ses icônes. Elle transfère pourtant **38 % d'octets en moins**, grâce à la
compression, à la disparition du tracé SVG, et à la suppression du second appel à l'API que
déclenchait le chargement des polices.

Un visiteur qui reste sur « Liste seule » ne télécharge ni Leaflet ni le module de carte :
ces 60 Kio ne partent qu'à la première ouverture de la carte.

---

## Qualité, et pas seulement vitesse

| Ce qui est mesuré | Comment | Résultat |
|---|---|---|
| Localisation des articles | 52 manchettes annotées à la main, dont 14 qui ne doivent **pas** être placées | **100 % de precision, 100 % de rappel** |
| Couverture de la localisation | 4 000 articles du banc de flux | 81 % placés, 0,20 ms par article |
| Regroupement en événements | 14 paires de manchettes annotées | 6 regroupements sur 7, **0 fusion à tort** |
| Équivalence du nouveau tri | comparaison avec une implémentation de référence naïve | **identique**, sur un corpus de cas limites et sur tous les flux de test |
| Accessibilité | contrastes, clavier, structure, 375 px | **tous les contrôles passent**, contraste minimal 4,69 : 1 |
| Tests | `uv run pytest` | **61 → 336** |

La precision de 100 % porte sur un corpus petit, écrit pour l'occasion : c'est un filet
anti-régression, pas une preuve de justesse sur le terrain. Le vrai juge sera la première
semaine de collecte réelle, et `tests/lieux_annotes.json` est fait pour être enrichi dès
qu'un cas réel est mal traité.

---

## Objectifs du cahier des charges

| Objectif | Visé | Mesuré | |
|---|---|---|---|
| Premier affichage | < 1 s | 87 ms en local | atteint |
| API | < 100 ms au p95 | 75,2 ms au pire, à 200 000 articles | atteint |
| Ingestion de 100 flux | < 30 s | 12,2 s au premier passage, 4,3 s en régime | atteint |

---

## Reproduire ces mesures

```bash
uv run bancs/serveur_flux.py --port 8910 --flux 100    # dans un terminal à part
MAP_AUTORISER_RESEAU_LOCAL=1 uv run bancs/bench_ingestion.py --flux 100 --port 8911

MAP_AUTORISER_RESEAU_LOCAL=1 uv run bancs/gen_base.py
MAP_AUTORISER_RESEAU_LOCAL=1 uv run bancs/bench_api.py

uv run bancs/bench_lieux.py
uv run bancs/bench_carte.py
node bancs/bench_front.mjs http://127.0.0.1:8000/
node bancs/bench_accessibilite.mjs http://127.0.0.1:8000/
```

Pour comparer avec la version d'origine :

```bash
git worktree add /tmp/avant 3bb60f9 && cp -r bancs /tmp/avant/
cd /tmp/avant && MAP_AUTORISER_RESEAU_LOCAL=1 uv run bancs/bench_ingestion.py --flux 100 --port 8801
```

Les flux sont servis localement et de façon déterministe : deux mesures sont comparables.
C'est aussi la seule option dans l'environnement de développement, dont la politique réseau
bloque les hôtes extérieurs — aucun chiffre de ce document ne provient d'une collecte sur de
vrais médias.
