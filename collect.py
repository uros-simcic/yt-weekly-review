"""Daily collector: list new videos on the configured channels, decide
which ones to summarize, and report.

Current scope: the dry-run listing report plus --summarize-one, the
single-video summarization test. The full daily summarize loop and the
persistent state ledger arrive in Phase 3 — running without either flag
refuses instead of pretending to work.
"""

import argparse
import datetime as dt
import json
import os
import sys

import gemini_client
import youtube_client as yt

# Every value we print that could contain an error message goes through
# scrub() first: urllib exceptions can carry request details, and the
# security rules require that secret values never reach logs.
SECRET_ENV_VARS = ("YT_API_KEY", "GEMINI_API_KEY", "MAIL_USERNAME", "MAIL_APP_PASSWORD", "MAIL_TO")


def scrub(text):
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "<%s>" % name)
    return text


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_processed(path=os.path.join("state", "processed.json")):
    """The processed-ID ledger lands in Phase 3; reading it already
    (empty when absent) keeps the filter logic complete and testable now."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def parse_utc(iso):
    """publishedAt is RFC3339 UTC, e.g. 2026-07-08T15:00:04Z."""
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def fmt_duration(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def classify(video, channel, config, processed, cutoff):
    """Return None if the video is a summarization candidate, else the
    skip reason. Order matters only for which reason gets reported."""
    if video["video_id"] in processed:
        return "already processed"
    if parse_utc(video["published_at"]) < cutoff:
        return "outside lookback window"
    if video["is_live_or_upcoming"]:
        return "live/upcoming, not finished"
    if video["duration_seconds"] < channel["min_minutes"] * 60:
        return "shorter than min_minutes=%d" % channel["min_minutes"]
    if video["duration_seconds"] > config["max_video_hours"] * 3600:
        # Distinct status: these get a one-line mention in the weekly
        # email (Phase 4) instead of silently disappearing.
        return "skipped_too_long (> %dh)" % config["max_video_hours"]
    return None


def select_within_budgets(candidates, config):
    """Oldest-first, stop at the first video that would bust a budget.

    Stopping (rather than cherry-picking smaller videos behind the big
    one) keeps ordering strictly oldest-first, so anything deferred today
    is automatically first in line tomorrow — the deferral queue costs
    nothing and nothing can starve.
    """
    candidates = sorted(candidates, key=lambda v: v["published_at"])
    selected = []
    budget_hours = 0.0
    for video in candidates:
        video_hours = video["duration_seconds"] / 3600
        # The 8h/day free cap may count the full video even when we send
        # clipped chunks (unverified), so the budget conservatively
        # charges full length per video.
        if (len(selected) >= config["max_videos_per_run"]
                or budget_hours + video_hours > config["daily_video_hours_budget"]):
            break
        selected.append(video)
        budget_hours += video_hours
    return selected, candidates[len(selected):], budget_hours


def collect_channel(channel, config, processed, cutoff, api_key):
    """List + classify one channel's recent uploads.
    Returns (candidates, skips) where skips is [(video, reason), ...]."""
    channel_id = channel.get("channel_id")
    if not channel_id:
        channel_id = yt.resolve_channel_id(channel["handle"], api_key)
        if not channel_id:
            raise RuntimeError("handle %r did not resolve to a channel" % channel["handle"])
        # Not written back automatically in Phase 1 — printed so it can be
        # pasted into config.json, which saves the lookup on every run.
        print("  note: resolved handle %s -> %s (add \"channel_id\" to config.json)"
              % (channel["handle"], channel_id))

    upload_ids = yt.list_recent_upload_ids(yt.uploads_playlist_id(channel_id), api_key)
    details = yt.fetch_video_details(upload_ids, api_key)

    candidates, skips = [], []
    for video_id in upload_ids:  # preserve newest-first listing order
        video = details.get(video_id)
        if video is None:
            continue  # failed validation; already warned by youtube_client
        reason = classify(video, channel, config, processed, cutoff)
        if reason is None:
            candidates.append(video)
        else:
            skips.append((video, reason))
    return candidates, skips


def summarize_one(video_id, config):
    """Phase 2 acceptance test: summarize exactly one video and print the
    takes with code-built timestamp links. No state is read or written, so
    repeated test runs cost only Gemini requests, never duplicates."""
    yt_key = os.environ.get("YT_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not yt_key or not gemini_key:
        sys.exit("collect.py: --summarize-one needs YT_API_KEY and GEMINI_API_KEY")
    if not yt.VIDEO_ID_RE.match(video_id):
        sys.exit("collect.py: %r is not a valid video id" % video_id)

    video = yt.fetch_video_details([video_id], yt_key).get(video_id)
    if video is None:
        sys.exit("collect.py: video %s not found or failed validation" % video_id)
    print("test video: %s  (%s)" % (video["title"], fmt_duration(video["duration_seconds"])))

    takes = gemini_client.summarize_video(
        video_id, video["duration_seconds"], config, gemini_key, debug=True)
    print("\ntakes:")
    for take in takes:
        # Links are constructed here, by code, from the validated id and an
        # integer — never from model output.
        print("  [%s] %s" % (fmt_duration(take["t_seconds"]), take["text"]))
        print("        https://www.youtube.com/watch?v=%s&t=%ds"
              % (video_id, take["t_seconds"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list and filter only; no Gemini calls, no state writes")
    parser.add_argument("--summarize-one", metavar="VIDEO_ID",
                        help="summarize one video and print its takes (Phase 2 test)")
    args = parser.parse_args()

    if args.summarize_one:
        try:
            summarize_one(args.summarize_one, load_config())
        except gemini_client.GeminiError as exc:
            sys.exit("collect.py: summarization failed: %s" % scrub(str(exc)))
        return
    if not args.dry_run:
        sys.exit("collect.py: use --dry-run or --summarize-one; the full "
                 "summarize loop arrives in Phase 3")

    api_key = os.environ.get("YT_API_KEY")
    if not api_key:
        sys.exit("collect.py: YT_API_KEY environment variable is not set")

    config = load_config()
    processed = load_processed()
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=config["lookback_days"])
    print("dry run — %s UTC, lookback since %s"
          % (now.strftime("%Y-%m-%d %H:%M"), cutoff.strftime("%Y-%m-%d %H:%M")))

    all_candidates = []
    per_channel = []  # (channel, candidates, skips) in config order
    for channel in config["channels"]:
        # One channel failing (bad handle, API hiccup, malformed payload)
        # must never kill the whole run.
        try:
            candidates, skips = collect_channel(channel, config, processed, cutoff, api_key)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the channel boundary
            print("warning: channel %r failed: %s" % (channel["name"], scrub(str(exc))),
                  file=sys.stderr)
            per_channel.append((channel, [], []))
            continue
        per_channel.append((channel, candidates, skips))
        all_candidates.extend(candidates)

    selected, deferred, budget_hours = select_within_budgets(all_candidates, config)
    selected_ids = {v["video_id"] for v in selected}

    for channel, candidates, skips in per_channel:
        print("\n%s" % channel["name"])
        if not candidates and not skips:
            print("  (no uploads in listing window)")
        for video in candidates:
            verdict = ("WOULD SUMMARIZE" if video["video_id"] in selected_ids
                       else "deferred to next run (budget)")
            print("  [%s]  %s  %s  %s  %s" % (verdict, video["video_id"],
                                              video["published_at"],
                                              fmt_duration(video["duration_seconds"]),
                                              video["title"]))
        for video, reason in skips:
            print("  [skip: %s]  %s  %s  %s  %s" % (reason, video["video_id"],
                                                    video["published_at"],
                                                    fmt_duration(video["duration_seconds"]),
                                                    video["title"]))

    print("\ntotals: %d candidate(s), %d selected (%.1fh of %sh budget), %d deferred"
          % (len(all_candidates), len(selected), budget_hours,
             config["daily_video_hours_budget"], len(deferred)))


if __name__ == "__main__":
    main()
