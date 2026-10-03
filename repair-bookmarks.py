#!/usr/bin/env python3
"""
repair-bookmarks.py — Complète le texte tronqué des bookmarks Obsidian.

Utilise l'API syndication publique de X (aucun login requis).
Pour chaque fichier Bookmarks/*.md, extrait le tweet_id depuis le frontmatter,
fetch le tweet complet et remplace la section ## Original si le texte est plus long.

Usage:
    python repair-bookmarks.py [--dry-run] [--vault /path/to/vault]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------- config

VAULT_PATH = Path("/home/j0wlt/Téléchargements/Tweeter repo")
BOOKMARKS_DIR = VAULT_PATH / "Bookmarks"

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"

REQUEST_TIMEOUT = 15
RATE_LIMIT_DELAY = 0.5  # secondes entre requêtes
MAX_RETRIES = 3


# ---------------------------------------------------------------- syndication API

def syndication_token(tweet_id: str) -> str:
    """Calcule le token syndication depuis le tweet_id (formule du widget X)."""
    v = (int(tweet_id) / 1e15) * math.pi
    int_part, frac = str(v).split(".")
    f = float("0." + frac)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    def b36(n: int) -> str:
        s = ""
        while n:
            s = digits[n % 36] + s
            n //= 36
        return s or "0"

    fs = ""
    for _ in range(20):
        f *= 36
        d = int(f)
        fs += digits[d]
        f -= d

    token = (b36(int(int_part)) + "." + fs).replace("0", "").replace(".", "")
    return token


def fetch_tweet_text(tweet_id: str) -> str | None:
    """Fetch le texte complet d'un tweet via l'API syndication publique.
    Retourne le texte complet, ou None en cas d'échec."""
    token = syndication_token(tweet_id)
    url = f"{SYNDICATION_URL}?id={tweet_id}&token={token}&lang=fr"

    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
            })
            if r.status_code == 200:
                data = r.json()
                # L'API syndication renvoie le texte dans "text"
                text = data.get("text", "")
                if text:
                    return text.strip()
                return None
            elif r.status_code == 404:
                return None  # tweet supprimé ou inaccessible
            elif r.status_code == 429:
                time.sleep(2 ** attempt)  # backoff exponentiel
                continue
            else:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1)
                    continue
                return None
        except requests.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep(1)
                continue
            return None
    return None


# ---------------------------------------------------------------- bookmark parsing

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
TWEET_ID_RE = re.compile(r'tweet_id:\s*"?(\d+)"?')


def extract_tweet_id(content: str) -> str | None:
    """Extrait le tweet_id depuis le frontmatter YAML."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    tm = TWEET_ID_RE.search(m.group(1))
    return tm.group(1) if tm else None


def extract_original_section(content: str) -> tuple[str, str, str] | None:
    """Extrait la section ## Original. Retourne (avant, texte_original, après)."""
    # trouve "## Original" suivi de lignes > (blockquote) jusqu'à ## ou fin
    match = re.search(
        r"(## Original\n)\n((?:>.*\n?)+)\n((?:\n## |\n\[|\Z))",
        content,
    )
    if not match:
        return None
    before = content[: match.start()]
    original_block = match.group(2)
    after = content[match.start(3) :]
    return before, original_block, after


def strip_blockquote(text: str) -> str:
    """Enlève le préfixe '> ' des lignes."""
    lines = text.strip().split("\n")
    return "\n".join(re.sub(r"^>\s?", "", line) for line in lines)


def to_blockquote(text: str) -> str:
    """Convertit du texte en blockquote."""
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


# ---------------------------------------------------------------- main

def repair_bookmark(file_path: Path, dry_run: bool = False) -> dict:
    """Répare un fichier bookmark. Retourne un rapport."""
    content = file_path.read_text(encoding="utf-8")
    tweet_id = extract_tweet_id(content)

    if not tweet_id:
        return {"status": "skip", "reason": "pas de tweet_id", "file": file_path.name}

    section = extract_original_section(content)
    if not section:
        return {"status": "skip", "reason": "pas de section Original", "file": file_path.name}

    before, original_block, after = section
    current_text = strip_blockquote(original_block)

    # Fetch le texte complet
    full_text = fetch_tweet_text(tweet_id)
    if full_text is None:
        return {"status": "error", "reason": "fetch échoué ou tweet supprimé", "file": file_path.name}

    # Compare
    if len(full_text) <= len(current_text):
        return {"status": "ok", "reason": "déjà complet", "file": file_path.name}

    if dry_run:
        return {
            "status": "would_fix",
            "file": file_path.name,
            "old_len": len(current_text),
            "new_len": len(full_text),
        }

    # Reconstruit le fichier
    new_block = to_blockquote(full_text)
    new_content = before + "## Original\n\n" + new_block + "\n" + after
    file_path.write_text(new_content, encoding="utf-8")

    return {
        "status": "fixed",
        "file": file_path.name,
        "old_len": len(current_text),
        "new_len": len(full_text),
    }


def main():
    parser = argparse.ArgumentParser(description="Répare les bookmarks Obsidian avec le texte complet")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans écrire")
    parser.add_argument("--vault", type=str, default=str(VAULT_PATH), help="Chemin du vault Obsidian")
    args = parser.parse_args()

    bookmarks_dir = Path(args.vault) / "Bookmarks"
    if not bookmarks_dir.is_dir():
        print(f"ERREUR: {bookmarks_dir} n'existe pas")
        sys.exit(1)

    files = sorted(bookmarks_dir.glob("*.md"))
    total = len(files)
    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Réparation de {total} bookmarks dans {bookmarks_dir}\n")

    results = {"fixed": 0, "ok": 0, "error": 0, "skip": 0, "would_fix": 0}
    errors = []

    for i, f in enumerate(files, 1):
        result = repair_bookmark(f, dry_run=args.dry_run)
        results[result["status"]] = results.get(result["status"], 0) + 1

        if result["status"] == "fixed":
            print(f"  [{i}/{total}] ✅ {result['file']}: {result['old_len']} → {result['new_len']} chars")
        elif result["status"] == "would_fix":
            print(f"  [{i}/{total}] 🔧 {result['file']}: {result['old_len']} → {result['new_len']} chars (simulation)")
        elif result["status"] == "error":
            errors.append(result)
            print(f"  [{i}/{total}] ❌ {result['file']}: {result['reason']}")
        # ok/skip = silencieux pour éviter le spam

        # Rate limiting
        if i < total:
            time.sleep(RATE_LIMIT_DELAY)

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}Résultats:")
    print(f"  Fixed:   {results.get('fixed', 0) + results.get('would_fix', 0)}")
    print(f"  OK:      {results.get('ok', 0)}")
    print(f"  Erreur:  {results['error']}")
    print(f"  Skip:    {results['skip']}")

    if errors:
        print(f"\nDétails erreurs:")
        for e in errors[:20]:
            print(f"  - {e['file']}: {e['reason']}")
        if len(errors) > 20:
            print(f"  ... et {len(errors) - 20} autres")


if __name__ == "__main__":
    main()
