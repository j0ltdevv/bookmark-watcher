#!/usr/bin/env python3
"""
bookmark-watcher — surveille les bookmarks X/Twitter et pousse une analyse sur Telegram.

Architecture "push agressif" :
  - régime permanent : HTTP pur vers l'API GraphQL de X avec les cookies de session
    (quelques Mo de RAM, réponse < 1 s, POLL_INTERVAL par défaut 45 s)
  - le navigateur camoufox ne sert qu'à deux choses : le login initial (une fois),
    et le rafraîchissement de session quand les cookies expirent (rare, automatique)

Modes :
  login    ouvre une fenêtre camoufox pour se connecter à X une fois (session persistée)
  refresh  capture une session fraîche (cookies + URL GraphQL) via navigateur headless
  once     un seul cycle de poll (test)
  daemon   boucle infinie (service systemd)

Pipeline par nouveau bookmark :
  poll GraphQL → dédup (seen.json) → import Siftly → Exa → brief LLM → Telegram
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from camoufox.sync_api import Camoufox

BASE = Path(__file__).resolve().parent
PROFILE_DIR = str(BASE / "x-profile")
SESSION_FILE = BASE / "session.json"
SEEN_FILE = BASE / "seen.json"
SYSTEM_PROMPT_FILE = BASE / "system-prompt.md"
MEMORY_DIR = BASE / "memory"

def load_memory() -> str:
    """Concatène tous les fichiers de mémoire structurée."""
    if not MEMORY_DIR.exists():
        return ""
    parts = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        parts.append(f"<memoire fichier=\"{f.name}\">\n{f.read_text().strip()}\n</memoire>")
    return "\n\n".join(parts)

# Bearer public embarqué dans le client web de X (constante, pas un secret utilisateur)
X_WEB_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4"
)

# ---------------------------------------------------------------- config

def load_env() -> dict:
    env = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

CFG = load_env()
TELEGRAM_BOT_TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = CFG.get("TELEGRAM_CHAT_ID", "")
SIFTLY_URL = CFG.get("SIFTLY_URL", "http://localhost:3000")
PROXY_URL = CFG.get("PROXY_URL", "http://localhost:8089")
PROXY_API_KEY = CFG.get("PROXY_API_KEY", "optional-server-key-for-proxy-auth")
EXA_API_KEY = CFG.get("EXA_API_KEY", "")
AI_MODEL = CFG.get("AI_MODEL", "nvidia/nemotron-3-super-120b-a12b")
POLL_INTERVAL = int(CFG.get("POLL_INTERVAL", "45"))
MAX_PER_POLL = int(CFG.get("MAX_PER_POLL", "10"))
MAX_SCROLLS_REFRESH = int(CFG.get("MAX_SCROLLS_REFRESH", "2"))

def load_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        base = SYSTEM_PROMPT_FILE.read_text()
    else:
        base = "Tu es un analyste technique. Réponds uniquement en JSON valide."
    memory = load_memory()
    if memory:
        base += "\n\n# TA MÉMOIRE (apprends-la, respecte-la, elle fait foi)\n\n" + memory
    return base

# ---------------------------------------------------------------- état

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default

def save_json(path: Path, data) -> None:
    if path is SEEN_FILE and len(data) > 5000:
        data = dict(sorted(data.items(), key=lambda kv: kv[1])[-5000:])
    path.write_text(json.dumps(data, indent=1))

# ---------------------------------------------------------------- session X

def _capture_bookmarks_request(headless: bool) -> dict:
    """Ouvre la page bookmarks dans camoufox, intercepte la requête GraphQL
    Bookmarks (URL + réponse) et les cookies. Retourne la session complète."""
    captured: dict = {}

    def on_response(response):
        if "graphql" in response.url and "/Bookmarks" in response.url:
            try:
                captured["url"] = response.url
                captured["headers"] = dict(response.request.headers)
                captured["body"] = response.json()
            except Exception:
                pass

    with Camoufox(headless=headless, persistent_context=True, user_data_dir=PROFILE_DIR, geoip=True) as ctx:
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(5000)

        if "/login" in page.url or page.query_selector('input[autocomplete="username"]'):
            raise RuntimeError("Session X absente ou expirée — lance `python watcher.py login`.")

        # scroll pour déclencher la pagination et bien capter la requête
        for _ in range(MAX_SCROLLS_REFRESH):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        deadline = time.time() + 20
        while "url" not in captured and time.time() < deadline:
            time.sleep(0.5)
        if "url" not in captured:
            raise RuntimeError("Requête GraphQL Bookmarks non interceptée — réessaie.")

        cookies = {c["name"]: c["value"] for c in ctx.cookies("https://x.com")}

    headers = captured.get("headers", {})
    headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    session = {
        "bookmarks_url": captured["url"],
        "headers": headers,
        "cookies": cookies,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_json(SESSION_FILE, session)
    print(f"[session] capturée ({len(cookies)} cookies, bearer OK)")
    return {**session, "body": captured.get("body")}

def http_headers(session: dict) -> dict:
    if session.get("headers"):
        h = dict(session["headers"])
        # cookies frais > cookies figés dans les headers capturés
        h["cookie"] = "; ".join(f"{k}={v}" for k, v in session.get("cookies", {}).items())
        return h
    cookies = session.get("cookies", {})
    return {
        "authorization": session.get("authorization", X_WEB_BEARER),
        "x-csrf-token": cookies.get("ct0", ""),
        "cookie": f"auth_token={cookies.get('auth_token', '')}; ct0={cookies.get('ct0', '')}",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "referer": "https://x.com/i/bookmarks",
    }

def get_session(force_refresh: bool = False) -> dict:
    session = load_json(SESSION_FILE, None)
    if session and not force_refresh:
        return session
    result = _capture_bookmarks_request(headless=True)
    return {k: v for k, v in result.items() if k != "body"}

# ---------------------------------------------------------------- parse GraphQL

def parse_bookmarks_payload(payload: dict) -> list[dict]:
    """Extrait les tweets de la réponse GraphQL Bookmarks (formats v1/v2)."""
    out = []
    try:
        instructions = (
            payload.get("data", {}).get("bookmark_timeline_v2", {})
            .get("timeline", {}).get("instructions", [])
        ) or payload.get("data", {}).get("bookmark_timeline", {}).get("timeline", {}).get("instructions", [])

        def walk_entry(entry):
            content = entry.get("content", {})
            items = []
            if content.get("entryType") == "TimelineTimelineItem":
                items.append(content)
            elif content.get("entryType") == "TimelineTimelineModule":
                items.extend(content.get("items", []) or [])
                items = [i.get("item", {}) for i in items]
            return items

        for instr in instructions:
            for entry in instr.get("entries", []) or []:
                for content in walk_entry(entry):
                    ic = content.get("itemContent", {})
                    if ic.get("itemType") != "TimelineTweet":
                        continue
                    result = ic.get("tweet_results", {}).get("result", {})
                    if result.get("__typename") == "TweetWithVisibilityResults":
                        result = result.get("tweet", {})
                    legacy = result.get("legacy", {})
                    tweet_id = legacy.get("id_str") or result.get("rest_id")
                    if not tweet_id:
                        continue
                    # structure 2026 : user_results.result.core.screen_name
                    # (l'ancien legacy.screen_name reste en fallback)
                    uresult = result.get("core", {}).get("user_results", {}).get("result", {})
                    handle = (
                        uresult.get("core", {}).get("screen_name")
                        or uresult.get("legacy", {}).get("screen_name")
                        or ""
                    )
                    # tweets longs : le texte complet est dans note_tweet
                    note = (
                        result.get("note_tweet", {}).get("note_tweet_results", {})
                        .get("result", {}).get("text")
                    )
                    media = legacy.get("extended_entities", {}).get("media", []) or []
                    out.append({
                        "tweet_id": tweet_id,
                        "text": (note or legacy.get("full_text", "")).strip(),
                        "author_handle": handle,
                        "created_at": legacy.get("created_at", ""),
                        "images": [
                            m.get("media_url_https") for m in media
                            if m.get("type") == "photo" and m.get("media_url_https")
                        ],
                    })
    except Exception as e:
        print(f"[parse] erreur de parsing GraphQL: {e}")
    return out

# ---------------------------------------------------------------- polling

def poll_once(session: dict) -> tuple[list[dict], dict | None]:
    """Un poll HTTP pur. Retourne (tweets, erreur_de_session)."""
    try:
        r = requests.get(session["bookmarks_url"], headers=http_headers(session), timeout=20)
    except requests.RequestException as e:
        return [], f"réseau: {e}"
    if r.status_code in (401, 403, 404):
        return [], f"session expirée (HTTP {r.status_code})"
    if r.status_code == 429:
        return [], "rate-limit X (HTTP 429)"
    if r.status_code != 200:
        return [], f"HTTP {r.status_code}"
    return parse_bookmarks_payload(r.json()), None

# ---------------------------------------------------------------- siftly / exa / llm / telegram

def push_to_siftly(tweets: list[dict]) -> None:
    payload = {"bookmarks": [
        {
            "id": t["tweet_id"],
            "handle": t["author_handle"],
            "author": t["author_handle"],
            "text": t["text"],
            "media": [{"type": "photo", "url": u} for u in t["images"][:4]],
        }
        for t in tweets
    ]}
    try:
        # Siftly /api/import attend un multipart (champ file + source), pas du JSON brut
        r = requests.post(
            f"{SIFTLY_URL}/api/import",
            files={"file": ("bookmarks.json", json.dumps(payload), "application/json")},
            data={"source": "bookmark"},
            timeout=30,
        )
        r.raise_for_status()
        print(f"[siftly] import OK: {r.json()}")
        health("siftly", True)
    except Exception as e:
        print(f"[siftly] import échoué (non bloquant): {e}")
        health("siftly", False)

def export_obsidian() -> None:
    """Régénère le vault Obsidian via Siftly (régénération complète, idempotente)."""
    try:
        r = requests.get(f"{SIFTLY_URL}/api/export/obsidian", params={"format": "obsidian"}, timeout=120)
        r.raise_for_status()
        out = r.json()
        print(f"[obsidian] vault régénéré : {out.get('count', '?')} notes, {len(out.get('errors', []))} erreur(s)")
        health("obsidian", True)
    except Exception as e:
        print(f"[obsidian] export échoué (non bloquant): {e}")
        health("obsidian", False)

def exa_search(query: str, num_results: int = 4, with_text: bool = False, start_date: str | None = None) -> list[dict]:
    if not EXA_API_KEY or not query:
        return []
    try:
        contents = {"text": {"maxCharacters": 600}} if with_text else {"text": False}
        body = {"query": query[:400], "numResults": num_results, "type": "auto", "contents": contents}
        if start_date:
            body["startPublishedDate"] = start_date
        r = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
            json=body, timeout=20,
        )
        r.raise_for_status()
        out = []
        for x in r.json().get("results", []):
            item = {"title": x.get("title", ""), "url": x.get("url", ""),
                    "date": (x.get("publishedDate") or "")[:10]}
            if with_text:
                item["text"] = (x.get("text") or "")[:600]
            out.append(item)
        return out
    except Exception as e:
        print(f"[exa] erreur (non bloquant): {e}")
        return []

def _extract_json_object(text: str) -> str | None:
    """Premier objet JSON équilibré du texte (tolère le raisonnement autour)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None

