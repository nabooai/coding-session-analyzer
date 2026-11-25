"""One way to page a list, used by every paged surface — REST and GraphQL alike.

The shape mirrors a common house idiom (`*Page { items, nextCursor, hasMore,
totalCount }`), NOT Relay connections: this codebase has no edges/node anywhere and inventing them
here would make CSA the odd one out for no gain.

THE CURSOR IS OPAQUE ON PURPOSE. Today it encodes an offset, because both things being paged are
naturally ordinal: a transcript's blocks are a list in a file, and a session listing is one ORDER BY.
Callers must never parse it — they echo back what they were given — so the day one of these needs
keyset pagination (a `WHERE started_at < ?` for a corpus that has grown), the encoding changes here
and no client changes at all. An integer `offset=` in the query string would have frozen that.

A cursor is signed with nothing and secret in no way: it names a POSITION, not a permission. Every
paged call re-runs its own authorization on the caller's principal, so replaying someone else's
cursor gets you your own data at their offset, which is worth exactly nothing.
"""
import base64
import json

DEFAULT_LIMIT = 50
MAX_LIMIT = 500          # a page is a screenful, not a bulk export; the cap is what makes that true


def clamp_limit(limit, default=DEFAULT_LIMIT, maximum=MAX_LIMIT):
    """A caller-supplied page size, bounded. Junk and non-positive values fall back to the default
    rather than erroring: a page size is a hint, and refusing the whole request over one is rude."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, maximum)


def encode_cursor(offset):
    """Position -> opaque token. urlsafe so it survives a query string untouched."""
    raw = json.dumps({"o": int(offset)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    """Opaque token -> position. Anything unreadable is position 0, NOT an error.

    A cursor arrives from a URL, a saved bookmark, a hand-edited client, or a version of this server
    that encoded it differently. Refusing the request would turn a stale link into a 400 the reader
    cannot fix; starting from the beginning is the harmless reading, and the response says where it
    actually is via `next_cursor`."""
    if not cursor:
        return 0
    try:
        pad = "=" * (-len(cursor) % 4)
        d = json.loads(base64.urlsafe_b64decode(cursor + pad))
        return max(0, int(d["o"]))
    except Exception:
        return 0


def page(items, limit=DEFAULT_LIMIT, cursor=None, total=None):
    """Slice an in-memory sequence into a page dict.

    `total` may be passed when the caller already knows the full count without materialising it (a
    SQL COUNT), in which case `items` is expected to be the already-sliced window; otherwise `items`
    is the whole sequence and the slicing happens here."""
    limit = clamp_limit(limit)
    start = decode_cursor(cursor)
    if total is None:
        total = len(items)
        window = list(items[start:start + limit])
    else:
        window = list(items)
    end = start + len(window)
    has_more = end < total
    return {"items": window, "total": int(total), "has_more": has_more,
            "next_cursor": encode_cursor(end) if has_more else None}


def sql_window(limit=DEFAULT_LIMIT, cursor=None):
    """(limit, offset) for a database-side page. The counterpart to `page` for anything too large to
    pull into memory first — the caller runs its own COUNT and passes both back to `page`."""
    return clamp_limit(limit), decode_cursor(cursor)


if __name__ == "__main__":
    assert clamp_limit(10) == 10 and clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit(0) == DEFAULT_LIMIT and clamp_limit(-5) == DEFAULT_LIMIT
    assert clamp_limit("banana") == DEFAULT_LIMIT, "a bad page size must not fail the request"
    assert clamp_limit(10_000) == MAX_LIMIT, "a page is bounded; this is what stops a bulk export"

    # round trip, and the promise that clients never parse it
    for n in (0, 1, 99, 123456):
        assert decode_cursor(encode_cursor(n)) == n
    assert "=" not in encode_cursor(7), "no padding: it rides in a URL"
    assert decode_cursor("not-a-cursor") == 0 and decode_cursor("") == 0 and decode_cursor(None) == 0, \
        "an unreadable cursor starts at the beginning; a stale bookmark must not 400"
    assert decode_cursor(encode_cursor(-3)) == 0, "a negative position clamps rather than slicing backwards"

    # walking a list to its end, exactly once each
    seq = list(range(10))
    seen, cur, pages = [], None, 0
    while True:
        p = page(seq, limit=3, cursor=cur)
        seen += p["items"]
        pages += 1
        assert p["total"] == 10
        if not p["has_more"]:
            assert p["next_cursor"] is None, "the last page must not offer a cursor to nothing"
            break
        cur = p["next_cursor"]
        assert pages < 10, "pagination did not terminate"
    assert seen == seq, f"paging lost or repeated items: {seen}"
    assert pages == 4, f"10 items at 3 per page is 4 pages, got {pages}"

    # an empty list is one empty page, not an error and not an endless one
    p = page([], limit=5)
    assert p["items"] == [] and p["total"] == 0 and not p["has_more"] and p["next_cursor"] is None

    # a cursor past the end yields nothing and stops
    p = page(seq, limit=3, cursor=encode_cursor(50))
    assert p["items"] == [] and not p["has_more"], "reading past the end is empty, never negative"

    # the db-side form: caller slices, we describe
    lim, off = sql_window(limit=3, cursor=encode_cursor(6))
    assert (lim, off) == (3, 6)
    p = page(seq[off:off + lim], limit=3, cursor=encode_cursor(6), total=len(seq))
    assert p["items"] == [6, 7, 8] and p["has_more"] and decode_cursor(p["next_cursor"]) == 9
    p = page(seq[9:12], limit=3, cursor=encode_cursor(9), total=len(seq))
    assert p["items"] == [9] and not p["has_more"], "a partial final page ends the walk"

    print("OK pagination: opaque cursor round-trips; junk/stale/negative cursors start at 0 rather "
          "than erroring; limits clamped; in-memory and SQL-window forms agree; a full walk visits "
          "every item exactly once and terminates")
