#!/usr/bin/env python3
"""Test du mode `local` : le watcher ne doit RIEN envoyer sur Telegram, mais
doit bien alimenter bot-context.json (c'est ce fichier que le profil Hermes
`bookmarks` lit pour deposer un commentaire dans le Bot Chat de l'app).

Utilise le venv du watcher (watcher.py importe camoufox au chargement).
Usage : ./venv/bin/python tests/test_local_channel.py
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("watcher", BASE / "watcher.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

assert w.TELEGRAM_CHANNEL == "local", f"attendu 'local', lu {w.TELEGRAM_CHANNEL!r}"
print("TELEGRAM_CHANNEL =", w.TELEGRAM_CHANNEL)

# toute tentative de sortir sur Telegram doit exploser bruyamment si elle a lieu
calls = {"post": 0, "hermes": 0, "record": 0}


def boom_post(*a, **k):
    calls["post"] += 1
    raise AssertionError("le watcher a tente un POST Telegram en mode local")


def boom_hermes(*a, **k):
    calls["hermes"] += 1
    raise AssertionError("le watcher a tente hermes send en mode local")


w.requests.post = boom_post
w.send_hermes = boom_hermes
w.record_brief = lambda *a, **k: calls.__setitem__("record", calls["record"] + 1)

tweet = {"tweet_id": "7777777777777777777", "author_handle": "x", "text": "t",
         "created_at": "", "links": []}
brief = {"verdict": "SOLIDE", "resume": ["un point"], "verification": "ok"}

print("\n=== send_telegram() en mode local ===")
rc = w.send_telegram(tweet, brief, [], label="TEST local")
print(f"    rc={rc}  (0 = brief memorise, aucune notif envoyee)")
assert rc == 0, "send_telegram doit retourner 0 en mode local"
assert calls["post"] == 0, "aucun POST Telegram ne doit partir"
assert calls["hermes"] == 0, "hermes send ne doit pas etre appele"
assert calls["record"] == 1, "record_brief DOIT tourner (c'est la source du profil bookmarks)"
print("    OK : aucune sortie Telegram, bot-context.json alimente")

print("\n=== tg_send() en mode local (sante / digest) ===")
calls["post"] = calls["hermes"] = 0
w.tg_send("**sante** test — doit rester dans le journal")
assert calls["post"] == 0 and calls["hermes"] == 0, "tg_send ne doit rien envoyer en local"
print("    OK : ecrit dans le journal uniquement")

print("\n=== notify() en mode local ===")
assert w.notify("markdown", subject="x", html_fallback="<b>x</b>") is True
print("    OK : notify() renvoie True sans rien envoyer")

print("\nOK")
