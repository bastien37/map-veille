# Bancs de mesure

Scripts de mesure **reproductibles** utilisés pour l'audit (phase 1) et pour la comparaison
avant / après (phase 4). Ils ne font partie ni du collecteur, ni du serveur : rien dans
`collecteur/`, `serveur/` ou `site/` ne les importe.

Les flux sont servis par un **serveur local déterministe** (`serveur_flux.py`) plutôt que par
de vrais médias : tailles et latences réalistes, mêmes octets à chaque essai, donc deux
mesures sont comparables. C'est aussi la seule option dans l'environnement d'audit, dont la
politique réseau bloque les hôtes externes.

| Script | Ce qu'il mesure |
|---|---|
| `serveur_flux.py` | 100 flux RSS locaux : 28 Kio chacun, latence 80–600 ms, 3 flux lents (3 s), 2 flux morts (503), gère `ETag` / `If-None-Match` |
| `bench_ingestion.py` | Collecte complète de bout en bout : temps mur, CPU, mémoire maximale |
| `profil_cpu.py` | Découpe l'ingestion en 4 étapes (téléchargement / parsing / tri / stockage) + profil `cProfile` |
| `gen_base.py` | Génère des bases SQLite réalistes de 5 000, 50 000 et 200 000 articles |
| `bench_api.py` | Latences p50/p95/p99 et taille des réponses de chaque route, aux trois volumes |
| `bench_sql.py` | Plans d'exécution SQLite, effet mesuré d'un index composite, taux de compression |
| `bench_front.mjs` | Chargement de la page : requêtes, octets, premier affichage (Playwright) |
| `proto_tri.py` | Prototype du tri en un seul balayage + **vérification d'équivalence stricte** avec `collect.classer` |
| `proto_py.py` | Prototype Python optimisé (téléchargement concurrent + tri rapide) |
| `go/` | Même travail en Go, pour chiffrer l'écart de langage (bibliothèque standard uniquement) |

## Lancer

```bash
uv sync --dev

# 1. serveur de flux, dans un terminal à part
uv run bancs/serveur_flux.py --port 8910 --flux 100

# 2. ingestion : référence actuelle, puis découpe par étape
uv run bancs/bench_ingestion.py --flux 100 --port 8911       # version actuelle, bout en bout
uv run bancs/profil_cpu.py 8910                              # découpe + profil CPU

# 3. API : générer les bases puis mesurer
uv run bancs/gen_base.py
uv run bancs/bench_api.py
uv run bancs/bench_sql.py

# 4. front (serveur lancé sur 8940 avec MAP_BASE=bancs/api_50k.sqlite)
node bancs/bench_front.mjs http://127.0.0.1:8940/ 0

# 5. comparaison de langage
uv run bancs/proto_tri.py                 # équivalence + vitesse du tri
uv run bancs/proto_py.py 8910 32          # Python optimisé
cd bancs/go && go run . 8910 32           # Go
```

Les bases produites (`bancs/*.sqlite`) sont ignorées par git.
