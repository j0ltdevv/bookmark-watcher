#!/usr/bin/env python3
"""repair-handles.py — répare les authorHandle='unknown' de Siftly
en récupérant tweet_id → handle depuis la timeline bookmarks X (session du watcher)."""

import json
import sqlite3
import urllib.parse

import requests

from watcher import get_session, poll_once, parse_bookmarks_payload, SIFTLY_DB

def all_bookmarks(session, max_pages=8):
    """Paginue la timeline bookmarks en réutilisant l'URL capturée (cursor bottom)."""
    seen = {}
    url = session["bookmarks_url"]
    for page in range(max_pages):
        tweets, err = poll_once(session) if page == 0 else (None, None)
        if page > 0:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            variables = json.loads(qs["variables"][0])
            variables["cursor"] = cursor
            new_url = urllib.parse.urlunparse(
                (parsed.scheme, parsed.netloc, parsed.path, "",
                 urllib.parse.urlencode({"variables": json.dumps(variables),
                                         "features": qs["features"][0]}, quote_via=urllib.parse.quote), ""))
            r = requests.get(new_url, headers=session["headers"], timeout=20)
            if r.status_code != 200:
                print(f"[repair] page {page+1}: HTTP {r.status_code}")
                break
            tweets = parse_bookmarks_payload(r.json())
        for t in tweets:
            if t["author_handle"] and t["author_handle"] != "unknown":
                seen[t["tweet_id"]] = t["author_handle"]
        print(f"[repair] page {page+1}: {len(tweets)} tweets (total connus: {len(seen)})")
        # cursor bottom
        payload = {"bookmark_timeline_v2": True}
        # on réutilise le body déjà parsé ? non : il faut le JSON brut — refetch ci-dessous
        try:
            if page == 0:
                r0 = requests.get(url, headers=session["headers"], timeout=20)
                raw = r0.json()
            else:
                raw = r.json()
            cursor = None
            for instr in raw["data"]["bookmark_timeline_v2"]["timeline"]["instructions"]:
                for entry in instr.get("entries", []):
                    if entry.get("entryId", "").startswith("cursor-bottom"):
                        c = entry.get("content", {})
                        cursor = c.get("value")
        except Exception as e:
            print(f"[repair] cursor: {e}")
            break
        if not cursor or not tweets:
            break
    return seen

def main():
    session = get_session()
    mapping = all_bookmarks(session)
    print(f"[repair] {len(mapping)} tweets avec handle récupérés")
    con = sqlite3.connect(f"file:{SIFTLY_DB}?mode=rw", uri=True)
    rows = con.execute('SELECT "tweetId" FROM Bookmark WHERE "authorHandle" IN (\'\', \'unknown\', \'x\')').fetchall()
    fixed = 0
    for (tweet_id,) in rows:
        handle = mapping.get(tweet_id)
        if handle:
            con.execute('UPDATE Bookmark SET "authorHandle"=? WHERE "tweetId"=?', (handle, tweet_id))
            fixed += 1
    con.commit()
    distinct = con.execute("SELECT COUNT(DISTINCT authorHandle) FROM Bookmark").fetchone()[0]
    remaining = con.execute("SELECT COUNT(*) FROM Bookmark WHERE authorHandle IN ('', 'unknown', 'x')").fetchone()[0]
    con.close()
    print(f"[repair] corrigés: {fixed} | auteurs distincts: {distinct} | unknown restants: {remaining}")

if __name__ == "__main__":
    main()
