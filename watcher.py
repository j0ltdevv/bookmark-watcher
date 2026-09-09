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
        return SYSTEM_PROMPT_FILE.read_text()
    return "Tu es un analyste technique. Réponds uniquement en JSON valide."

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
        r = requests.post(f"{SIFTLY_URL}/api/import", json=payload, timeout=30)
        r.raise_for_status()
        print(f"[siftly] import OK: {r.json()}")
    except Exception as e:
        print(f"[siftly] import échoué (non bloquant): {e}")

def exa_search(query: str) -> list[dict]:
    if not EXA_API_KEY or not query:
        return []
    try:
        r = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
            json={"query": query[:400], "numResults": 4, "type": "auto",
                  "contents": {"text": False}},
            timeout=20,
        )
        r.raise_for_status()
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""),
             "date": (x.get("publishedDate") or "")[:10]}
            for x in r.json().get("results", [])
        ]
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

def llm_brief(tweet: dict, exa_results: list[dict]) -> dict:
    evidence = "\n".join(
        f"- {r['title']} ({r['date'] or 'date inconnue'}) {r['url']}" for r in exa_results
    ) or "(aucun résultat web)"
    author = tweet["author_handle"] or "inconnu"
    user_msg = f"""TWEET de @{author} (posté {tweet['created_at'] or 'date inconnue'}) :
\"\"\"{tweet['text'][:1500]}\"\"\"

RÉSULTATS WEB EXA (contexte : fraîcheur, écho éventuel) :
{evidence}

Analyse selon le système. Réponds UNIQUEMENT en JSON."""
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

def send_telegram(tweet: dict, brief: dict) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] token/chat_id manquants — message non envoyé")
        return
    author = tweet["author_handle"] or "x"
    url = f"https://x.com/{author}/status/{tweet['tweet_id']}"
    verdict = VERDICT_LABEL.get(brief.get("verdict", ""), "❓ " + str(brief.get("verdict", "?")))
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    excerpt = esc(tweet["text"][:300])

    lines = [f"🐦 <a href=\"{url}\">Nouveau bookmark</a> — @{author}", ""]
    if excerpt:
        lines += [f"<i>« {excerpt}… »</i>", ""]
    lines.append(f"<b>{verdict}</b>")
    if brief.get("resume"):
        lines += ["", esc(brief["resume"])]
    if brief.get("nouveaute"):
        lines.append(f"\n🆕 <b>{esc(brief['nouveaute'])}</b> — {esc(brief.get('contexte_nouveaute', ''))}")
    if brief.get("raison"):
        lines.append(f"\n💡 {esc(brief['raison'])}")
    srcs = [s for s in (brief.get("sources") or []) if isinstance(s, str)][:3]
    if srcs:
        lines += ["", "Sources: " + " · ".join(f"<a href=\"{s}\">lien</a>" for s in srcs)]

    msg = "\n".join(lines)[:4000]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=20,
        )
        if not r.ok:
            print(f"[telegram] erreur {r.status_code}: {r.text[:200]}")
        else:
            print(f"[telegram] envoyé ({url})")
    except Exception as e:
        print(f"[telegram] échec: {e}")

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
            brief = llm_brief(t, exa)
            send_telegram(t, brief)
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
    return process_new(tweets, load_json(SEEN_FILE, {}), notify=notify)

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
    elif mode == "daemon":
        print(f"[daemon] démarré — poll HTTP toutes les {POLL_INTERVAL}s")
        while True:
            try:
                poll_cycle(notify=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[daemon] erreur de cycle: {e}")
            time.sleep(POLL_INTERVAL)
    else:
        print(f"Usage: {sys.argv[0]} [login|refresh|baseline|once|daemon]")
        sys.exit(1)

if __name__ == "__main__":
    main()
