"""Tests du parser GraphQL X et de l'extraction JSON — exécuter avec pytest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import _extract_json_object, parse_bookmarks_payload

def make_tweet(tid, text, handle, typename="Tweet", note_text=None):
    legacy = {"id_str": tid, "full_text": text, "created_at": "Mon Sep 08 20:00:00 +0000 2026",
              "extended_entities": {"media": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/x.jpg"}]}}
    core = {"user_results": {"result": {"__typename": "User",
            "core": {"screen_name": handle}, "legacy": {"screen_name": handle}}}}
    inner = {"legacy": legacy, "core": core}
    if note_text:
        inner["note_tweet"] = {"note_tweet_results": {"result": {"text": note_text}}}
    if typename == "TweetWithVisibilityResults":
        # chez X, le tweet réel est enveloppé sous la clé "tweet"
        return {"__typename": typename, "rest_id": tid, "tweet": inner}
    return {"__typename": typename, "rest_id": tid, **inner}

def wrap(result):
    return {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [
        {"entries": [
            {"entryId": f"tweet-{result['rest_id']}", "content": {"entryType": "TimelineTimelineItem",
             "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": result}}}},
            {"entryId": "cursor-bottom-0", "content": {"entryType": "TimelineTimelineCursor", "value": "CUR"}},
        ]}]}}}}

def test_parse_v2_basic():
    tweets = parse_bookmarks_payload(wrap(make_tweet("111", "hello world", "devguy")))
    assert len(tweets) == 1
    t = tweets[0]
    assert t["tweet_id"] == "111"
    assert t["author_handle"] == "devguy"
    assert t["text"] == "hello world"
    assert len(t["images"]) == 1

def test_parse_visibility_wrapper():
    tweets = parse_bookmarks_payload(wrap(make_tweet("222", "wrapped", "otherguy", typename="TweetWithVisibilityResults")))
    assert tweets[0]["tweet_id"] == "222"
    assert tweets[0]["author_handle"] == "otherguy"

def test_parse_long_tweet_note():
    long = "x" * 500
    tweets = parse_bookmarks_payload(wrap(make_tweet("333", "short truncated", "guy", note_text=long)))
    assert tweets[0]["text"] == long, "note_tweet doit primer sur full_text tronqué"

def test_cursor_ignored_and_empty_ok():
    payload = {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [{"entries": [
        {"entryId": "cursor-bottom-0", "content": {"entryType": "TimelineTimelineCursor", "value": "C"}}]}]}}}}
    assert parse_bookmarks_payload(payload) == []
    # structure inconnue → pas de crash, liste vide (dégradation silencieuse)
    assert parse_bookmarks_payload({"unexpected": 1}) == []

def test_extract_json_object_balanced():
    obj = _extract_json_object('noise {"text": "hello } world", "n": 1} trailing')
    assert obj is not None
    import json
    assert json.loads(obj)["text"] == "hello } world"

def test_extract_json_object_none():
    assert _extract_json_object("no json here") is None
    assert _extract_json_object("{ truncated") is None
