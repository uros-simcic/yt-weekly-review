"""YouTube Data API v3 client — stdlib only.

Why the official Data API and not RSS feeds or yt-dlp: YouTube blocks
scraping from datacenter IPs (GitHub Actions runners included), so the
API is the only listing mechanism that dependably works from Actions.
This project spends ~10-20 quota units/day against the 10,000/day
default quota, so quota is a non-issue.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://www.googleapis.com/youtube/v3"

# Only video IDs matching this shape are ever accepted or turned into
# links anywhere in the pipeline (security rule: links are constructed
# by code from validated IDs only).
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# YouTube emits ISO-8601 durations: usually PT#H#M#S, occasionally with
# a days part (P#DT...) on very long streams; weeks are legal ISO-8601 so
# we accept them too. Live entries report "P0D" (-> 0 seconds), which the
# duration filters reject naturally. Anything unparseable returns None
# and the caller skips that video — malformed data is a skip, not a crash.
_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def parse_duration_seconds(iso):
    if not isinstance(iso, str):
        return None
    m = _DURATION_RE.match(iso)
    if not m or not any(m.groups()):  # bare "P"/"PT" match the regex but carry no value
        return None
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds


def _get(endpoint, params, api_key):
    """GET one Data API endpoint, return parsed JSON.

    The API key travels in the X-Goog-Api-Key header rather than the
    usual `key=` query parameter: urllib error messages embed the full
    request URL, so a key in the query string could leak into logs on
    any failed request. A header value never appears in those messages.
    """
    url = "%s/%s?%s" % (API_BASE, endpoint, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"X-Goog-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def resolve_channel_id(handle, api_key):
    """Resolve an @handle to a channel ID (channels.list?forHandle).

    Costs 1 quota unit; callers should cache the result in config.json
    so this only runs for newly added channels. Returns None when the
    handle doesn't resolve.
    """
    data = _get("channels", {"part": "id", "forHandle": handle}, api_key)
    items = data.get("items") or []
    channel_id = items[0].get("id") if items else None
    return channel_id if isinstance(channel_id, str) else None


def uploads_playlist_id(channel_id):
    """A channel's uploads playlist is its channel ID with the leading
    'UC' swapped for 'UU' — documented Data API convention that saves a
    channels.list call per channel per run.
    """
    if not (isinstance(channel_id, str) and channel_id.startswith("UC")):
        raise ValueError("unexpected channel_id format: %r" % (channel_id,))
    return "UU" + channel_id[2:]


def list_recent_upload_ids(playlist_id, api_key, max_items=15):
    """Return the newest ~max_items upload video IDs, newest first.

    Deliberately a single page with no pagination: the collector runs
    daily and no configured channel publishes 15+ videos between runs,
    while unbounded pagination is exactly the kind of loop the burn
    guards are meant to rule out.
    """
    data = _get(
        "playlistItems",
        {"part": "contentDetails", "playlistId": playlist_id, "maxResults": max_items},
        api_key,
    )
    ids = []
    for item in data.get("items") or []:
        vid = (item.get("contentDetails") or {}).get("videoId")
        if isinstance(vid, str) and VIDEO_ID_RE.match(vid):
            ids.append(vid)
        else:
            print("warning: playlistItems entry without a valid videoId, skipped", file=sys.stderr)
    return ids


def fetch_video_details(video_ids, api_key):
    """Fetch snippet + duration + live status for a list of video IDs.

    Batches up to 50 IDs per videos.list call (one call per channel at
    the default max_items=15). Returns {video_id: record} containing only
    records that passed validation; malformed items are dropped with a
    warning so one bad payload can't kill a run.
    """
    details = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        data = _get(
            "videos",
            {"part": "snippet,contentDetails,liveStreamingDetails", "id": ",".join(batch)},
            api_key,
        )
        for item in data.get("items") or []:
            record = _validate_video_item(item)
            if record is None:
                print("warning: malformed videos.list item %r, skipped"
                      % (item.get("id"),), file=sys.stderr)
            else:
                details[record["video_id"]] = record
    return details


def _validate_video_item(item):
    """Validate one videos.list item into the record shape the collector
    uses; None means the item is missing required fields."""
    video_id = item.get("id")
    snippet = item.get("snippet") or {}
    content = item.get("contentDetails") or {}
    title = snippet.get("title")
    published_at = snippet.get("publishedAt")
    duration_seconds = parse_duration_seconds(content.get("duration"))
    if not (isinstance(video_id, str) and VIDEO_ID_RE.match(video_id)):
        return None
    if not isinstance(title, str) or not isinstance(published_at, str) or duration_seconds is None:
        return None
    # liveBroadcastContent covers scheduled ("upcoming") and running
    # ("live") broadcasts. A finished stream/premiere flips back to
    # "none" but keeps a liveStreamingDetails block — with actualEndTime
    # set. A block *without* actualEndTime therefore means the broadcast
    # hasn't ended, so the video isn't summarizable yet.
    live_state = snippet.get("liveBroadcastContent", "none")
    live_details = item.get("liveStreamingDetails")
    unfinished_live = live_details is not None and "actualEndTime" not in live_details
    return {
        "video_id": video_id,
        "title": title,
        "published_at": published_at,
        "duration_seconds": duration_seconds,
        "is_live_or_upcoming": live_state != "none" or unfinished_live,
    }
