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

import base64 as base64_mod
import html as html_mod
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from camoufox.sync_api import Camoufox

BASE = Path(__file__).resolve().parent
PROFILE_DIR = str(BASE / "x-profile")
SESSION_FILE = BASE / "session.json"
SEEN_FILE = BASE / "seen.json"
CHASSE_SEEN_FILE = BASE / "chasse-seen.json"
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
OPENMAUSBOT_WEBHOOK_URL = CFG.get("OPENMAUSBOT_WEBHOOK_URL", "")
SIFTLY_URL = CFG.get("SIFTLY_URL", "http://localhost:3000")
PROXY_URL = CFG.get("PROXY_URL", "http://localhost:8089")
PROXY_API_KEY = CFG.get("PROXY_API_KEY", "optional-server-key-for-proxy-auth")
EXA_API_KEY = CFG.get("EXA_API_KEY", "")
AI_MODEL = CFG.get("AI_MODEL", "nvidia/nemotron-3-super-120b-a12b")
POLL_INTERVAL = int(CFG.get("POLL_INTERVAL", "45"))
MAX_PER_POLL = int(CFG.get("MAX_PER_POLL", "10"))
MAX_SCROLLS_REFRESH = int(CFG.get("MAX_SCROLLS_REFRESH", "2"))
CHASSE_INTERVAL = int(CFG.get("CHASSE_INTERVAL", "3600"))   # s entre 2 chasses (0 = désactivé)
CHASSE_MAX_PUSH = int(CFG.get("CHASSE_MAX_PUSH", "2"))      # max de trouvailles poussées par chasse
CHASSE_MIN_ENGAGEMENT = int(CFG.get("CHASSE_MIN_ENGAGEMENT", "15"))  # likes+rts min pour être candidat

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
    if path is SESSION_FILE:
        os.chmod(path, 0o600)   # contient une session X exploitable

# ---------------------------------------------------------------- session X

def _capture_bookmarks_request(headless: bool) -> dict:
    """Ouvre la page bookmarks dans camoufox, intercepte la requête GraphQL
    Bookmarks (URL + réponse) et les cookies. Capte aussi SearchTimeline
    (pour le mode chasse). Retourne la session complète."""
    captured: dict = {}

    def on_response(response):
        if "graphql" not in response.url:
            return
        try:
            if "/Bookmarks" in response.url:
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

def _tweets_from_instructions(instructions: list) -> list[dict]:
    """Extrait les tweets d'une liste d'instructions timeline X (partagé
    par Bookmarks et SearchTimeline — même structure d'entries)."""
    out = []

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
                links = []
                for u in legacy.get("entities", {}).get("urls", []) or []:
                    eu = u.get("expanded_url") or ""
                    if eu.startswith("http") and eu not in links:
                        links.append(eu)
                # contexte de thread : tweet cité (dans le payload) + parent de réponse (id)
                quoted = None
                qres = result.get("quoted_status_result", {}).get("result", {})
                if qres.get("__typename") == "TweetWithVisibilityResults":
                    qres = qres.get("tweet", {})
                qlegacy = qres.get("legacy", {})
                if qlegacy.get("full_text"):
                    qu = qres.get("core", {}).get("user_results", {}).get("result", {})
                    qhandle = (qu.get("core", {}).get("screen_name")
                               or qu.get("legacy", {}).get("screen_name") or "?")
                    quoted = {"handle": qhandle, "text": qlegacy.get("full_text", "").strip()}
                out.append({
                    "tweet_id": tweet_id,
                    "text": (note or legacy.get("full_text", "")).strip(),
                    "author_handle": handle,
                    "created_at": legacy.get("created_at", ""),
                    "images": [
                        m.get("media_url_https") for m in media
                        if m.get("type") == "photo" and m.get("media_url_https")
                    ],
                    "links": links,
                    # métriques d'engagement (utiles au mode chasse)
                    "likes": legacy.get("favorite_count", 0),
                    "rts": legacy.get("retweet_count", 0),
                    "replies": legacy.get("reply_count", 0),
                    # contexte fil/citations/pièces jointes
                    "reply_to": legacy.get("in_reply_to_status_id_str"),
                    "quoted": quoted,
                    "video_count": sum(1 for m in media if m.get("type") in ("video", "gif")),
                })
    return out

def parse_bookmarks_payload(payload: dict) -> list[dict]:
    """Extrait les tweets de la réponse GraphQL Bookmarks (formats v1/v2)."""
    try:
        instructions = (
            payload.get("data", {}).get("bookmark_timeline_v2", {})
            .get("timeline", {}).get("instructions", [])
        ) or payload.get("data", {}).get("bookmark_timeline", {}).get("timeline", {}).get("instructions", [])
        return _tweets_from_instructions(instructions)
    except Exception as e:
        print(f"[parse] erreur de parsing GraphQL: {e}")
    return []