def llm_brief(tweet: dict, exa_results: list[dict], recent: list[dict] | None = None) -> dict:
    evidence = "\n".join(
        f"- {r['title']} ({r['date'] or 'date inconnue'}) {r['url']}" for r in exa_results
    ) or "(aucun résultat web)"
    author = tweet["author_handle"] or "inconnu"
    already = ""
    if recent:
        lines = []
        for r in recent:
            b = r.get("brief", {}) or {}
            lines.append(f"- @{r['author']} [{(r.get('ts') or '')[:10]}] verdict={b.get('verdict')}: {r['text'][:120]}")
        already = ("SUJETS DÉJÀ TRAITÉS RÉCEMMENT (détecte les doublons, échos et suites) :\n"
                   + "\n".join(lines)
                   + '\n\nSi ce tweet recouvre fortement un sujet déjà traité, ajoute au JSON le champ '
                   '"dejavu": {"ref": "@auteur le JJ/MM (verdict)", "note": "1 phrase : suite/echo/approfondissement de quoi"}, '
                   "sinon \"dejavu\": null.\n\n")
    user_msg = f"""TWEET de @{author} (posté {tweet['created_at'] or 'date inconnue'}) :
\"\"\"{tweet['text'][:1500]}\"\"\"

RÉSULTATS WEB EXA (contexte : fraîcheur, écho éventuel) :
{evidence}

{already}Analyse selon le système. Réponds UNIQUEMENT en JSON."""
    last_err = None
    for attempt in range(4):   # NIM renvoie des rafales de 503 — retry avec backoff
        try:
            r = requests.post(
                f"{PROXY_URL}/v1/messages",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": 3000, "temperature": 0.4,
                      "system": load_system_prompt(),
                      "messages": [{"role": "user", "content": user_msg}]},
                timeout=120,
            )
            r.raise_for_status()
            blocks = r.json().get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            m = _extract_json_object(text)
            if not m:
                raise ValueError(f"pas de JSON: {text[:200]}")
            return json.loads(m)
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep((attempt + 1) * 4)
    print(f"[llm] brief échoué après 4 essais: {last_err}")
    return {}

