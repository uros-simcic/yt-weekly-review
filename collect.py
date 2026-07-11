"""Daily collector: list new videos on the configured channels, summarize
new full episodes, and update the state ledger.

State lifecycle (state/processed.json, keyed by video id):
  pending_retry    -> attempted, failed, attempts < max_attempts_per_video;
                      NOT terminal, resurfaces as a candidate next run.
  summarized       -> terminal; takes appended to state/summaries.json.
  failed_permanent -> terminal; attempts hit max_attempts_per_video.
  skipped_too_long -> terminal; duration > max_video_hours.
Only the three terminal statuses stop a video from being reprocessed —
pending_retry exists so a transient failure gets retried tomorrow with
its attempt count intact instead of being silently dropped or retried
forever.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

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


def gh_warning(message):
    """Print a GitHub Actions '::warning::' annotation: surfaces as a
    visible marker on the run's summary page instead of being buried in
    the log, for things worth a human's attention (not routine, expected
    outcomes like a first-attempt retry)."""
    print("::warning::%s" % message, file=sys.stderr)


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


PROCESSED_PATH = os.path.join("state", "processed.json")
SUMMARIES_PATH = os.path.join("state", "summaries.json")

# Only these stop classify() from offering the video again. pending_retry
# is deliberately absent: it must resurface as a normal candidate.
TERMINAL_STATUSES = {"summarized", "failed_permanent", "skipped_too_long"}


def load_processed(path=PROCESSED_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        # Fail loud rather than defaulting to {}: silently treating
        # corrupted state as empty would risk re-summarizing and
        # re-emailing everything already sent. State is version-controlled,
        # so recovery is a git revert, not a code path.
        sys.exit("collect.py: %s is corrupted (%s) — restore it from a "
                 "previous git commit before running again" % (path, exc))


def save_processed(processed, path=PROCESSED_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(processed, f, indent=2, sort_keys=True)
        f.write("\n")


def load_summaries(path=SUMMARIES_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        sys.exit("collect.py: %s is corrupted (%s) — restore it from a "
                 "previous git commit before running again" % (path, exc))


def save_summaries(summaries, path=SUMMARIES_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2)
        f.write("\n")


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
    record = processed.get(video["video_id"])
    if record and record.get("status") in TERMINAL_STATUSES:
        return record["status"]
    if parse_utc(video["published_at"]) < cutoff:
        return "outside lookback window"
    if video["is_live_or_upcoming"]:
        return "live/upcoming, not finished"
    if video["duration_seconds"] < channel["min_minutes"] * 60:
        return "shorter than min_minutes=%d" % channel["min_minutes"]
    if video["duration_seconds"] > config["max_video_hours"] * 3600:
        return "skipped_too_long"
    return None


def select_within_budgets(candidates, config):
    """Oldest-first, stop at the first video that would bust a budget.

    Stopping (rather than cherry-picking smaller videos behind the big
    one) keeps ordering strictly oldest-first, so anything deferred today
    is automatically first in line tomorrow — the deferral queue costs
    nothing and nothing can starve.

    Three independent budgets, any of which can stop selection:
    max_videos_per_run, daily_video_hours_budget (the free-tier 8h/day
    video cap, charged at full video length per the brief's conservative
    rule), and daily_request_budget (the scarcer real constraint: 20
    requests/day observed on the free tier).
    """
    candidates = sorted(candidates, key=lambda v: v["published_at"])
    selected = []
    budget_hours = 0.0
    budget_requests = 0
    for video in candidates:
        video_hours = video["duration_seconds"] / 3600
        video_requests = gemini_client.estimate_request_count(
            video["duration_seconds"], config)
        if (len(selected) >= config["max_videos_per_run"]
                or budget_hours + video_hours > config["daily_video_hours_budget"]
                or budget_requests + video_requests > config["daily_request_budget"]):
            break
        selected.append(video)
        budget_hours += video_hours
        budget_requests += video_requests
    return selected, candidates[len(selected):], budget_hours, budget_requests


def check_config_budgets(config):
    """Refuse configs where the largest allowed video could never fit the
    daily budgets: selection is strictly oldest-first and stops at the
    first video that busts a budget, so an unrunnable video at the head
    of the queue would stall the whole pipeline forever."""
    worst_requests = gemini_client.estimate_request_count(
        config["max_video_hours"] * 3600, config)
    if worst_requests > config["daily_request_budget"]:
        sys.exit("collect.py: config error — a %dh video (max_video_hours) needs "
                 "%d requests but daily_request_budget is %d; raise the budget "
                 "or lower max_video_hours"
                 % (config["max_video_hours"], worst_requests,
                    config["daily_request_budget"]))
    if config["max_video_hours"] > config["daily_video_hours_budget"]:
        sys.exit("collect.py: config error — max_video_hours (%d) exceeds "
                 "daily_video_hours_budget (%d); such a video could never be "
                 "scheduled" % (config["max_video_hours"],
                                config["daily_video_hours_budget"]))


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


def process_selected(selected, channel_by_id, config, processed, summaries, gemini_key):
    """Summarize each selected video, updating processed/summaries in
    place. One video's failure never stops the rest (per-video
    try/except is the brief's isolation rule extended to summarization).
    Pacing between videos reuses request_pacing_seconds; the last video
    skips the trailing sleep since nothing follows it.
    """
    for i, video in enumerate(selected):
        if i:
            time.sleep(config["request_pacing_seconds"])
        video_id = video["video_id"]
        channel_name = channel_by_id[video_id]
        prior_attempts = processed.get(video_id, {}).get("attempts", 0)
        try:
            takes = gemini_client.summarize_video(
                video_id, video["duration_seconds"], config, gemini_key)
        except gemini_client.GeminiError as err:
            attempts = prior_attempts + 1
            if attempts >= config["max_attempts_per_video"]:
                processed[video_id] = {
                    "status": "failed_permanent", "attempts": attempts,
                    "channel": channel_name, "title": video["title"], "reported": False,
                }
                gh_warning("%s failed permanently after %d attempts: %s"
                          % (video_id, attempts, scrub(str(err))))
            else:
                processed[video_id] = {
                    "status": "pending_retry", "attempts": attempts,
                    "channel": channel_name, "title": video["title"],
                }
                print("warning: %s failed (attempt %d/%d), will retry: %s"
                      % (video_id, attempts, config["max_attempts_per_video"], scrub(str(err))),
                      file=sys.stderr)
            continue
        processed[video_id] = {
            "status": "summarized", "attempts": prior_attempts + 1,
            "channel": channel_name, "title": video["title"],
        }
        summaries.append({
            "video_id": video_id, "channel": channel_name, "title": video["title"],
            "duration_seconds": video["duration_seconds"],
            "published_at": video["published_at"], "takes": takes, "sent": False,
        })


def run_daily(config, yt_key, gemini_key):
    """The real daily pipeline: list, classify, budget-select, summarize,
    persist state. State is saved even if a later step raises, so
    progress already made is never lost to an unrelated crash."""
    check_config_budgets(config)
    processed = load_processed()
    summaries = load_summaries()
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=config["lookback_days"])
    print("collect — %s UTC, lookback since %s"
          % (now.strftime("%Y-%m-%d %H:%M"), cutoff.strftime("%Y-%m-%d %H:%M")))

    all_candidates, channel_by_id = [], {}
    for channel in config["channels"]:
        try:
            candidates, skips = collect_channel(channel, config, processed, cutoff, yt_key)
        except Exception as exc:  # noqa: BLE001 — one channel's failure must not kill the run
            gh_warning("channel %r failed: %s" % (channel["name"], scrub(str(exc))))
            continue
        for video in candidates:
            channel_by_id[video["video_id"]] = channel["name"]
        all_candidates.extend(candidates)
        # skipped_too_long is terminal and must be recorded once so the
        # weekly email can mention it exactly one time (reported: false
        # until review.py flips it).
        for video, reason in skips:
            if reason == "skipped_too_long" and video["video_id"] not in processed:
                processed[video["video_id"]] = {
                    "status": "skipped_too_long", "attempts": 0,
                    "channel": channel["name"], "title": video["title"], "reported": False,
                }

    try:
        selected, deferred, budget_hours, budget_requests = select_within_budgets(
            all_candidates, config)
        print("selected %d video(s): %.1fh / %sh video-hours budget, "
              "~%d / %d requests budget, %d deferred"
              % (len(selected), budget_hours, config["daily_video_hours_budget"],
                 budget_requests, config["daily_request_budget"], len(deferred)))
        process_selected(selected, channel_by_id, config, processed, summaries, gemini_key)
    finally:
        save_processed(processed)
        save_summaries(summaries)


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
        print("        https://www.youtube.com/watch?v=%s&t=%d"
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

    yt_key = os.environ.get("YT_API_KEY")
    if not yt_key:
        sys.exit("collect.py: YT_API_KEY environment variable is not set")

    if not args.dry_run:
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            sys.exit("collect.py: GEMINI_API_KEY environment variable is not set")
        run_daily(load_config(), yt_key, gemini_key)
        return

    config = load_config()
    check_config_budgets(config)  # surface a misconfig in dry runs too
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
            candidates, skips = collect_channel(channel, config, processed, cutoff, yt_key)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the channel boundary
            gh_warning("channel %r failed: %s" % (channel["name"], scrub(str(exc))))
            per_channel.append((channel, [], []))
            continue
        per_channel.append((channel, candidates, skips))
        all_candidates.extend(candidates)

    selected, deferred, budget_hours, budget_requests = select_within_budgets(
        all_candidates, config)
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

    print("\ntotals: %d candidate(s), %d selected (%.1fh of %sh video-hours, "
          "~%d of %d requests budget), %d deferred"
          % (len(all_candidates), len(selected), budget_hours,
             config["daily_video_hours_budget"], budget_requests,
             config["daily_request_budget"], len(deferred)))


if __name__ == "__main__":
    main()
