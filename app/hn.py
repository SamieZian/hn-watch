"""Hacker News client via the Algolia HN Search API.

One request returns fully-hydrated items newer than a timestamp, which is
exactly the shape a monitor tick needs (the official Firebase API would take
N+1 requests). Trade-off: Algolia indexing lags the live site by a few
minutes — irrelevant for a periodic monitor.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


@dataclass
class HNItem:
    hn_id: int
    title: str
    url: str | None
    author: str
    points: int
    num_comments: int
    created_at_i: int

    @property
    def hn_url(self) -> str:
        return f"https://news.ycombinator.com/item?id={self.hn_id}"

    @property
    def created_at(self) -> str:
        return datetime.fromtimestamp(
            self.created_at_i, tz=timezone.utc
        ).isoformat(timespec="seconds")


async def fetch_recent(since_ts: int, limit: int = 50) -> list[HNItem]:
    """Stories (incl. Show/Ask HN) created after `since_ts`, newest first."""
    params = {
        "tags": "(story,show_hn,ask_hn)",
        "hitsPerPage": limit,
        "numericFilters": f"created_at_i>{since_ts}",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
    items = []
    for hit in resp.json().get("hits", []):
        try:
            items.append(
                HNItem(
                    hn_id=int(hit["objectID"]),
                    title=hit.get("title") or "(untitled)",
                    url=hit.get("url"),
                    author=hit.get("author") or "?",
                    points=hit.get("points") or 0,
                    num_comments=hit.get("num_comments") or 0,
                    created_at_i=hit.get("created_at_i") or 0,
                )
            )
        except (KeyError, ValueError):
            continue
    return items