VERDICT_LABEL = {
    "GAME_CHANGER": "🚀 GAME CHANGER",
    "SOLIDE": "✅ SOLIDE",
    "MOYEN": "😐 MOYEN",
    "BRUIT": "🗑️ BRUIT",
}

def _bullets(value, icon="•") -> list[str]:
    """Normalise un champ du brief (str ou list) en liste de bullets."""
    if isinstance(value, list):
        return [f"  {icon} {str(v).strip()}" for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [f"  {icon} {value.strip()}"]
    return []

def send_telegram(tweet: dict, brief: dict, exa_results: list[dict] | None = None) -> int | None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] token/chat_id manquants — message non envoyé")
        return None
    author = tweet["author_handle"] or "x"
    url = f"https://x.com/{author}/status/{tweet['tweet_id']}"
    verdict = VERDICT_LABEL.get(brief.get("verdict", ""), "❓ " + str(brief.get("verdict", "?")))
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    excerpt = esc(tweet["text"][:280])

    lines = [f"🐦 <a href=\"{url}\">Bookmark</a> — @{author}", ""]
    if excerpt:
        lines += [f"<i>« {excerpt}… »</i>", ""]
    lines.append(f"<b>{verdict}</b>")

    dj = brief.get("dejavu")
    if isinstance(dj, dict) and dj.get("ref"):
        lines.append(f"\n🍲 <b>Déjà vu</b> — {esc(dj.get('ref', ''))} : {esc(dj.get('note', ''))}")

    resume = _bullets(brief.get("resume"))
    if resume:
        lines.append("\n<b>🎯 C'est quoi</b>")
        lines += [esc(b) for b in resume]
    if brief.get("nouveaute"):
        lines.append(f"\n🆕 <b>{esc(brief['nouveaute'])}</b> — {esc(brief.get('contexte_nouveaute', ''))}")
    raison = _bullets(brief.get("raison"))
    if raison:
        lines.append("\n<b>💡 Ce qu'elle vaut</b>")
        lines += [esc(b) for b in raison]
    limites = _bullets(brief.get("limites"))
    if limites:
        lines.append("\n<b>⚠️ Limites</b>")
        lines += [esc(b) for b in limites]
    srcs = [s for s in (brief.get("sources") or []) if isinstance(s, str)][:3]
    if srcs:
        lines += ["", "Sources: " + " · ".join(f"<a href=\"{s}\">lien</a>" for s in srcs)]

    suggestions = [str(s).strip() for s in (brief.get("suggestions") or []) if str(s).strip()][:3]
    if suggestions:
        lines.append("\n<b>👉 Pour creuser</b>")
        lines += [esc(f"  • {s}") for s in suggestions]

    msg = "\n".join(lines)[:3900]

    # boutons inline : feedback + approfondir/comparer + suggestions
    keyboard = []
    tid = tweet["tweet_id"][-12:]   # callback_data limité à 64 octets
    keyboard.append([{"text": "🔥", "callback_data": f"fb:hot:{tid}"},
                     {"text": "😐", "callback_data": f"fb:meh:{tid}"},
                     {"text": "🗑️", "callback_data": f"fb:trash:{tid}"},
                     {"text": "🔬", "callback_data": f"deep:{tid}"},
                     {"text": "🆚", "callback_data": f"cmp:{tid}"}])
    for i, s in enumerate(suggestions):
        label = s if len(s) <= 40 else s[:37] + "…"
        keyboard.append([{"text": f"👉 {label}", "callback_data": f"sugg:{i}:{tid}"}])

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": False,
                  "reply_markup": {"inline_keyboard": keyboard}},
            timeout=20,
        )
        if not r.ok:
            print(f"[telegram] erreur {r.status_code}: {r.text[:200]}")
            return None
        msg_id = r.json().get("result", {}).get("message_id")
        print(f"[telegram] envoyé ({url})")
        record_brief(tweet, brief, exa_results or [], msg_id)
        return msg_id
    except Exception as e:
        print(f"[telegram] échec: {e}")
        return None

# ---------------------------------------------------------------- contexte conversationnel

BOT_CONTEXT_FILE = BASE / "bot-context.json"
MSG_TO_CONTEXT: dict[int, dict] = {}   # message_id -> record (reconstruit au démarrage)

