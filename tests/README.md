# Tests

```bash
./venv/bin/python -m pytest tests/ -q
```

Couvre : parser GraphQL X (v2, TweetWithVisibilityResults, note_tweet, cursor ignoré,
structure inconnue dégradée en silence) et extraction JSON string-aware.

Les fixtures sont des payloads synthétiques reproduisant les structures réelles de l'API X.
Si X change sa structure et casse le poll, ajoute un fixture du nouveau format ici.
