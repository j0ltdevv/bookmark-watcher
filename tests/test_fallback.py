#!/usr/bin/env python3
"""Vérifie le repli du canal de notification SANS réseau : send_telegram() doit
emprunter le bot direct quand `hermes send` échoue (mode channel=auto).

Aucun message réel n'est envoyé — requests.post et send_hermes sont stubés.
Usage : ./venv/bin/python tests/test_fallback.py"""
import importlib.util
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("watcher", BASE / "watcher.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

calls = {"hermes": 0, "direct": 0, "record": 0}

def fake_send_hermes(text, *, subject=None):
    calls["hermes"] += 1
    print("    [stub] send_hermes() -> False (Hermes cassé)")
    return False

def fake_post(url, **kw):
    calls["direct"] += 1
    print(f"    [stub] POST direct -> {url.split('/bot')[0]}/bot…/sendMessage")
    return types.SimpleNamespace(ok=True, status_code=200,
                                 json=lambda: {"result": {"message_id": 999}})

w.send_hermes = fake_send_hermes
w.requests.post = fake_post
w.record_brief = lambda *a, **k: calls.__setitem__("record", calls["record"] + 1)

tweet = {"tweet_id": "1", "author_handle": "x", "text": "texte", "created_at": "", "links": []}
brief = {"verdict": "SOLIDE", "resume": ["un point"], "verification": "ok"}

# ce test porte sur les canaux QUI ENVOIENT : on force 'auto' quel que soit le
# mode du .env (par defaut 'local', qui n'envoie rien — cf. test_local_channel.py).
w.TELEGRAM_CHANNEL = "auto"
print("=== channel=auto, Hermes cassé -> repli direct ===")
rc = w.send_telegram(tweet, brief, [], label="TEST repli")
print(f"    rc={rc}  (999 = message_id du bot direct)")

assert calls["hermes"] == 1, "send_hermes n'a pas été tenté"
assert calls["direct"] == 1, "le repli direct n'a pas eu lieu"
assert calls["record"] == 1, "record_brief doit tourner même en repli (le contexte reste alimenté)"
print("    OK : hermes tenté une fois, direct utilisé, contexte alimenté")

print("\n=== channel=hermes, Hermes cassé -> AUCUN repli (mode strict) ===")
calls.update(hermes=0, direct=0, record=0)
w.TELEGRAM_CHANNEL = "hermes"
rc = w.send_telegram(tweet, brief, [], label="TEST strict")
print(f"    rc={rc}")
assert calls["direct"] == 0, "le mode strict ne doit PAS retomber sur le bot direct"
print("    OK : pas de repli, comme voulu")

print("\nOK")