def record_brief(tweet: dict, brief: dict, exa_results: list[dict], msg_id: int | None) -> None:
    """Mémorise un brief pour le mode questions/réponses."""
    records = load_json(BOT_CONTEXT_FILE, [])
    record = {
        "tweet_id": tweet["tweet_id"],
        "author": tweet["author_handle"],
        "text": tweet["text"][:1500],
        "created_at": tweet["created_at"],
        "brief": brief,
        "sources": exa_results,
        "msg_id": msg_id,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    records = [r for r in records if r["tweet_id"] != record["tweet_id"]]
    records.append(record)
    save_json(BOT_CONTEXT_FILE, records[-30:])
    if msg_id:
        MSG_TO_CONTEXT[msg_id] = record

def load_context_index() -> None:
    for r in load_json(BOT_CONTEXT_FILE, []):
        if r.get("msg_id"):
            MSG_TO_CONTEXT[r["msg_id"]] = r

def recent_context(n: int = 8) -> list[dict]:
    return load_json(BOT_CONTEXT_FILE, [])[-n:]

def llm_answer(question: str, records: list[dict], exa_results: list[dict] | None = None) -> str:
    """Mode conversationnel : réponse texte libre avec le contexte des bookmarks + recherche web."""
    ctx = []
    for r in records:
        b = r.get("brief", {})
        ctx.append(
            f"Tweet de @{r['author']} ({r['created_at']}):\n\"\"\"{r['text'][:800]}\"\"\"\n"
            f"Brief envoyé: verdict={b.get('verdict')}, résumé={b.get('resume')}, "
            f"fraîcheur={b.get('nouveaute')} — {b.get('contexte_nouveaute')}\n"
            f"Limites notées: {b.get('limites')}\n"
            f"Sources: {json.dumps([s.get('url') for s in (r.get('sources') or []) if isinstance(s, dict)])}"
        )
    web = ""
    if exa_results:
        lines = []
        for s in exa_results:
            lines.append(f"- **{s['title']}** ({s.get('date') or 'date inconnue'}) {s['url']}")
            if s.get("text"):
                lines.append(f"  {s['text'][:500]}")
        web = "\n\nRÉSULTATS DE RECHERCHE WEB POUR CETTE QUESTION (frais, à utiliser en priorité) :\n" + "\n".join(lines)
    system = load_system_prompt() + (
        "\n\nMODE ACTUEL : conversation. L'utilisateur pose une question sur ses bookmarks récents. "
        "Réponds en texte libre (pas de JSON), en pote cash, direct et utile. "
        "Des résultats de recherche web frais sont fournis avec la question : UTILISE-LES. "
        "Ne demande JAMAIS à l'utilisateur de te donner un lien ou une source — cherche et réponds."
    )
    user = (f"BOOKMARKS RÉCENTS ET LEURS BRIEFS :\n\n" + "\n\n---\n\n".join(ctx)
            + web
            + f"\n\nQUESTION DE L'UTILISATEUR :\n{question}")
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                f"{PROXY_URL}/v1/messages",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": 2000, "temperature": 0.5,
                      "system": system,
                      "messages": [{"role": "user", "content": user}]},
                timeout=120,
            )
            r.raise_for_status()
            blocks = r.json().get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            if text:
                return text
            raise ValueError("réponse vide")
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep((attempt + 1) * 4)
    return f"(j'ai raté la réponse : {last_err})"

def find_record_by_tid(tid: str) -> dict | None:
    for r in reversed(load_json(BOT_CONTEXT_FILE, [])):
        if r["tweet_id"].endswith(tid):
            return r
    return None

def update_record(tweet_id: str, mutate) -> dict | None:
    """Applique une mutation au record correspondant et le resauvegarde."""
    records = load_json(BOT_CONTEXT_FILE, [])
    for r in records:
        if r["tweet_id"] == tweet_id:
            mutate(r)
            save_json(BOT_CONTEXT_FILE, records)
            if r.get("msg_id"):
                MSG_TO_CONTEXT[r["msg_id"]] = r
            return r
    return None

FEEDBACK_ACK = {"hot": "🔥 noté — je renforce ce genre", "meh": "😐 noté — calibrage en cours",
                "trash": "🗑️ noté — je serai plus dur la prochaine fois"}

def handle_callback(cb: dict) -> None:
    """Boutons inline des briefs : feedback / approfondir / comparer / suggestion."""
    data = cb.get("data", "") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                  json={"callback_query_id": cb["id"]}, timeout=10)

    tid = data.split(":")[-1]
    record = MSG_TO_CONTEXT.get(msg.get("message_id")) or find_record_by_tid(tid)
    if not record:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": "🤔 j'ai perdu le contexte de ce brief (redémarrage ?) — renvoie-moi le lien du tweet"}, timeout=15)
        return

    if data.startswith("fb:"):
        kind = data.split(":")[1]
        update_record(record["tweet_id"], lambda r: r.setdefault("reactions", {}).update({kind: r.get("reactions", {}).get(kind, 0) + 1}))
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"], "text": FEEDBACK_ACK.get(kind, "noté"), "show_alert": False}, timeout=10)
        print(f"[feedback] {kind} sur {record['tweet_id']}")
        return

    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction",
                  json={"chat_id": chat_id, "action": "typing"}, timeout=10)

    if data.startswith("deep:"):
        question = ("Deep dive ce sujet : va chercher la source principale (repo/article), "
                    "vérifie les claims en détail, architecture, alternatives, et ce que le tweet ne dit pas.")
    elif data.startswith("cmp:"):
        question = ("Fais le parallèle avec les alternatives existantes les plus pertinentes : "
                    "forces/faiblesses de chacune, et laquelle mérite notre attention.")
    elif data.startswith("sugg:"):
        try:
            idx = int(data.split(":")[1])
            question = str((record.get("brief", {}).get("suggestions") or [])[idx])
        except (ValueError, IndexError):
            question = "Approfondis le point le plus intéressant de ce sujet."
    else:
        return

    ask_and_send(question, record, chat_id, context_note=f"Tweet concerné de @{record['author']} : « {record['text'][:400]} »")

def ask_and_send(question: str, records: list[dict], chat_id, context_note: str | None = None, search_extra: str = "") -> None:
    """Recherche web + LLM + envoi de la réponse conversationnelle."""
    q_full = f"{question}\n\n({context_note})" if context_note else question
    print(f"[ask] {question[:60]}")
    web = exa_search(f"{question} {(records[0]['text'][:150] if records else search_extra)[:200]}",
                     num_results=6, with_text=True)
    answer = llm_answer(q_full, records, web)
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(s))
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": chat_id, "text": html(answer)[:4000], "parse_mode": "HTML"},
                  timeout=30)
    print(f"[ask] réponse envoyée ({len(answer)} chars)")

