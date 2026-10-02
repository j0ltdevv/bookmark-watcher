---
name: bookmark-watcher
description: "Parle des briefs de veille X du user : derniers résumés, recap, recherche dans la bibliothèque, état du watcher. À utiliser quand le user demande ce qu'il a bookmarké, un recap, ou l'état de sa veille."
---

# Bookmark-watcher

Le user a un watcher local qui surveille ses bookmarks X en continu et produit des **briefs**
(verdict + résumé + limites + sources) dans `bot-context.json`. Les briefs arrivent aussi ici
via webhook. Ce skill apprend au bot à en parler.

## 1. Launch — où sont les données

- Base : `/home/jolt/Documents/Default Project/bookmark-watcher/`
- Briefs + verdicts : `bot-context.json` — liste de records
  `{tweet_id, author, text, brief:{verdict, resume[], raison[], limites[], sources[], suggestions[], verification, dejavu}, reactions{}}`,
  les plus récents à la fin. Commande : `python3 -c "import json; recs=json.load(open('bot-context.json')); print(len(recs))"`.
- Bibliothèque complète : SQLite `/home/jolt/Documents/Default Project/siftly/prisma/dev.db`,
  table `Bookmark` (colonnes camelCase : `tweetId`, `authorHandle`, `text`, `importedAt`).
  Requête type : `sqlite3 dev.db "SELECT authorHandle, substr(text,1,120), datetime(importedAt/1000,'unixepoch') FROM Bookmark ORDER BY importedAt DESC LIMIT 10;"`.
- Mémoire du watcher (goûts, consignes) : `memory/profile.md`, `memory/feedback.md`, `memory/learnings.md`.
- Service systemd : `bookmark-watcher.service` (`systemctl --user is-active bookmark-watcher`).

Ne jamais afficher ni demander les secrets de `.env` (tokens Telegram, clés API, cookies X).

## 2. Doctor — le watcher tourne-t-il ?

`systemctl --user is-active bookmark-watcher` doit répondre `active`.
Sinon, cause la plus fréquente : quoting `ExecStart` perdu sur le chemin à espace
(`Unable to locate executable '/home/jolt/Documents/Default'` dans les logs = requoter,
pas un venv cassé). Logs : `journalctl --user -u bookmark-watcher -n 20`.

## 3. Drive — ce que le bot sait faire

- **Derniers briefs** : lire la fin de `bot-context.json`, présenter verdict + résumé en
  sections courtes, ton pote cash et enjoué, 100 % français.
- **Recap** : compter les verdicts (SOLIDE / MOYEN / BRUIT / GAME_CHANGER) sur les N derniers,
  top 2-3 avec liens `https://x.com/<author>/status/<tweet_id>`.
- **Retrouver un bookmark** : chercher dans `bot-context.json` puis dans la table `Bookmark`
  (FTS ou LIKE sur `text`), jamais d'URL inventée.
- **Approfondir** : le record contient déjà `sources[]` et `verification` ; chercher sur le
  web plutôt que demander un lien au user.
- **État** : Doctor ci-dessus + date du dernier record de `bot-context.json`.

## 4. Evidence

Chaque réponse cite : lien(s) x.com réels issus des records, verdicts tels quels,
et « je ne trouve pas » quand ni les records ni Siftly ne contiennent le sujet
(plutôt qu'inventer).

## 5. Cleanup

Lecture seule. Ne jamais écrire dans `bot-context.json`, `seen.json`, `session.json`,
ni relancer le service sans demande explicite. Ne rien committer dans le repo du watcher.

## 6. Feature map

1. « mes derniers briefs » → fin de `bot-context.json` → résumés courts. Preuve : liens x.com.
2. « recap de la semaine » → verdicts comptés + top avec liens. Preuve : chiffres cohérents.
3. « c'était quoi le tweet sur X ? » → recherche records puis SQLite. Preuve : lien ou aveu d'absence.

## 7. Maintenance

Si le schéma de `bot-context.json` change (nouveau champ de brief), mettre à jour
les sections 1 et 3. Re-lire `memory/feedback.md` du watcher pour rester aligné sur son ton.
