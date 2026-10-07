#!/usr/bin/env python3
"""Test du canal de notification : vérifie que send_telegram() livre réellement
un brief via le bot Hermes, et que le repli direct fonctionne en cas d'échec.

Usage (venv du watcher) : ./venv/bin/python tests/test_notify_channel.py
Sans arguments, n'envoie QUE le test Hermes (dry-run respecté via SEND=0)."""
import importlib.util
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("watcher", BASE / "watcher.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

SEND = os.environ.get("SEND", "1") == "1"

# un vrai brief, celui du dernier bookmark traité (bot-context.json)
records = json.loads((BASE / "bot-context.json").read_text())
rec = records[-1]
tweet = {"tweet_id": rec["tweet_id"], "author_handle": rec["author"],
         "text": rec["text"], "created_at": rec.get("created_at", ""), "links": []}
brief = rec["brief"]

print(f"TELEGRAM_CHANNEL = {w.TELEGRAM_CHANNEL}")
print(f"author           = @{rec['author']}  verdict={brief.get('verdict')}")

# --- 1. conversion HTML -> Markdown ---------------------------------------
html_sample = ('<b>GAME</b> <a href="https://x.com/a/status/1">— @a</a>\n'
               '<i>« quote »</i>\n\n<b>Limites</b>\n• x & y <z>')
md = w._html_to_markdown(html_sample)
print("\n[1] _html_to_markdown")
print(md)
assert "<b>" not in md and "<i>" not in md, "des balises HTML ont survécu"
assert "&amp;" not in md, "les entités HTML ont survécu"

# --- 2. canal mort -------------------------------------------------------
assert w.push_to_openmausbot(tweet, brief, "test") is False, "push OMB doit être inerte"
print("\n[2] push_to_openmausbot -> False (inerte) OK")

# --- 3. envoi réel -------------------------------------------------------
if not SEND:
    print("\n[3] SEND=0 : envoi reel ignore")
    raise SystemExit(0)

print("\n[3] envoi reel via send_telegram() ...")
w.record_brief = lambda *a, **k: print("    record_brief() appele (contexte conversationnel alimente)")
rc = w.send_telegram(tweet, brief, [], label="TEST migration")
print(f"    rc = {rc}")
assert rc is not None, "l'envoi a echoue — le repli direct aurait du prendre le relais"

print("\nOK")