def telegram_listener() -> None:
    """Long polling getUpdates — répond aux questions sur les bookmarks."""
    load_context_index()
    offset = 0
    print("[listener] Telegram en écoute")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30,
                        "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=40,
            )
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                    continue
                msg = u.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()

                # vocaux → whisper → traités comme une question texte
                voice = msg.get("voice") or msg.get("audio")
                if voice and not text:
                    tmp = BASE / f"voice_{u['update_id']}.ogg"
                    if download_telegram_file(voice["file_id"], tmp):
                        text = transcribe_voice(str(tmp))
                        tmp.unlink(missing_ok=True)
                        if text:
                            print(f"[voice] transcrit: {text[:80]!r}")
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": f"🎙️ <i>{esc(text[:200])}</i>"},
                                timeout=15,
                            )
                        else:
                            print("[voice] transcription vide")

                if not chat_id or not text or str(chat_id) != str(TELEGRAM_CHAT_ID):
                    continue
                reply = msg.get("reply_to_message", {})
                reply_id = reply.get("message_id")
                record = MSG_TO_CONTEXT.get(reply_id)
                lower = text.lower()

                if lower.startswith(("retiens", "note que", "apprends")):
                    # mémoire : instruction durable à retenir
                    remember_feedback(text)
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": "🧠 Retenu ! C'est dans ma mémoire, je l'appliquerai."},
                        timeout=15,
                    )
                    continue

                # le bot cherche LUI-MÊME sur le web avant de répondre
                log_question(text)
                if lower.startswith(("cherche ", "recherche ", "dans ma biblio", "c'était quoi",
                                     "c'etait quoi", "j'avais", "retrouve", "t'as pas un tweet")):
                    results = search_library(text)
                    if results:
                        note = "Résultats de SA bibliothèque Siftly :\n" + "\n".join(
                            f"- @{r['author']} (verdict {r['brief'].get('verdict')}) : {r['text'][:200]} "
                            f"→ https://x.com/{r['author']}/status/{r['tweet_id']}" for r in results)
                        ask_and_send(f"L'utilisateur cherche dans SA bibliothèque : « {text} ». "
                                     f"Présente chaque résultat (verdict + 1 bullet + lien cliquable) et propose un angle d'analyse.",
                                     results, chat_id, context_note=note)
                    else:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                      json={"chat_id": chat_id, "text": "🤷 rien trouvé dans ta bibliothèque sur ça — "
                                            "essaie avec d'autres mots, ou bookmark-le d'abord 😉"}, timeout=15)
                    continue
                if record and lower in ("deep", "approfondir", "approfondis", "🔬"):
                    ask_and_send("Deep dive ce sujet : va chercher la source principale (repo/article), "
                                 "vérifie les claims en détail, architecture, alternatives, et ce que le tweet ne dit pas.",
                                 [record], chat_id,
                                 context_note=f"Tweet concerné de @{record['author']} : « {record['text'][:400]} »")
                    continue
                web = exa_search(text, num_results=6, with_text=True)
                if record:
                    ask_and_send(text, [record], chat_id, search_extra=record["text"][:150])
                elif text.startswith("/"):
                    answer = ("Commandes : réponds à n'importe quel brief pour me poser une question "
                              "précise, ou écris-moi direct pour parler des derniers bookmarks. "
                              "Vocaux OK aussi 🎙️ — et commence par « retiens… » pour m'apprendre quelque chose.")
                    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                  json={"chat_id": chat_id, "text": esc(answer), "parse_mode": "HTML"}, timeout=15)
                else:
                    ask_and_send(text, recent_context(), chat_id, search_extra=text)
                    continue
                esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                # **gras** du modèle -> <b> Telegram (après échappement)
                html = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(s))
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": html(answer)[:4000], "parse_mode": "HTML"},
                    timeout=30,
                )
                print(f"[listener] réponse envoyée ({len(answer)} chars)")
        except Exception as e:
            print(f"[listener] erreur: {e}")
            time.sleep(5)

# ---------------------------------------------------------------- bibliothèque parlante

SIFTLY_DB = Path(CFG.get("SIFTLY_DB", str(BASE.parent / "siftly" / "prisma" / "dev.db")))

def search_library(query: str, limit: int = 5) -> list[dict]:
    """Recherche FTS5 dans la bibliothèque Siftly (lecture directe SQLite)."""
    clean = re.sub(r"[^A-Za-z0-9À-ÿ ]", " ", query).strip()
    if not clean:
        return []
    terms = " ".join(f'"{t}"*' for t in clean.split()[:6])
    out = []
    try:
        con = sqlite3.connect(f"file:{SIFTLY_DB}?mode=ro", uri=True)
        rows = con.execute(
            """SELECT b."tweetId", b.text, b."authorHandle", b.verdict, b.ai_summary
               FROM bookmark_fts f JOIN Bookmark b ON b.id = f.bookmark_id
               WHERE bookmark_fts MATCH ? ORDER BY rank LIMIT ?""",
            (terms, limit),
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"[library] FTS erreur: {e}")
        rows = []
    if not rows:   # fallback LIKE si le FTS ne matche pas
        try:
            con = sqlite3.connect(f"file:{SIFTLY_DB}?mode=ro", uri=True)
            like = f"%{clean.split()[0]}%"
            rows = con.execute(
                """SELECT "tweetId", text, "authorHandle", verdict, ai_summary
                   FROM Bookmark WHERE text LIKE ? ORDER BY "importedAt" DESC LIMIT ?""",
                (like, limit),
            ).fetchall()
            con.close()
        except Exception:
            rows = []
    for tweet_id, text, handle, verdict, summary in rows:
        out.append({
            "tweet_id": tweet_id, "author": handle or "x", "created_at": "",
            "text": (text or "")[:800],
            "brief": {"verdict": verdict or "?", "resume": summary or ""},
            "sources": [], "ts": "",
        })
    return out

# ---------------------------------------------------------------- watchlist GitHub

WATCHLIST_FILE = BASE / "watchlist.json"
GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")

