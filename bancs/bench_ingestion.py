#!/usr/bin/env python3
"""Baseline ingestion : N flux RSS servis en local, collecteur actuel inchange.

  python3 bench_ingestion.py --flux 100 [--profil] [--sains]
"""
import argparse
import os
import resource
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(Path(__file__).parent))
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ.pop("http_proxy", None)
os.environ.pop("HTTP_PROXY", None)

import serveur_flux  # noqa: E402
import base  # noqa: E402
import collect  # noqa: E402


def hote(i: int, un_seul: bool) -> str:
    """Un domaine par flux (127.0.0.1, 127.0.0.2, …), comme 100 medias differents.
    Tout 127.0.0.0/8 est du loopback : un seul serveur repond pour toutes ces adresses."""
    return "127.0.0.1" if un_seul else f"127.0.0.{1 + i % 250}"


def preparer(chemin: Path, n: int, port: int, sains: bool, un_seul_hote: bool = False):
    if chemin.exists():
        chemin.unlink()
    cx = base.ouvrir(chemin)
    cx.execute("DELETE FROM sources")
    for i in range(n):
        if sains and (i in serveur_flux.FLUX_LENTS or i in serveur_flux.FLUX_MORTS):
            continue
        base._inserer_source(cx, {"nom": f"Media {i}", "type": "rss",
                                  "url": f"http://{hote(i, un_seul_hote)}:{port}/feed/{i}",
                                  "actif": True})
    cx.commit()
    cx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flux", type=int, default=100)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--profil", action="store_true")
    ap.add_argument("--sains", action="store_true", help="exclut les flux lents et morts")
    ap.add_argument("--repeter", type=int, default=1)
    ap.add_argument("--un-seul-hote", action="store_true",
                    help="tous les flux sur le meme domaine (montre l'effet du verrou par domaine)")
    a = ap.parse_args()

    serveur_flux.demarrer(a.port)
    time.sleep(0.3)
    chemin = Path(__file__).parent / "bench.sqlite"

    octets = sum(len(serveur_flux.flux(i)) for i in range(a.flux))
    print(f"--- Ingestion : {a.flux} flux, {octets/1024:.0f} KiB a telecharger "
          f"({'sains uniquement' if a.sains else '3 lents (3 s) + 2 morts (503)'}, "
          f"{'un seul domaine' if a.un_seul_hote else 'un domaine par flux'}) ---")

    for essai in range(a.repeter):
        preparer(chemin, a.flux, a.port, a.sains, a.un_seul_hote)
        r0 = resource.getrusage(resource.RUSAGE_SELF)
        t0, c0 = time.perf_counter(), time.process_time()
        if a.profil:
            import cProfile
            import pstats
            pr = cProfile.Profile()
            pr.enable()
        sortie = []
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            collect.main(["--base", str(chemin), "--migrer", "/nexistepas.json"])
        if a.profil:
            pr.disable()
        mur, cpu = time.perf_counter() - t0, time.process_time() - c0
        r1 = resource.getrusage(resource.RUSAGE_SELF)
        lignes = [l for l in buf.getvalue().splitlines() if not l.startswith("[ok]")]
        print(f"  essai {essai+1} : mur {mur:6.2f} s | CPU {cpu:5.2f} s "
              f"({100*cpu/mur:4.1f} %) | maxRSS {r1.ru_maxrss/1024:.0f} Mio "
              f"(+{(r1.ru_maxrss-r0.ru_maxrss)/1024:.0f}) | "
              f"IO attendue {mur-cpu:5.2f} s")
        for l in lignes:
            print("        " + l)
        if a.profil:
            st = pstats.Stats(pr)
            st.sort_stats("cumulative")
            print("  --- profil (cumulatif, top 18) ---")
            import re
            b = io.StringIO()
            st.stream = b
            st.print_stats(18)
            for l in b.getvalue().splitlines():
                if re.match(r"\s+\d", l) or "ncalls" in l:
                    print("   " + l[:150])


if __name__ == "__main__":
    main()