def parse_search_payload(payload: dict) -> list[dict]:
    """Extrait les tweets d'une réponse GraphQL SearchTimeline."""
    try:
        instructions = (
            payload.get("data", {}).get("search_by_raw_query", {})
            .get("search_timeline", {}).get("timeline", {}).get("instructions", [])
        )
        return _tweets_from_instructions(instructions)
    except Exception as e:
        print(f"[parse] erreur de parsing search: {e}")
    return []

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

def fetch_link(url: str, max_chars: int = 3500) -> dict | None:
    """Récupère le contenu réel du lien partagé (article, repo, page).
    Retourne {url, title, text} ou None si inaccessible/non-textuel."""
    if not url:
        return None
    try:
        r = requests.get(
            url, timeout=15, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
                     "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                     "Accept-Language": "fr,en;q=0.8"},
        )
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.status_code != 200 or ("html" not in ctype and "text" not in ctype and "xml" not in ctype):
            print(f"[fetch] {url[:80]} -> HTTP {r.status_code} ({ctype[:40] or 'sans type'}) : ignoré")
            return None
        # text/* sans charset déclaré : requests part sur ISO-8859-1 → mojibake
        r.encoding = r.apparent_encoding or "utf-8"
        html_src = r.text
        m = re.search(r"<title[^>]*>(.*?)</title>", html_src, re.S | re.I)
        title = re.sub(r"\s+", " ", html_mod.unescape(m.group(1))).strip() if m else ""
        # vire scripts/styles/comments, puis les balises, puis compacte
        html_src = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ", html_src)
        html_src = re.sub(r"(?s)<!--.*?-->", " ", html_src)
        text = re.sub(r"(?s)<[^>]+>", " ", html_src)
        text = re.sub(r"\s+", " ", html_mod.unescape(text)).strip()
        if len(text) < 200:
            print(f"[fetch] {url[:80]} : page quasi vide ({len(text)} chars) — ignoré")
            return None
        print(f"[fetch] OK {url[:80]} ({len(text)} chars, titre: {title[:60]})")
        return {"url": r.url, "title": title, "text": text[:max_chars]}
    except Exception as e:
        print(f"[fetch] échec {url[:80]} : {e}")
        return None

def fetch_parent_tweet(tweet_id: str) -> dict | None:
    """Tweet parent d'une réponse, via l'API syndication publique (sans login).
    Token dérivé de l'id (formule du widget X : (id/1e15)*pi en base36)."""
    if not tweet_id:
        return None
    try:
        import math
        def b36(n: int) -> str:
            a = "0123456789abcdefghijklmnopqrstuvwxyz"
            s = ""
            while n:
                s = a[n % 36] + s
                n //= 36
            return s or "0"
        v = (int(tweet_id) / 1e15) * math.pi
        int_part, frac = str(v).split(".")
        f = float("0." + frac)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        fs = ""
        for _ in range(20):
            f *= 36
            d = int(f)
            fs += digits[d]
            f -= d
        token = (b36(int(int_part)) + "." + fs).replace("0", "").replace(".", "")
        r = requests.get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token={token}&lang=fr",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"},
        )
        if not r.ok:
            print(f"[parent] syndication {r.status_code} pour {tweet_id}")
            return None
        d = r.json()
        return {
            "handle": (d.get("user", {}) or {}).get("screen_name", "?"),
            "text": (d.get("text") or "").strip(),
        }
    except Exception as e:
        print(f"[parent] échec {tweet_id}: {e}")
        return None

VISION_MODELS = ["meta/llama-3.2-11b-vision-instruct", "meta/llama-3.2-90b-vision-instruct"]

def analyze_image(image_url: str) -> str | None:
    """Description d'une image jointe via le modèle vision NIM (à travers le proxy).
    None si indisponible — l'appelant annotera le média comme non analysé."""
    if not image_url:
        return None
    try:
        img = requests.get(image_url, timeout=20)
        if img.status_code != 200 or not img.content:
            return None
        b64 = base64_mod.standard_b64encode(img.content).decode()
        for model in VISION_MODELS:
            try:
                r = requests.post(
                    f"{PROXY_URL}/v1/messages",
                    headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "max_tokens": 400,
                          "messages": [{"role": "user", "content": [
                              {"type": "image", "source": {"type": "base64",
                                                           "media_type": img.headers.get("Content-Type", "image/jpeg").split(";")[0],
                                                           "data": b64}},
                              {"type": "text", "text": "Décris factuellement cette image (texte visible, code, graphique, UI) en 3 phrases max. C'est la pièce jointe d'un tweet analysé."}]}]},
                    timeout=120,
                )
                if r.ok:
                    text = " ".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
                    if text:
                        return text
            except Exception:
                continue
    except Exception as e:
        print(f"[vision] échec {image_url[:60]}: {e}")
    return None