def build_watchlist() -> dict:
    """Repose sur les records SOLIDE/GAME_CHANGER contenant un lien GitHub."""
    wl = {k.lower(): v for k, v in load_json(WATCHLIST_FILE, {}).items()}
    for r in load_json(BOT_CONTEXT_FILE, []):
        if (r.get("brief", {}) or {}).get("verdict") not in ("SOLIDE", "GAME_CHANGER"):
            continue
        urls = [s.get("url", "") for s in (r.get("sources") or []) if isinstance(s, dict)]
        found = GITHUB_RE.findall(" ".join(urls + [r.get("text", "")]))
        for slug in found:
            slug = slug.rstrip(".…,)")
            if slug.count("/") == 1 and len(slug) < 60:
                key = slug.lower()
                wl.setdefault(key, {"added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "last_tag": None, "display": slug})
    if wl:
        save_json(WATCHLIST_FILE, wl)
    return wl

def check_watchlist() -> int:
    """Vérifie les nouvelles releases GitHub ; alerte Telegram si nouveau tag."""
    wl = build_watchlist()
    if not wl:
        print("[watch] watchlist vide")
        return 0
    alerts = 0
    for slug, info in list(wl.items())[:25]:
        try:
            r = requests.get(f"https://api.github.com/repos/{slug}/releases/latest", timeout=15)
            if r.status_code == 404:   # pas de release formelle — skip silencieux
                continue
            r.raise_for_status()
            tag = r.json().get("tag_name") or ""
            if not tag:
                continue
            if info.get("last_tag") and info["last_tag"] != tag:
                name = r.json().get("name") or tag
                body = (r.json().get("body") or "")[:600]
                tg_send(f"🚀 **{slug}** — nouvelle release **{tag}** ({name})\n\n{body}\n\nhttps://github.com/{slug}/releases")
                alerts += 1
            info["last_tag"] = tag
            info["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        except Exception as e:
            print(f"[watch] {slug}: {e}")
        time.sleep(1)
    save_json(WATCHLIST_FILE, wl)
    print(f"[watch] {len(wl)} repos surveillés, {alerts} nouvelle(s) release(s)")
    return alerts

# ---------------------------------------------------------------- santé / auto-surveillance

_HEALTH = {"poll": 0, "brief": 0, "siftly": 0, "obsidian": 0}
_ALERTED: set[str] = set()
HEALTH_THRESHOLD = 3
HEALTH_LABEL = {
    "poll": "le polling X plante en boucle (session ou réseau ?)",
    "brief": "les briefs LLM échouent (proxy/NIM down ?)",
    "siftly": "l'import vers Siftly échoue",
    "obsidian": "l'export Obsidian échoue",
}

def health(key: str, ok: bool) -> None:
    if ok:
        _HEALTH[key] = 0
        if key in _ALERTED:
            _ALERTED.discard(key)
            tg_send(f"✅ **Rétabli** — {HEALTH_LABEL[key]}")
        return
    _HEALTH[key] = _HEALTH.get(key, 0) + 1
    if _HEALTH[key] >= HEALTH_THRESHOLD and key not in _ALERTED:
        _ALERTED.add(key)
        tg_send(f"🩺 **J'ai un problème** — {HEALTH_LABEL[key]} ({_HEALTH[key]} échecs d'affilée). "
                f"Regarde `journalctl --user -u bookmark-watcher -f`.")
        print(f"[health] alerte envoyée: {key}")

# ---------------------------------------------------------------- digest + veille proactive

def tg_send(text: str) -> None:
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(s))
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": html(text)[:4000], "parse_mode": "HTML",
                        "disable_web_page_preview": True}, timeout=30)

def _records_last_24h() -> list[dict]:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out = []
    for r in load_json(BOT_CONTEXT_FILE, []):
        try:
            if datetime.fromisoformat(r.get("ts", "")) >= cutoff:
                out.append(r)
        except ValueError:
            pass
    return out

def _llm_text(prompt: str, max_tokens: int = 1500) -> str:
    for attempt in range(3):
        try:
            r = requests.post(
                f"{PROXY_URL}/v1/messages",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": max_tokens, "temperature": 0.5,
                      "system": load_system_prompt(),
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=180,
            )
            r.raise_for_status()
            text = "\n".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
            if text:
                return text
        except Exception as e:
            if attempt == 2:
                print(f"[llm] échec: {e}")
            time.sleep((attempt + 1) * 4)
    return ""

def mode_digest() -> None:
    recs = _records_last_24h()
    if not recs:
        tg_send("☀️ **Digest** — veille calme ces dernières 24 h, rien de nouveau bookmarké.")
        return
    lines = []
    for r in recs:
        b = r.get("brief", {}) or {}
        author = r.get("author") or "x"
        url = f"https://x.com/{author}/status/{r['tweet_id']}"
        lines.append(f"- verdict={b.get('verdict')} | @{author} | {r['text'][:150]} | {url} | "
                     f"déjà_vu={json.dumps(b.get('dejavu')) if b.get('dejavu') else 'non'} | "
                     f"réactions={json.dumps(r.get('reactions', {}))}")
    prompt = ("Rédige le DIGEST DU MATIN pour l'utilisateur, à partir des bookmarks des dernières 24 h.\n\n"
              "BOOKMARKS :\n" + "\n".join(lines) + "\n\n"
              "Format obligatoire, enjoué et ultra-structuré (bullets courts, PAS de pavés) :\n"
              "- 1 ligne d'intro d'humeur\n"
              "- '🏆 À ne pas rater' : top 2-3 avec lien et 1 bullet chacun\n"
              "- '📊 Le reste en vrac' : une ligne par autre bookmark (verdict + sujet)\n"
              "- '🍲 Doublons torchés' si des déjà-vu\n"
              "- Termine par UNE question de relance liée au sujet le plus intéressant")
    digest = _llm_text(prompt)
    if digest:
        tg_send("☀️ **DIGEST DU MATIN**\n\n" + digest)
        print("[digest] envoyé")
    else:
        print("[digest] génération échouée")

