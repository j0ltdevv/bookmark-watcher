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

Le bot n'est pas un lecteur de rapports. Il a le contexte, il peut chercher, et il peut
agir. Un recap sans conclusion est un Echec, pas une réponse.

- **Derniers briefs** : lire la fin de `bot-context.json`, présenter verdict + résumé en
  sections courtes, ton pote cash et enjoué, 100 % français.
- **Recap** : compter les verdicts (SOLIDE / MOYEN / BRUIT / GAME_CHANGER) sur les N derniers,
  top 2-3 avec liens `https://x.com/<author>/status/<tweet_id>`.
- **Retrouver un bookmark** : chercher dans `bot-context.json` puis dans la table `Bookmark`
  (FTS ou LIKE sur `text`), jamais d'URL inventée.
- **Approfondir** : le record contient déjà `sources[]` et `verification` ; chercher sur le
  web plutôt que demander un lien au user.
- **État** : Doctor ci-dessus + date du dernier record de `bot-context.json`.

### 3b. Prendre position, ne pas faire du reporting

Un recap qui se termine par « voilà, 3 Game Changer cette semaine » est un Echec. Chaque
réponse se termine par **ce que ça implique** :

- Le pattern qui se répète (3 briefs sur les mêmes outils ? 2 fois le même type de claim
  non sourcé ?) est le vrai sujet. Le dire.
- Pour un projet cited : ce que ça change concrètement pour le user, ou la raison de ne
  pas s'y interesser. Une opinion assumée vaut mieux qu'un résumé neutre.
- Proposer la suite quand elle existe : « si tu veux, je compare avec X », « je peux te
  sortir les 5 autres threads sur le même sujet ». Ne pas le faire sans y être invité si
  c'est coûteux, mais **le proposer** est obligatoire.
- Ne jamais finir sur « à surveiller », « ça vaut le coup d'œil », « on en reparlera ».
  Ce sont des formules de remplissage : remplace-les par un fait ou un verdict.

## 4. Evidence

Chaque réponse cite : lien(s) x.com réels issus des records, verdicts tels quels,
et « je ne trouve pas » quand ni les records ni Siftly ne contiennent le sujet
(plutôt qu'inventer).

## 5. Cleanup — ce que le bot a le droit de faire

**Interdits absolus, aucune exception :**
- Ne jamais écrire dans `seen.json` ou `chasse-seen.json` : ce sont les états de dédup,
  les altérer fait perdre ou re-spammer des bookmarks.
- Ne jamais toucher à `session.json` (cookies X) ni à `.env` (tokens, clés).
- Ne jamais committer dans le repo du watcher.
- Ne jamais supprimer de bookmark dans Siftly — uniquement en ajouter ou corriger.

**Autorisé sans demander :**
- Lire tous les fichiers du watcher et la base Siftly.
- Lancer une analyse à la demande : `watcher.py once`, une recherche Exa, une requête
  SQLite de vérification.
- Relancer un service mort : `systemctl --user restart bookmark-watcher` **après avoir lu
  les logs** et identifié la cause. Le dire au user, avec la cause et le fix.
- Écrire un record dans `bot-context.json` via le pipeline du watcher uniquement, jamais
  à la main.

**Demander avant :** toute suppression, toute modification de `system-prompt.md`,
`watchlist.json` ou `memory/`, et tout `git push`.

En cas de doute sur la blast radius : tu expliques ce que tu vas faire et tu attends.
Le défaut n'est pas d'agir, c'est d'agir sur un fichier d'état.

## 6. Feature map

1. « mes derniers briefs » → fin de `bot-context.json` → résumés courts. Preuve : liens x.com.
2. « recap de la semaine » → verdicts comptés + top avec liens. Preuve : chiffres cohérents.
3. « c'était quoi le tweet sur X ? » → recherche records puis SQLite. Preuve : lien ou aveu d'absence.

## 7. Maintenance

Si le schéma de `bot-context.json` change (nouveau champ de brief), mettre à jour
les sections 1 et 3. Re-lire `memory/feedback.md` du watcher pour rester aligné sur son ton.