def research_questions(tweet: dict, link: dict | None, parent: dict | None) -> list[str]:
    """Le LLM formule 2-3 questions de vérification précises auxquelles le tweet
    + la source ne répondent pas (doutes, claims à confirmer, limites pressenties)."""
    user_msg = (
        f"TWEET de @{tweet['author_handle'] or '?'} :\n\"\"\"{tweet['text'][:800]}\"\"\"\n\n"
        + (f"Source principale lue : « {link['title']} » — début : {link['text'][:600]}\n\n" if link else "Pas de source lue.\n\n")
        + (f"C'est une réponse au tweet de @{parent['handle']} : « {parent['text'][:300]} »\n\n" if parent else "")
        + "Formule 2 à 3 questions de vérification PRÉCISES et recherchables sur le web auxquelles "
        "ce tweet ne répond pas : claims chiffrés à confirmer, comparaison avec l'existant, "
        "doutes techniques, ce qui pourrait être exagéré. Ces questions vont servir à chercher sur le web.\n"
        'Réponds UNIQUEMENT en JSON : {"questions": ["q1", "q2", "q3"]}'
    )
    txt = _llm_text(user_msg, max_tokens=700)
    m = _extract_json_object(txt)
    if not m:
        return []
    try:
        qs = json.loads(m).get("questions", [])
    except json.JSONDecodeError:
        return []
    return [str(q).strip()[:150] for q in qs if str(q).strip()][:3]

def research_evidence(questions: list[str]) -> list[dict]:
    """Pour chaque question de vérification : une recherche web ciblée avec extraits."""
    out = []
    for q in questions:
        res = exa_search(q, num_results=3, with_text=True)
        for r in res:
            r["question"] = q
        out.extend(res)
        time.sleep(1)
    return out

def _extract_json_object(text: str) -> str | None:
    """Premier objet JSON équilibré du texte (tolère le raisonnement autour).
    String-aware : les accolades entre guillemets ne comptent pas."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None

def llm_brief(tweet: dict, exa_results: list[dict], recent: list[dict] | None = None,
              link: dict | None = None, parent: dict | None = None, quoted: dict | None = None,
              media_desc: str | None = None, extra_evidence: list[dict] | None = None) -> dict:
    evidence = "\n".join(
        f"- {r['title']} ({r['date'] or 'date inconnue'}) {r['url']}" for r in exa_results
    ) or "(aucun résultat web)"
    deep = ""
    if extra_evidence:
        lines = []
        for r in extra_evidence[:9]:
            lines.append(f"- (pour « {r.get('question','')} ») {r['title']} {r['url']}" +
                         (f" : {r['text'][:200]}" if r.get("text") else ""))
        deep = ("RECHERCHE APPROFONDIE (réponses trouvées aux questions de vérification) :\n"
                + "\n".join(lines) + "\n\n")
    link_section = ""
    link_instruction = ""
    if link:
        link_section = (f"CONTENU RÉEL DU LIEN PARTAGÉ (fetched, source primaire) — « {link['title']} » :\n"
                        f"\"\"\"{link['text']}\"\"\"\n\n"
                        "VERIFICATION OBLIGATOIRE : confronte ce contenu aux claims du tweet. Champ "
                        f"\"verification\" : \"ok\" (le lien tient les claims) | \"decalage\" (le lien dit "
                        "autre chose / moins que le tweet) | \"inaccessible\" (vide ou hors-sujet). "
                        "Plus champ \"verification_note\": 1 phrase factuelle appuyée sur le CONTENU (pas le tweet).\n\n")
    else:
        link_instruction = ('Pas de contenu de lien récupérable : champ "verification": "inaccessible", '
                            '"verification_note": raison courte. Ne présente JAMAIS les résultats Exa comme si tu avais lu la source.\n\n')
    thread_section = ""
    if parent:
        thread_section += (f"CONTEXTE : ce tweet est une RÉPONSE au tweet de @{parent['handle']} :\n"
                           f"\"\"\"{parent['text'][:500]}\"\"\"\n"
                           "Évalue le tweet PAR RAPPORT à ce parent (est-il pertinent, apporte-t-il quelque chose ?).\n\n")
    if quoted:
        thread_section += (f"CONTEXTE : ce tweet CITE le tweet de @{quoted['handle']} :\n"
                           f"\"\"\"{quoted['text'][:500]}\"\"\"\n"
                           "Le commentaire ajouté par rapport au tweet cité est-il substantiel ?\n\n")
    media_section = ""
    if media_desc:
        media_section = (f"PIÈCE JOINTE IMAGE (analysée par vision) :\n\"\"\"{media_desc[:600]}\"\"\"\n"
                         "Intègre ce qu'elle montre dans l'analyse.\n\n")
    elif tweet.get("images") or tweet.get("video_count"):
        what = []
        if tweet.get("images"):
            what.append(f"{len(tweet['images'])} image(s)")
        if tweet.get("video_count"):
            what.append(f"{tweet['video_count']} vidéo(s)")
        media_section = (f"⚠️ Le tweet a des pièces jointes ({', '.join(what)}) NON analysables : "
                         "si un claim repose sur leur contenu, note-le explicitement dans \"limites\".\n\n")
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

{thread_section}{media_section}{link_section}{deep}RÉSULTATS WEB EXA (contexte : fraîcheur, écho éventuel) :
{evidence}

{link_instruction}{already}Analyse selon le système. Réponds UNIQUEMENT en JSON."""
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
            brief = json.loads(m)
            # validation du verdict : coerce vers l'échelle connue, sinon MOYEN
            verdict = str(brief.get("verdict", "")).strip().upper().replace(" ", "_")
            if verdict not in VERDICT_LABEL:
                verdict = "GAME_CHANGER" if "GAME" in verdict else "MOYEN"
            # verification : valeur imposée si le modèle n'a pas coopéré
            verif = str(brief.get("verification", "")).strip().lower()
            if verif not in ("ok", "decalage", "inaccessible"):
                brief["verification"] = "inaccessible"
                brief.setdefault("verification_note", "vérification indisponible")
            brief["verdict"] = verdict
            return brief
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

