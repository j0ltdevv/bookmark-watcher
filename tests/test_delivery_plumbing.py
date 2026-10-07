"""
Test de plomberie : un tweet REEL (avec lien) doit survivre au pipeline
HTML -> Markdown -> `hermes send` -> Telegram.

On n'appelle AUCUN LLM : le brief est fabrique en dur. Seul le chemin de
sortie est exerce, ce qui est justement ce qu'on veut tester.

usage: ./venv/bin/python tests/test_delivery_plumbing.py [--send]
       sans --send : dry-run, rien n'est envoye
"""
import sys
import os
import re
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watcher  # noqa: E402

# Un tweet REEL de @simonw (le brief Deno), avec le lien partage d'origine.
TWEET = {
    "tweet_id": "2107630159927062921",
    "author_handle": "simonw",
    "text": "I released an LLM plugin for sending prompts to OpenAI's new Jev-clone "
            "decision model that's based on GPT-6 Luna https://t.co/abcd1234 & "
            "more <not a tag> \"quoted\" 'apos'",
    "images": [],
}

BRIEF = {
    "verdict": "SOLIDE",
    "resume": [
        "Simon Willison publie un plugin LLM qui envoie des prompts vers un "
        "modele de decision base sur GPT-6 Luna.",
        "Le tweet contient <balises> HTML, un &, des guillemets et <b>du gras</b> "
        "pour tester l'echappement.",
    ],
    "verification": "decalage",
    "verification_note": "Le lien t.co redirige, le contenu reel n'a pas ete lu.",
    "nouveaute": "oui",
    "contexte_nouveaute": "Publie le 2026-10-07.",
    "raison": ["Le plugin evite de recopier a la main le prompt dans un autre client."],
    "limites": [
        "Le lien partage n'a pas ete ouvert, le contenu reel est inconnu.",
        "Aucun benchmark de cout par prompt n'est donne.",
    ],
    "sources": [
        "https://github.com/simonw/llm",
        "https://mistral.ai/news/mistral-large-4/",
    ],
    "suggestions": [
        "Comparer le cout par prompt avec l'appel direct a l'API.",
        "Cloner le repo et lancer les tests avant de l installer.",
    ],
    "angle": "Ca evite de recopier le prompt dans un autre client, mais le "
             "vrai interet c'est la livraison: si ce message arrive lisible "
             "avec ses liens cliquables, la plomberie est bonne.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="envoie vraiment (Telegram)")
    args = ap.parse_args()

    print("=" * 62)
    print("1. Conversion HTML -> Markdown")
    print("=" * 62)
    lines = []
    excerpt = TWEET["text"][:280]
    lines.append(f'Bookmark <a href="https://x.com/{TWEET["author_handle"]}/status/'
                 f'{TWEET["tweet_id"]}">— @{TWEET["author_handle"]}</a>')
    lines.append(f'<i>« {excerpt}… »</i>')
    lines.append(f'<b>✅ SOLIDE</b>')
    lines.append("\n<b>🎯 C'est quoi</b>")
    lines.append("  • Le plugin evite &amp; de recopier <b>du gras</b> a la main.")
    lines.append("\n<i>un angle avec <balises> et &amp;</i>")
    lines.append("\n<b>⚠️ Limites</b>")
    lines.append("  • Limite avec &lt;balise&gt; et &amp;")
    lines += ["", 'Sources: ' + " · ".join(f'<a href="{s}">lien</a>' for s in BRIEF["sources"])]

    html = "\n".join(lines)
    md = watcher._html_to_markdown(html)
    print(md)

    print()
    print("=" * 62)
    print("2. Verifications de la conversion")
    print("=" * 62)
    checks = []

    def check(label, cond, detail=""):
        checks.append((label, cond, detail))
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))

    check("aucun <b> restant", "<b>" not in md and "</b>" not in md)
    check("aucun <i> restant", "<i>" not in md and "</i>" not in md)
    check("aucune balise HTML brute", not any(t in md for t in ("<a ", "<br", "</i>")))
    check("&amp; de-entite", "&amp;" not in md, md)
    check("gras markdown ** present", "**" in md)
    check("italique markdown _ present", "_" in md)
    check("lien markdown [x](url)", "](" in md and "https://x.com/simonw/status/" in md)
    check("URL du lien partage conservee", "https://github.com/simonw/llm" in md)
    # 1 lien tweet + 2 liens sources = 3 liens cliquables au total
    check("3 liens cliquables (tweet + 2 sources)", md.count("](https") == 3,
          f"trouve {md.count('](https')}")
    # L'extrait du tweet contient un t.co en texte brut (normal) : on verifie
    # que chaque lien markdown a bien ses parentheses fermees.
    md_links = re.findall(r"\]\((https?://[^)]*)\)", md)
    check("chaque lien markdown a ses parentheses fermees",
          all(u.rstrip().endswith((")", "")) and ")" not in u for u in md_links)
          and len(md_links) == md.count("]("),
          f"{len(md_links)} lien(s) bien forme(s) pour {md.count('](')} marqueur(s)")

    failed = [c for c in checks if not c[1]]
    print()
    if failed:
        print(f"ECHEC : {len(failed)} verification(s) en panne.")
    else:
        print(f"OK : {len(checks)} verifications passent.")

    print()
    print("=" * 62)
    print("3. Envoi reel" + ("" if args.send else " (DRY-RUN)"))
    print("=" * 62)
    if not args.send:
        print("  Relance avec --send pour envoyer sur Telegram.")
        return 0 if not failed else 1

    ok = watcher.send_hermes(md, subject="Test plomberie — @simonw")
    print(f"  send_hermes() -> {ok}")
    return 0 if (ok and not failed) else 1


if __name__ == "__main__":
    sys.exit(main())