def mode_proactive() -> None:
    themes = " | ".join(r["text"][:80] for r in load_json(BOT_CONTEXT_FILE, [])[-15:])
    learnings = LEARNINGS_FILE.read_text()[:1500]
    q = _llm_text(
        f"Sur la base de ses thèmes récents : {themes}\n\nSA MÉMOIRE D'APPRENTISSAGE :\n{learnings}\n\n"
        "Propose exactement 3 requêtes de recherche web pour lui trouver du contenu FRAÎS (dernières 48 h) "
        "qu'il n'a probablement pas vu — nouveaux outils, releases, vulnérabilités, annonces. "
        "Évite les sujets déjà dans la liste. Réponds UNIQUEMENT avec un JSON array de 3 strings.",
        max_tokens=300,
    )
    try:
        m = re.search(r"\[[\s\S]*\]", q)
        queries = json.loads(m.group(0))[:3] if m else []
    except Exception:
        queries = []
    if not queries:
        print("[proactive] pas de requêtes générées")
        return
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    results = []
    for query in queries:
        for s in exa_search(query, num_results=4, with_text=False, start_date=yesterday):
            results.append({"query": query, **s})
        time.sleep(1)
    if not results:
        print("[proactive] rien de frais trouvé")
        return
    src = "\n".join(f"- [{r.get('date', '?')}] {r['title']} — {r['url']}" for r in results[:20])
    curated = _llm_text(
        f"Résultats de recherche web frais (48 h) sur ses thèmes :\n{src}\n\n"
        "Sélectionne les 2-3 VRAIMENT intéressants pour un expert cyber/dev (ignorer le contenu sponsorisé/recyclé). "
        "Pour chacun : titre en gras, 1-2 bullets (quoi + pourquoi ça le concerne), lien. "
        "Si rien ne vaut le coup, dis-le franchement en une ligne. Enjoué, bullets courts, PAS de pavés. "
        "Commence par '📡 VEILLE PROACTIVE — j'ai cherché pour toi'.",
        max_tokens=900,
    )
    if curated:
        tg_send(curated)
        print(f"[proactive] envoyé ({len(results)} résultats examinés)")

def mode_morning() -> None:
    mode_digest()
    time.sleep(3)
    mode_proactive()
    time.sleep(3)
    check_watchlist()

def mode_weekly() -> None:
    """Mémo hebdomadaire : note Markdown dans le vault Obsidian + message Telegram."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recs = []
    for r in load_json(BOT_CONTEXT_FILE, []):
        try:
            if datetime.fromisoformat(r.get("ts", "")) >= cutoff:
                recs.append(r)
        except ValueError:
            pass
    if not recs:
        print("[weekly] rien cette semaine")
        return
    lines = []
    for r in recs:
        b = r.get("brief", {}) or {}
        lines.append(f"- {b.get('verdict')} | @{r['author']} | {r['text'][:120]} | "
                     f"réactions={json.dumps(r.get('reactions', {}))}")
    memo = _llm_text(
        "Rédige la MÉMO HEBDO de veille en Markdown (langue fr, enjoué, ULTRA structuré : headers ##, "
        "bullets courts d'une ligne, zéro pavé). Sections :\n"
        "## 🏆 Les 3 qui valent le coup (titre + 1 bullet pourquoi + lien)\n"
        "## 📊 Le reste (verdict / sujet / @auteur, une ligne chacun)\n"
        "## 🍲 Doublons et bruit torchés\n"
        "## 🧠 Ce que j'ai appris de tes goûts cette semaine (2-3 bullets)\n\n"
        "BOOKMARKS DE LA SEMAINE :\n" + "\n".join(lines) +
        f"\n\nSA MÉMOIRE D'APPRENTISSAGE :\n{LEARNINGS_FILE.read_text()[:1200]}",
        max_tokens=1800,
    )
    if not memo:
        print("[weekly] génération échouée")
        return
    week = datetime.now(timezone.utc).isocalendar()
    vault = BASE.parent / "Obsidian" / "Twitter-Graph"
    memo_dir = vault / "Memos"
    memo_dir.mkdir(parents=True, exist_ok=True)
    path = memo_dir / f"Semaine-{week.week:02d}.md"
    path.write_text(f"# Veille — Semaine {week.week:02d} ({week.year})\n\n{memo}\n")
    tg_send(f"📖 **Mémo hebdo écrite** → Obsidian : `Memos/Semaine-{week.week:02d}.md`\n\n{memo[:2500]}")
    print(f"[weekly] écrite: {path}")

# ---------------------------------------------------------------- vocaux (whisper local)

_WHISPER = None
def transcribe_voice(ogg_path: str) -> str:
    """Transcrit un vocal avec faster-whisper (modèle chargé au premier usage)."""
    global _WHISPER
    if _WHISPER is None:
        print("[voice] chargement du modèle whisper (première fois)…")
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _WHISPER.transcribe(ogg_path, language="fr", beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()

def download_telegram_file(file_id: str, dest: Path) -> bool:
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
                         params={"file_id": file_id}, timeout=15)
        path = r.json()["result"]["file_path"]
        data = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=30)
        dest.write_bytes(data.content)
        return True
    except Exception as e:
        print(f"[voice] téléchargement échoué: {e}")
        return False

# ---------------------------------------------------------------- apprentissage

FEEDBACK_FILE = MEMORY_DIR / "feedback.md"
LEARNINGS_FILE = MEMORY_DIR / "learnings.md"
QUESTIONS_LOG = BASE / "questions.log"
REFLECT_EVERY = 20   # briefs traités entre deux passes d'auto-réflexion
_reflect_counter = 0

def remember_feedback(text: str) -> None:
    """Le user a dit 'retiens …' : condense en une ligne et l'ajoute au feedback."""
    old = FEEDBACK_FILE.read_text().strip()
    r = requests.post(
        f"{PROXY_URL}/v1/messages",
        headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
        json={"model": AI_MODEL, "max_tokens": 200, "temperature": 0.2,
              "messages": [{"role": "user", "content":
                  f"Condense cette instruction utilisateur en UNE ligne de consigne durable, "
                  f"impérative, au 3e personne neutre (pas de 'il faut que je'). "
                  f"Préfixe par la date du jour {datetime.now():%Y-%m-%d}. Réponds UNIQUEMENT avec la ligne.\n\n"
                  f"Instruction : {text}"}]},
        timeout=60,
    )
    line = "\n".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    if line:
        FEEDBACK_FILE.write_text(old + f"\n- {line[:300]}\n")
        print(f"[memory] feedback retenu: {line[:80]}")

def log_question(text: str) -> None:
    try:
        with QUESTIONS_LOG.open("a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M}\t{text[:300]}\n")
    except OSError:
        pass

