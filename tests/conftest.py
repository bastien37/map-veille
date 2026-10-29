"""Réglages communs aux tests.

Les tests réseau montent un serveur HTTP sur 127.0.0.1. Or le collecteur refuse par
défaut les adresses du réseau interne (protection SSRF) : on lève cette protection pour
la durée des tests, comme on le ferait en développement local avec
MAP_AUTORISER_RESEAU_LOCAL=1.
"""
import os
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
os.environ.setdefault("MAP_AUTORISER_RESEAU_LOCAL", "1")

import collect  # noqa: E402

collect.AUTORISER_RESEAU_LOCAL = True