def send_telegram(tweet: dict, brief: dict, exa_results: list[dict] | None = None, label: str = "Bookmark") -> int | None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] token/chat_id manquants — message non envoyé")
        return None
    author = tweet["author_handle"] or "x"
    url = f"https://x.com/{author}/status/{tweet['tweet_id']}"
    verdict = VERDICT_LABEL.get(brief.get("verdict", ""), "❓ " + str(brief.get("verdict", "?")))
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    excerpt = esc(tweet["text"][:280])

    lines = [f"{label} <a href=\"{url}\">— @{author}</a>", ""]
    if excerpt:
        lines += [f"<i>« {excerpt}… »</i>", ""]
    lines.append(f"<b>{verdict}</b>")

    verif = str(brief.get("verification", "")).lower()
    vnote = esc(str(brief.get("verification_note", "")).strip())
    VERIF_BADGE = {"ok": "✅ Source vérifiée", "decalage": "⚠️ Décalage tweet/source",
                   "inaccessible": "❓ Source non vérifiée"}
    if verif in VERIF_BADGE:
        lines.append(f"\n{VERIF_BADGE[verif]}" + (f" — {vnote}" if vnote else ""))

    dj = brief.get("dejavu")
    if isinstance(dj, dict) and dj.get("ref"):
        lines.append(f"\n🍲 <b>Déjà vu</b> — {esc(dj.get('ref', ''))} : {esc(dj.get('note', ''))}")

    resume = _bullets(brief.get("resume"))
    if resume:
        lines.append("\n<b>🎯 C'est quoi</b>")
        lines += [esc(b) for b in resume]
    angle = str(brief.get("angle") or "").strip()
    if angle:
        lines.append(f"\n<i>{esc(angle)}</i>")
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

WEBHOOK_TASK = """Voici un bookmark X que j'ai analyse. Utilise le skill `bookmark-watcher` \
pour le ton, le format et les sources de verite.

Ce que j'attends de ta reponse, en francais, 4 a 8 lignes :
1. Un commentaire direct sur le fond, comme un pote qui vient de lire le truc. \
Pas un resume du payload.
2. UN detail que je n'ai pas dans le resume : ce qui est verifie, ce qui ne l'est \
pas, ou ce que ca change concretement pour moi.
3. Une fin qui propose quelque chose de concret : une comparaison, un test a lancer, \
un angle a creuser.

Si le verdict est MOYEN ou BRUIT, dis-le cash. Ne gonfle jamais le verdict pour etre \
gentil. Jamais de "a surveiller" ni de "ca vaut le coup d'oeil".

Le brief :
---
{payload}
---"""


