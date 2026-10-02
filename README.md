# bookmark-watcher

Surveille tes bookmarks X/Twitter en continu et t'envoie sur Telegram, pour chaque nouveau :

- le lien du post
- un verdict sans complaisance : 🚀 GAME CHANGER / ✅ SOLIDE / 😐 MOYEN / 🗑️ BRUIT
- un résumé concret, la fraîcheur (récent / déjà vu), et pourquoi ça compte (ou pas)
- les sources web (recherche Exa) qui appuient l'analyse

Chaque bookmark détecté est aussi importé automatiquement dans Siftly (déjà dédupliqué en DB),
donc la catégorisation / l'export Obsidian suivent avec le reste.

## Mise en route (une fois)

```bash
# 1. Connexion X — une fenêtre s'ouvre, connecte-toi, la session persiste
./venv/bin/python watcher.py login

# 2. Créer le bot Telegram (2 min) :
#    - dans Telegram, parle à @BotFather → /newbot → note le token
#    - envoie un message quelconque à ton bot, puis récupère ton chat_id :
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9]*'
#    - remplis TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env

# 3. Marque tes bookmarks ACTUELS comme déjà vus (sinon tu manges 15 messages d'un coup)
./venv/bin/python watcher.py baseline

# 4. Test à la main — bookmarque un tweet, puis :
./venv/bin/python watcher.py once

# 5. Lancer en continu (poll HTTP toutes les 45 s, navigateur uniquement si session expirée)
systemctl --user enable --now bookmark-watcher.service
```

## Réglages (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `POLL_INTERVAL` | 45 | secondes entre deux scans de x.com/i/bookmarks |
| `MAX_PER_POLL` | 10 | nouveaux bookmarks traités par cycle (anti-rafales) |
| `AI_MODEL` | nemotron-3-super | modèle NIM pour le brief |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | sans eux, tout tourne mais rien n'est envoyé |
| `OPENMAUSBOT_WEBHOOK_URL` | — | webhook ingress OpenMausBot (`http://127.0.0.1:8800/hooks/wh_xxx/secret`) : chaque brief y est aussi poussé en Markdown, pour les résumés + discussion dans OpenMausBot |

## Dépannage

- `Session X absente ou expirée` → relance `watcher.py login`
- logs : `journalctl --user -u bookmark-watcher -f`
- l'état de dédup est dans `seen.json` (supprime-le pour forcer un re-scan complet, *avec* re-spam Telegram)
