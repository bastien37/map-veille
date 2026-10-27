#!/usr/bin/env python3
"""Serveur de flux RSS local, reproductible, pour mesurer l'ingestion.

Simule N flux de medias : taille et latence realistes, quelques flux lents,
quelques flux morts. Deterministe (pas de random non seme) => comparable
avant/apres.

  python3 serveur_flux.py --port 8899 --flux 100
"""
import argparse
import hashlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOTS = ("Ukraine Kharkiv frappes drones", "Union europeenne Bruxelles budget defense",
        "seisme Japon tsunami alerte", "Bresil Lula deforestation secheresse",
        "Chine Taiwan manoeuvres navales", "Sahel Mali Burkina operation",
        "petrole OPEP baril prix energie", "fusee lancement satellite orbite",
        "inondations Pakistan mousson", "Etats-Unis Washington Congres vote",
        "Israel Gaza cessez-le-feu negociations", "Inde New Delhi sommet",
        "Russie Moscou sanctions economiques", "Nigeria Lagos croissance",
        "Allemagne Berlin industrie automobile", "cyberattaque hopital donnees")

LAT_BASE_MS = 80          # latence minimale
LAT_SPREAD_MS = 520       # etalement
FLUX_LENTS = {7, 23, 61}  # 3 flux a 3 s
FLUX_MORTS = {13, 47}     # 2 flux qui repondent 503


def latence_ms(i: int) -> int:
    if i in FLUX_LENTS:
        return 3000
    return LAT_BASE_MS + (i * 37) % LAT_SPREAD_MS


def corps_flux(i: int, n_items: int = 40) -> bytes:
    """Flux RSS 2.0 deterministe, ~40 items, taille comparable a un vrai flux media."""
    t0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0"><channel>',
           f'<title>Media {i}</title><link>http://127.0.0.1/feed/{i}</link>',
           '<description>Flux de test</description>']
    for k in range(n_items):
        d = t0 - timedelta(hours=k * 6 + (i % 5))
        sujet = MOTS[(i + k) % len(MOTS)]
        # un item sur 12 est un doublon inter-flux (meme titre, lien different)
        partage = (k % 12 == 0)
        titre = (f"{sujet} : le point sur la situation"
                 if partage else f"{sujet} : depeche {i}-{k} du media {i}")
        out.append(
            f"<item><title>{titre}</title>"
            f"<link>http://127.0.0.1/feed/{i}/article/{k}</link>"
            f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>&lt;p&gt;{sujet}. "
            + ("Analyse et contexte de la situation sur le terrain. " * 8)
            + "&lt;/p&gt;</description>"
            f"<category>international</category></item>")
    out.append('</channel></rss>')
    return "\n".join(out).encode("utf-8")


CACHE = {}


def flux(i: int) -> bytes:
    if i not in CACHE:
        CACHE[i] = corps_flux(i)
    return CACHE[i]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    compteur = 0
    verrou = threading.Lock()

    def log_message(self, *a):
        pass

    def do_GET(self):
        chemin = self.path.split("?")[0]
        if chemin == "/__stats":
            corps = str(Handler.compteur).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return
        if not chemin.startswith("/feed/"):
            self.send_error(404)
            return
        try:
            i = int(chemin.split("/")[2])
        except (IndexError, ValueError):
            self.send_error(404)
            return
        with Handler.verrou:
            Handler.compteur += 1
        time.sleep(latence_ms(i) / 1000)
        if i in FLUX_MORTS:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        corps = flux(i)
        etag = '"' + hashlib.md5(corps).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", "Mon, 27 Oct 2026 12:00:00 GMT")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)


def demarrer(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--flux", type=int, default=100)
    a = ap.parse_args()
    srv = demarrer(a.port)
    tot = sum(len(flux(i)) for i in range(a.flux))
    print(f"{a.flux} flux sur http://127.0.0.1:{a.port}/feed/N  "
          f"({tot/1024:.0f} KiB au total, {tot/a.flux/1024:.1f} KiB par flux)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)