def push_to_openmausbot(tweet: dict, brief: dict | None, label: str = "Bookmark") -> bool:
    """Pousse un brief vers OpenMausBot via webhook ingress.

    Le champ `task` est ce que l'app lit (`parsed.data.task ?? parsed.data.message`) :
    c'est l'instruction du tour. Le champ `text` n'est JAMAIS lu et tombait sur le
    prompt par defaut de l'app, « Review the incoming event and summarize ».
    No-op si OPENMAUSBOT_WEBHOOK_URL n'est pas configurée. Ne lève jamais :
    le canal Telegram reste prioritaire et indépendant."""
    if not OPENMAUSBOT_WEBHOOK_URL:
        return False
    if not brief:
        return False
    try:
        author = tweet.get("author_handle") or "x"
        url = f"https://x.com/{author}/status/{tweet.get('tweet_id')}"
        verdict = VERDICT_LABEL.get(brief.get("verdict", ""), str(brief.get("verdict", "?")))
        excerpt = (tweet.get("text") or "")[:280]

        lines = [f"## {label} — @{author}", "", f"[Voir le tweet]({url})", ""]
        if excerpt:
            lines += [f"> {excerpt}…", ""]
        lines.append(f"**{verdict}**")
        verif = str(brief.get("verification", "")).lower()
        VERIF_BADGE_MD = {"ok": "✅ Source vérifiée", "decalage": "⚠️ Décalage tweet/source",
                          "inaccessible": "❓ Source non vérifiée"}
        vnote = str(brief.get("verification_note", "")).strip()
        if verif in VERIF_BADGE_MD:
            lines.append(f"\n{VERIF_BADGE_MD[verif]}" + (f" — {vnote}" if vnote else ""))
        dj = brief.get("dejavu")
        if isinstance(dj, dict) and dj.get("ref"):
            lines.append(f"\n🍲 **Déjà vu** — {dj.get('ref', '')} : {dj.get('note', '')}")
        resume = _bullets(brief.get("resume"))
        if resume:
            lines.append("\n**🎯 C'est quoi**")
            lines += [f"- {b.lstrip(' •')}" for b in resume]
        angle = str(brief.get("angle") or "").strip()
        if angle:
            lines.append(f"\n_{angle}_")
        if brief.get("nouveaute"):
            lines.append(f"\n🆕 **{brief['nouveaute']}** — {brief.get('contexte_nouveaute', '')}")
        raison = _bullets(brief.get("raison"))
        if raison:
            lines.append("\n**💡 Ce qu'elle vaut**")
            lines += [f"- {b.lstrip(' •')}" for b in raison]
        limites = _bullets(brief.get("limites"))
        if limites:
            lines.append("\n**⚠️ Limites**")
            lines += [f"- {b.lstrip(' •')}" for b in limites]
        srcs = [s for s in (brief.get("sources") or []) if isinstance(s, str)][:3]
        if srcs:
            lines += ["", "Sources : " + " · ".join(f"[lien]({s})" for s in srcs)]
        suggestions = [str(s).strip() for s in (brief.get("suggestions") or []) if str(s).strip()][:3]
        if suggestions:
            lines.append("\n**👉 Pour creuser**")
            lines += [f"- {s}" for s in suggestions]

        msg = "\n".join(lines)[:15000]
        task = WEBHOOK_TASK.format(payload=msg)
        r = requests.post(OPENMAUSBOT_WEBHOOK_URL, json={"task": task, "text": msg}, timeout=20)
        if not r.ok:
            print(f"[openmausbot] erreur {r.status_code}: {r.text[:200]}")
            return False
        print(f"[openmausbot] brief poussé ({url})")
        return True
    except Exception as e:
        print(f"[openmausbot] échec: {e}")
        return False

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
        "Ne demande JAMAIS à l'utilisateur de te donner un lien ou une source — cherche et réponds.\n"
        "FORMAT DE SORTIE OBLIGATOIRE : d'abord ta réflexion interne (ce que tu veux), puis une ligne "
        "contenant exactement ---RÉPONSE--- , puis TA RÉPONSE FINALE pour l'utilisateur. "
        "Seule la partie après ---RÉPONSE--- sera envoyée : jamais de méta-commentaire "
        "('the user wants', 'let's tackle', 'hmm'), jamais d'anglais dans la réponse finale."
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
            if not text:
                raise ValueError("réponse vide")
            # le modèle raisonne dans le texte : on ne garde que ce qui suit le marqueur
            if "---RÉPONSE---" in text:
                text = text.split("---RÉPONSE---", 1)[1].strip()
            return text
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
    # deep dive : on lit aussi la source primaire du tweet concerné, pas que les résultats Exa
    if records and question.startswith(("Deep dive", "Fais le parallèle")):
        src_url = ""
        for s in (records[0].get("sources") or []):
            src_url = s.get("url", "") if isinstance(s, dict) else s
            if src_url:
                break
        if not src_url and records[0].get("links"):
            src_url = records[0]["links"][0]
        if src_url:
            page = fetch_link(src_url, max_chars=5000)
            if page:
                web = [{"title": f"SOURCE PRIMAIRE du tweet : {page['title']}", "url": page["url"],
                        "date": "", "text": page["text"]}] + web
    answer = llm_answer(q_full, records, web)
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(s))
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": chat_id, "text": html(answer)[:4000], "parse_mode": "HTML"},
                  timeout=30)
    print(f"[ask] réponse envoyée ({len(answer)} chars)")

TG_OFFSET_FILE = BASE / "tg-offset.txt"