def maybe_reflect() -> None:
    """Tous les REFLECT_EVERY briefs : passe d'auto-réflexion → met à jour learnings.md."""
    global _reflect_counter
    _reflect_counter += 1
    if _reflect_counter % REFLECT_EVERY != 0:
        return
    print("[memory] passe d'auto-réflexion…")
    records = load_json(BOT_CONTEXT_FILE, [])
    stats = []
    for r in records[-40:]:
        b = r.get("brief", {})
        stats.append(f"- @{r['author']}: verdict={b.get('verdict')}, fraîcheur={b.get('nouveaute')}, "
                     f"réactions={json.dumps(r.get('reactions', {}))}, thème={r['text'][:100]!r}")
    questions = ""
    if QUESTIONS_LOG.exists():
        qlines = QUESTIONS_LOG.read_text().strip().splitlines()[-30:]
        questions = "\n\nDERNIÈRES QUESTIONS DU USER :\n" + "\n".join(qlines)
    old_learnings = LEARNINGS_FILE.read_text().strip()
    r = requests.post(
        f"{PROXY_URL}/v1/messages",
        headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
        json={"model": AI_MODEL, "max_tokens": 1200, "temperature": 0.3,
              "messages": [{"role": "user", "content":
                  f"Tu maintiens la mémoire d'apprentissage d'un bot de veille pour un utilisateur expert cyber/dev.\n\n"
                  f"MÉMOIRE ACTUELLE :\n{old_learnings}\n\n"
                  f"STATISTIQUES RÉCENTES (verdicts des derniers briefs) :\n" + "\n".join(stats) + questions + "\n\n"
                  f"Mets à jour la mémoire : que remarques-tu sur ses goûts (verdicts acceptés, thèmes récurrents, "
                  f"sujets de questions) ? Garde uniquement ce qui reste utile, retire ce qui est dépassé, "
                  f"ajoute les nouveaux patterns concrets avec dates. Reste compact (max 40 lignes), "
                  f"même structure de sections. Réponds UNIQUEMENT avec le nouveau contenu Markdown complet."}]},
        timeout=180,
    )
    content = "\n".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    if content.startswith("#") and len(content) < 6000:
        LEARNINGS_FILE.write_text(content)
        print("[memory] learnings.md mis à jour")
    else:
        print("[memory] réflexion ignorée (réponse inattendue)")

# ---------------------------------------------------------------- cycle

def process_new(tweets: list[dict], seen: dict, notify: bool) -> int:
    fresh = [t for t in tweets if t["tweet_id"] not in seen]
    fresh = list(reversed(fresh))[:MAX_PER_POLL]
    if not fresh:
        return 0

    if notify:
        print(f"[poll] {len(fresh)} nouveaux bookmarks à traiter")
        push_to_siftly(fresh)
    else:
        print(f"[poll] baseline : {len(fresh)} tweets marqués vus sans notification")

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in fresh:
        if notify:
            print(f"[brief] https://x.com/{t['author_handle'] or 'x'}/status/{t['tweet_id']}")
            exa = exa_search(t["text"])
            brief = llm_brief(t, exa, recent=recent_context(15))
            health("brief", bool(brief))
            send_telegram(t, brief, exa)
            time.sleep(2)
        seen[t["tweet_id"]] = now_iso
        save_json(SEEN_FILE, seen)   # sauvegarde à chaque envoi : pas de re-spam si crash
    return len(fresh)

def poll_cycle(notify: bool = True) -> int:
    print(f"[poll] {datetime.now().isoformat(timespec='seconds')}")
    session = get_session()
    tweets, err = poll_once(session)
    if err:
        print(f"[poll] {err} — rafraîchissement de session via navigateur…")
        session = get_session(force_refresh=True)
        tweets, err2 = poll_once(session)
        if err2:
            print(f"[poll] échec après refresh : {err2} — cycle abandonné")
            return 0
    print(f"[poll] {len(tweets)} tweets reçus")
    n = process_new(tweets, load_json(SEEN_FILE, {}), notify=notify)
    if n > 0 and notify:
        # import + analyse faits : on régénère le vault (le 2e export du cycle
        # suivant rattrapera tags/catégories arrivés entre-temps)
        export_obsidian()
        maybe_reflect()
    return n

def mode_login() -> None:
    print("Connexion X — connecte-toi dans la fenêtre qui s'ouvre.")
    print(f"Profil persistant : {PROFILE_DIR}")
    with Camoufox(headless=False, persistent_context=True, user_data_dir=PROFILE_DIR, geoip=True) as ctx:
        page = ctx.new_page()
        page.goto("https://x.com/login", wait_until="domcontentloaded")
        try:
            page.wait_for_url("**/home", timeout=600_000)
            print("Connecté — session sauvegardée. Lance maintenant : python watcher.py once")
        except Exception:
            print("Fenêtre fermée avant la fin — relance si la session n'est pas prise.")

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if mode == "login":
        mode_login()
    elif mode == "refresh":
        _capture_bookmarks_request(headless=True)
    elif mode == "once":
        n = poll_cycle(notify=True)
        print(f"[once] {n} nouveau(x) traité(s)")
    elif mode == "baseline":
        # premier lancement : marque tout ce qui existe comme déjà vu, sans spammer
        n = poll_cycle(notify=False)
        print(f"[baseline] {n} tweets marqués vus")
    elif mode == "weekly":
        mode_weekly()
    elif mode == "digest":
        mode_digest()
    elif mode == "morning":
        mode_morning()
    elif mode == "daemon":
        import threading
        print(f"[daemon] démarré — poll HTTP toutes les {POLL_INTERVAL}s")
        threading.Thread(target=telegram_listener, daemon=True).start()
        pending_export = False
        while True:
            try:
                n = poll_cycle(notify=True)
                health("poll", True)
                if pending_export:
                    # rattrape les tags/catégories calculés depuis l'export précédent
                    export_obsidian()
                    pending_export = False
                if n > 0:
                    pending_export = True
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[daemon] erreur de cycle: {e}")
                health("poll", False)
            time.sleep(POLL_INTERVAL)
    else:
        print(f"Usage: {sys.argv[0]} [login|refresh|baseline|once|daemon]")
        sys.exit(1)

if __name__ == "__main__":
    main()
