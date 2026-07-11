# yt-weekly-review

Watches a list of YouTube channels, summarizes every new full episode with the Gemini API, and emails one weekly review — three timestamped key takes per video, each linking straight to that moment. Runs entirely on GitHub Actions at no cost (free tiers of the YouTube Data API, the Gemini API, and Actions).

One entry from a weekly email:

```
Matt Wolfe — "How AI Agents Actually Work (Beyond the Hype)" (28m)
  [03:47] An agent is an LLM in a loop: plan, call a tool, read the result, repeat until a goal check passes.
  [11:20] MCP standardizes the tool side — one server exposes data and actions to any model, replacing per-app integrations.
  [19:05] Agent reliability compounds per step: a 95%-reliable step chained 10 times succeeds only ~60% of the time.
```

## How it works

Two scheduled workflows:

- **collect** (daily) — lists new uploads per channel via the YouTube Data API, filters out Shorts, clips, and live/upcoming broadcasts using a per-channel minimum duration, then summarizes each new full episode with Gemini. Gemini reads the YouTube URL directly (audio + frames, processed server-side), so there is no video downloading and no transcript scraping — which YouTube blocks from datacenter IPs anyway.
- **review** (Sunday) — assembles everything collected during the week into one plain-text + HTML email, grouped by channel, and sends it via Gmail SMTP.

## Design notes

- **Python stdlib only.** No pip installs; nothing to audit beyond this repo.
- **Untrusted-content hardening.** Video content can carry prompt injection, so model output is display-only text. Every link is constructed by code from a validated video ID and an integer timestamp — the model never produces a URL, filename, or parameter to anything.
- **Burn guards.** Processed-video ledger, per-run and per-day budgets, attempt ceilings, request pacing, and a workflow concurrency group: a bug cannot re-summarize the same video daily or hammer an API.
- **Conservative by default.** Free-tier rate limits are treated as worst-case until you observe your real ones; every knob lives in `config.json`. Note that Gemini's YouTube-URL video input is currently a free preview feature, so its terms and limits may change.

## Run your own

This repo is the **engine**: public, code only. Your **instance** is a small private repo holding your channel list and pipeline state — so your subscriptions and the daily state commits stay private while the code stays open.

1. Create a private repo (e.g. `yt-weekly-review-run`).
2. Copy `config.example.json` into it as `config.json` and add your channels. `channel_id` can be omitted — the collector resolves handles and prints the ID for you to paste back.
3. Copy the files from `templates/workflows/` into the instance's `.github/workflows/`. They check out this engine at run time; point `repository:` at your own copy of the engine if you prefer to pin.
4. Get keys: a Gemini API key from [Google AI Studio](https://aistudio.google.com) and a YouTube Data API v3 key from the Google Cloud console (restrict the key to that API).
5. Add the instance's Actions secrets: `YT_API_KEY`, `GEMINI_API_KEY`, `MAIL_USERNAME`, `MAIL_APP_PASSWORD` (a Gmail app password), `MAIL_TO`.
6. Dispatch the **collect** workflow with dry-run enabled and check the log: it lists the last week of videos per channel, each with a verdict — summarize, defer, or skip (with the reason).

## Configuration

The main knobs in `config.json`:

| key | meaning |
| --- | --- |
| `start_date` | the app's launch day (YYYY-MM-DD): it watches channels from this day on and never works through back catalogs |
| `channels[].min_minutes` | per-channel minimum duration — `4` excludes Shorts, `30` keeps only full podcast episodes |
| `lookback_days` | how far back the daily run looks; the extra day of overlap means a delayed cron never drops a video. Anything a run couldn't process is queued and carried over — budgets delay videos, never drop them |
| `max_video_hours` | videos longer than this are skipped and mentioned in the weekly email; `8` matches Gemini's own free-tier cap of 8 hours of YouTube video per day, so anything above it couldn't be processed in a day regardless |
| `max_videos_per_run`, `daily_video_hours_budget`, `daily_request_budget` | per-run and per-day processing caps; anything over budget is picked up the next day, oldest first |
| `chunk_minutes`, `single_request_max_minutes` | long videos are summarized in clipped chunks and merged |
| `request_pacing_seconds`, `max_attempts_per_video` | rate-limit safety and the retry ceiling |

## Monitoring

A video that permanently fails to summarize, or gets skipped for being too long, shows up as a `::warning::` annotation on the Actions run summary (not just buried in the log), and as a one-line mention in the next weekly email. A run that fails outright (bad config, corrupted state, missing secret) exits non-zero, which GitHub surfaces as a failed run — whether you get emailed about that depends on your own notification settings for the instance repo (Settings → Notifications, or "Watch" the repo), worth checking once.

## Status

Complete and running unattended: **collect** daily at 05:30 UTC, **review** every Sunday at 07:00 UTC (GitHub cron is best-effort, so start times can drift by up to ~30 minutes — the design tolerates that). Both workflows also keep `workflow_dispatch` for manual runs and dry runs. The config schema is stable.

## See also

[AI-daily-harvest](https://github.com/uros-simcic/AI-daily-harvest) — daily AI news harvest: fetches articles via RSS, summarizes with Mistral, emails the result.

## License

MIT