def telegram_listener() -> None:
    """Long polling getUpdates — répond aux questions sur les bookmarks."""
    load_context_index()
    offset = 0
    if TG_OFFSET_FILE.exists():
        try:
            offset = int(TG_OFFSET_FILE.read_text().strip())
        except ValueError:
            offset = 0
    print(f"[listener] Telegram en écoute (offset {offset})")
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
                TG_OFFSET_FILE.write_text(str(offset))
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

_HEALTH = {"poll": 0, "brief": 0, "siftly": 0, "obsidian": 0, "chasse": 0}
_ALERTED: set[str] = set()
HEALTH_THRESHOLD = 3
HEALTH_LABEL = {
    "poll": "le polling X plante en boucle (session ou réseau ?)",
    "brief": "les briefs LLM échouent (proxy/NIM down ?)",
    "siftly": "l'import vers Siftly échoue",
    "obsidian": "l'export Obsidian échoue",
    "chasse": "les chasses X échouent (SearchTimeline refusée ? relance `refresh`)",
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

# ---------------------------------------------------------------- chasse (veille proactive X)

CHASSE_FALLBACK_QUERIES = [
    "AI agents open source min_faves:50",
    "cybersecurity 0day OR exploit lang:en min_faves:30",
    "local LLM tools min_faves:30",
    "pentest OR redteam tool lang:en min_faves:20",
    "devtools CLI min_faves:40",
]

def _chromium_executable() -> str | None:
    """Chromium Playwright installé (version la plus récente)."""
    import glob
    cands = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome")),
                   reverse=True)
    return cands[0] if cands else None

def chasse_collect(queries: list[str]) -> tuple[list[dict], str | None]:
    """Exécute les recherches X dans un Chromium Playwright (cookies du compte)
    et intercepte les réponses SearchTimeline 200.
    ⚠️ Pourquoi PAS camoufox ni HTTP pur :
      - HTTP pur : X répond 404 corps vide au replay de SearchTimeline (headers
        capturés d'une vraie réponse 200 compris) — enforcement transaction-id ;
      - camoufox : son anti-fingerprint produit des transaction-ids invalidés
        par X (404 aussi, même en vrai navigateur) alors que Chrome passe.
    Retourne (tweets dédupliqués, erreur)."""
    from urllib.parse import quote
    from playwright.sync_api import sync_playwright
    session = get_session()
    if not session.get("cookies"):
        return [], "pas de session X — `python watcher.py login` puis `refresh`"
    cookies = [{"name": k, "value": v, "domain": ".x.com", "path": "/"}
               for k, v in session["cookies"].items()]
    exe = _chromium_executable()
    if not exe:
        return [], "chromium playwright absent — `playwright install chromium`"

    payloads: list[dict] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, executable_path=exe, args=["--no-sandbox"])
            ctx = b.new_context(locale="fr-FR")
            ctx.add_cookies(cookies)
            page = ctx.new_page()

            def on_response(response):
                if "/SearchTimeline" in response.url and response.status == 200:
                    try:
                        payloads.append(response.json())
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(f"https://x.com/search?q={quote(queries[0])}&f=live",
                      wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(8000)
            if "/login" in page.url:
                b.close()
                return [], "cookies X expirés — `python watcher.py login` puis `refresh`"
            for q in queries[1:]:
                try:
                    page.goto(f"https://x.com/search?q={quote(q)}&f=live",
                              wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(8000)
                except Exception as e:
                    # une recherche lente ne doit pas tuer les suivantes
                    print(f"[chasse] recherche lente ignorée ({q[:40]!r}: {type(e).__name__})")
            b.close()
    except Exception as e:
        if payloads:
            print(f"[chasse] navigateur interrompu en cours ({e}) — résultats partiels")
        else:
            return [], f"navigateur: {e}"
    seen_ids: set[str] = set()
    tweets = []
    for payload in payloads:
        for t in parse_search_payload(payload):
            if t["tweet_id"] not in seen_ids:
                seen_ids.add(t["tweet_id"])
                tweets.append(t)
    return tweets, None

def chasse_queries() -> list[str]:
    """5 requêtes de recherche générées par le LLM depuis la mémoire + sujets récents."""
    recent = "\n".join(f"- @{r['author']}: {r['text'][:80]}" for r in recent_context(10))
    txt = _llm_text(
        "Génère 5 requêtes de recherche X (Twitter) pour trouver des tweets des dernières 48 h "
        "que cet utilisateur risque d'avoir ratés et qui l'intéresseraient à coup sûr.\n"
        f"Ses sujets récents :\n{recent}\n\n"
        "Règles : une requête par ligne, sans numérotation ni guillemets, plutôt en anglais, "
        "opérateurs X autorisés (min_faves:20, OR, lang:en), précises et variées (pas 5 fois la même idée).\n"
        "FORMAT OBLIGATOIRE : d'abord ta réflexion, puis une ligne contenant exactement ---RÉPONSE--- , "
        "puis les 5 requêtes (une par ligne, RIEN d'autre après).",
        max_tokens=800,
    )
    if "---RÉPONSE---" in txt:
        txt = txt.split("---RÉPONSE---", 1)[1]
    # une requête X : ligne courte sans ponctuation de phrase ni bavardage ;
    # nemotron numérote souvent ("1. ...") sans mettre le marqueur → strip
    BAVARD = ("voici", "here", "note", "the ", "these", "ces ", "requêt", "quer",
              "we need", "we must", "we'll", "let's", "make sure", "add operators", "ensure")
    queries = []
    for l in txt.splitlines():
        q = re.sub(r"^\d+\s*[.)]\s*", "", l.strip())
        q = q.strip("-•* ").strip('"').strip("'")
        if (not q or q.endswith((".", "!", "?")) or "---" in q
                or len(q) > 110 or q.lower().startswith(BAVARD)):
            continue
        queries.append(q)
    if len(queries) >= 3:
        return queries[:5]
    print("[chasse] génération LLM insuffisante — requêtes de secours")
    return CHASSE_FALLBACK_QUERIES

def chasse_curation(candidates: list[dict]) -> list[dict]:
    """Le LLM choisit 0 à CHASSE_MAX_PUSH trouvailles vraiment exceptionnelles."""
    lines = []
    for i, t in enumerate(candidates[:40]):
        lines.append(f"[{i}] @{t['author_handle']} ❤️{t['likes']} 🔁{t['rts']} — {t['text'][:200]}")
    txt = _llm_text(
        "Voici des tweets trouvés par recherche X, tous des dernières 48 h. L'utilisateur est expert "
        "cybersécurité (ex-ANSSI), dev, full-automatisation, IA locale. Il n'a PAS vu ces tweets.\n\n"
        + "\n".join(lines)
        + f"\n\nSélectionne au MAXIMUM {CHASSE_MAX_PUSH} tweets VRAIMENT exceptionnels pour lui : "
        "outil révolutionnaire, vulnérabilité majeure, révélation qui va faire du bruit dans son domaine. "
        "Zéro hype creuse, zéro recycle. Si rien ne mérite, sélectionne RIEN — c'est une réponse valide.\n"
        'Réponds UNIQUEMENT en JSON : {"picks": [{"idx": <numéro>, "pourquoi": "1 phrase"}]}',
        max_tokens=600,
    )
    m = _extract_json_object(txt)
    if not m:
        return []
    try:
        picks = json.loads(m).get("picks", [])
    except json.JSONDecodeError:
        return []
    out = []
    for p in picks:
        try:
            idx = int(p.get("idx"))
            t = candidates[idx]
            out.append({**t, "pourquoi": str(p.get("pourquoi", ""))[:200]})
        except (ValueError, IndexError, KeyError):
            continue
    return out[:CHASSE_MAX_PUSH]

def mode_chasse(dry_run: bool = False) -> int:
    """Cherche sur X ce que l'utilisateur a raté, filtre fort, pousse les pépites.
    Retourne le nombre de trouvailles poussées (ou -1 en échec dur)."""
    queries = chasse_queries()
    print(f"[chasse] requêtes: {queries}")
    seen = load_json(CHASSE_SEEN_FILE, {})
    bookmarked = load_json(SEEN_FILE, {})   # ne jamais re-proposer ce qu'il a déjà bookmarké
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    tweets, err = chasse_collect(queries)
    if err:
        print(f"[chasse] {err}")
        return -1
    candidates: dict[str, dict] = {}
    for t in tweets:
        tid = t["tweet_id"]
        if tid in seen or tid in bookmarked:
            continue
        try:
            if datetime.strptime(t["created_at"], "%a %b %d %H:%M:%S %z %Y") < cutoff:
                continue
        except ValueError:
            pass
        if (t.get("likes", 0) + t.get("rts", 0)) < CHASSE_MIN_ENGAGEMENT:
            continue
        if not t["text"]:
            continue
        candidates[tid] = t
    print(f"[chasse] {len(tweets)} tweets vus, {len(candidates)} candidats (48 h, engagement ≥ {CHASSE_MIN_ENGAGEMENT})")
    if not candidates:
        print("[chasse] rien de nouveau")
        return 0
    if dry_run:
        for t in list(candidates.values())[:15]:
            print(f"  [cand] @{t['author_handle']} ❤️{t['likes']} — {t['text'][:90]}")

    print(f"[chasse] {len(candidates)} candidats → curation")
    picks = chasse_curation(list(candidates.values()))
    # tout le pool examiné est marqué vu : pas de re-curation à chaque cycle
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        seen.update({tid: now_iso for tid in candidates})
        save_json(CHASSE_SEEN_FILE, seen)

    pushed = 0
    for p in picks:
        print(f"[chasse] pépite: @{p['author_handle']} — {p['pourquoi']}")
        if dry_run:
            continue
        brief = deep_brief(p, recent=recent_context(15))
        health("brief", bool(brief))
        if brief:
            send_telegram(p, brief, label="📡 T'as loupé ça")
            push_to_openmausbot(p, brief, label="📡 T'as loupé ça")
            pushed += 1
        time.sleep(2)
    print(f"[chasse] terminé — {pushed} poussée(s) sur {len(candidates)} candidats")
    return pushed

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
    # vault configuré dans Siftly (Settings) sinon fallback sibling
    vault = None
    try:
        r = requests.get(f"{SIFTLY_URL}/api/settings", timeout=10)
        path = r.json().get("obsidianVaultPath")
        if path:
            vault = Path(path)
    except Exception:
        pass
    if not vault or not vault.exists():
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
    """Transcrit un vocal : Groq whisper d'abord, faster-whisper local en secours."""
    groq_key = CFG.get("GROQ_API_KEY", "")
    if groq_key:
        try:
            with open(ogg_path, "rb") as f:
                r = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    files={"file": (os.path.basename(ogg_path), f, "audio/ogg")},
                    data={"model": "whisper-large-v3-turbo", "language": "fr"},
                    timeout=60,
                )
            if r.ok and r.json().get("text", "").strip():
                return r.json()["text"].strip()
            print(f"[voice] groq HTTP {r.status_code}, bascule local")
        except Exception as e:
            print(f"[voice] groq échoué ({e}), bascule local")
    global _WHISPER
    if _WHISPER is None:
        print("[voice] chargement du modèle whisper (première fois)…")
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel("base", device="cpu", compute_type="int8")
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

def deep_brief(tweet: dict, recent: list[dict] | None = None) -> dict | None:
    """Pipeline complet d'analyse : lien + parent/cité + image + boucle de
    recherche par questions + brief. Retourne le brief enrichi (sources y
    sont ajoutées). None si le LLM a échoué."""
    link = fetch_link(tweet["links"][0]) if tweet.get("links") else None
    parent = fetch_parent_tweet(tweet["reply_to"]) if tweet.get("reply_to") else None
    quoted = tweet.get("quoted")
    media_desc = analyze_image(tweet["images"][0]) if tweet.get("images") else None

    # boucle itérative : le LLM pose ses doutes, on va chercher les réponses
    questions = research_questions(tweet, link, parent)
    extra = research_evidence(questions) if questions else []
    if questions:
        print(f"[deep] {len(questions)} question(s) cherchée(s): {questions[:2]}")

    brief = llm_brief(tweet, exa_search(tweet["text"], with_text=True),
                      recent=recent, link=link, parent=parent, quoted=quoted,
                      media_desc=media_desc, extra_evidence=extra)
    if brief and link:
        brief.setdefault("sources", [])
        if link["url"] not in brief["sources"]:
            brief["sources"].insert(0, link["url"])
    return brief

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
            brief = deep_brief(t, recent=recent_context(15))
            health("brief", bool(brief))
            send_telegram(t, brief)
            push_to_openmausbot(t, brief)
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
            return -1   # échec réel : le daemon déclenchera health(poll, False)
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
    elif mode == "chasse":
        n = mode_chasse()
        print(f"[chasse] {n} trouvaille(s) poussée(s)")
    elif mode == "chasse-dry":
        n = mode_chasse(dry_run=True)
        print(f"[chasse-dry] {n} trouvaille(s) sélectionnée(s) (rien envoyé)")
    elif mode == "digest":
        mode_digest()
    elif mode == "morning":
        mode_morning()
    elif mode == "daemon":
        import threading
        print(f"[daemon] démarré — poll HTTP toutes les {POLL_INTERVAL}s"
              + (f", chasse toutes les {CHASSE_INTERVAL}s" if CHASSE_INTERVAL else ", chasse désactivée"))
        threading.Thread(target=telegram_listener, daemon=True).start()
        pending_export = False
        # première chasse ~5 min après le boot (les redémarrages en rafale ne spamment pas)
        last_chasse = time.time() - CHASSE_INTERVAL + 300 if CHASSE_INTERVAL else 0
        while True:
            try:
                n = poll_cycle(notify=True)
                health("poll", n >= 0)
                if n < 0:
                    n = 0
                if pending_export:
                    # rattrape les tags/catégories calculés depuis l'export précédent
                    export_obsidian()
                    pending_export = False
                if n > 0:
                    pending_export = True
                if CHASSE_INTERVAL and time.time() - last_chasse >= CHASSE_INTERVAL:
                    last_chasse = time.time()
                    try:
                        health("chasse", mode_chasse() >= 0)
                    except Exception as e:
                        print(f"[daemon] erreur chasse: {e}")
                        health("chasse", False)
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
