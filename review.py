"""Weekly reviewer: build and send one email from state/summaries.json,
then mark what was sent so re-running never duplicates it.

Security (mirrors gemini_client/collect): every link in the email is
built here, from a validated video id plus an integer offset — never
from model output. Everything rendered is HTML-escaped. The only
outbound endpoint is smtp.gmail.com:465.
"""

import argparse
import datetime as dt
import html
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import collect
import youtube_client as yt

NOTE_STATUSES = ("failed_permanent", "skipped_too_long")


def fmt_duration_human(seconds):
    """Header duration style per the brief's own example: "(2h 07m)" when
    there's an hour component, "(28m)" when there isn't."""
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def fmt_timestamp(seconds):
    """Take-timestamp style, matching YouTube's own convention (no
    zero-padded leading unit): "12:41", "1:33:12"."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def build_link(video_id, t_seconds=None):
    """The only place review.py constructs a URL: always from a
    regex-validated id and an int, never from take text or any other
    model output."""
    if not yt.VIDEO_ID_RE.match(video_id or ""):
        raise ValueError("invalid video id: %r" % (video_id,))
    url = "https://www.youtube.com/watch?v=%s" % video_id
    if t_seconds is not None:
        url += "&t=%d" % max(0, int(t_seconds))
    return url


def gather_pending(config, summaries):
    """Unsent entries grouped by channel in config.json order; within a
    channel, oldest published first. Channels with nothing pending are
    dropped rather than shown empty. Entries whose channel has since been
    removed from config.json still go out (appended after the configured
    channels) — already-summarized work must never be silently lost."""
    order = [c["name"] for c in config["channels"]]
    by_channel = {name: [] for name in order}
    for entry in summaries:
        if not entry.get("sent", False):
            by_channel.setdefault(entry["channel"], []).append(entry)
    for entries in by_channel.values():
        entries.sort(key=lambda e: e["published_at"])
    removed = sorted(name for name in by_channel if name not in set(order))
    return [(name, by_channel[name]) for name in order + removed if by_channel[name]]


def gather_notes(processed):
    """One-line-each mentions for terminal failures/skips not yet
    reported in a previous email. Independent of gather_pending: these
    must surface even on a zero-new-videos week, so a permanently broken
    channel can't go silently unnoticed."""
    notes = [(vid, rec) for vid, rec in processed.items()
             if rec.get("status") in NOTE_STATUSES and not rec.get("reported", False)]
    notes.sort(key=lambda item: (item[1].get("channel", ""), item[1].get("title", "")))
    return notes


def note_line(record, config):
    if record["status"] == "failed_permanent":
        return "%s — \"%s\" — could not be summarized after %d attempts" % (
            record["channel"], record["title"], record["attempts"])
    return "%s — \"%s\" — skipped (longer than the %dh limit)" % (
        record["channel"], record["title"], config["max_video_hours"])


def build_subject():
    # Matches the sibling AI-daily-harvest project's subject style
    # exactly: f"... — {date.today():%d %b %Y}", e.g. "10 Jul 2026".
    return "YT Weekly Review — %s" % dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y")


def build_plain_text(grouped, notes, config):
    lines = []
    if not grouped:
        lines.append("No new videos from your channels this week.")
        lines.append("")
    for channel_name, entries in grouped:
        for entry in entries:
            lines.append("%s — \"%s\" (%s)" % (
                channel_name, entry["title"], fmt_duration_human(entry["duration_seconds"])))
            for take in entry["takes"]:
                lines.append("  [%s] %s" % (fmt_timestamp(take["t_seconds"]), take["text"]))
            lines.append("")
    if notes:
        lines.append("Also this week:")
        for _, record in notes:
            lines.append("  " + note_line(record, config))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_html(grouped, notes, config):
    parts = ['<div style="font-family: -apple-system, Arial, sans-serif; '
             'font-size: 14px; color: #111; line-height: 1.5;">']
    if not grouped:
        parts.append("<p>No new videos from your channels this week.</p>")
    for channel_name, entries in grouped:
        for entry in entries:
            title_url = html.escape(build_link(entry["video_id"]))
            parts.append(
                '<p style="margin-bottom:6px;"><strong>%s</strong> — '
                '<a href="%s">%s</a> (%s)<br>'
                % (html.escape(channel_name), title_url, html.escape(entry["title"]),
                   fmt_duration_human(entry["duration_seconds"])))
            take_html = []
            for take in entry["takes"]:
                link = html.escape(build_link(entry["video_id"], take["t_seconds"]))
                take_html.append(
                    '&nbsp;&nbsp;<a href="%s">[%s]</a> %s'
                    % (link, fmt_timestamp(take["t_seconds"]), html.escape(take["text"])))
            parts.append("<br>".join(take_html))
            parts.append("</p>")
    if notes:
        parts.append("<p><strong>Also this week:</strong><br>")
        parts.append("<br>".join(
            "&nbsp;&nbsp;%s" % html.escape(note_line(record, config)) for _, record in notes))
        parts.append("</p>")
    parts.append("</div>")
    return "".join(parts)


def send_email(subject, plain_text, html_body):
    mail_user = os.environ.get("MAIL_USERNAME")
    mail_pass = os.environ.get("MAIL_APP_PASSWORD")
    mail_to = os.environ.get("MAIL_TO")
    if not (mail_user and mail_pass and mail_to):
        sys.exit("review.py: MAIL_USERNAME, MAIL_APP_PASSWORD, MAIL_TO must all be set")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_user
    msg["To"] = mail_to
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [addr.strip() for addr in mail_to.split(",") if addr.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(mail_user, mail_pass)
        server.sendmail(mail_user, recipients, msg.as_string())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print the email; do not send, do not mark sent")
    args = parser.parse_args()

    config = collect.load_config()
    processed = collect.load_processed()
    summaries = collect.load_summaries()

    grouped = gather_pending(config, summaries)
    notes = gather_notes(processed)
    video_count = sum(len(entries) for _, entries in grouped)

    subject = build_subject()
    plain_text = build_plain_text(grouped, notes, config)

    if args.dry_run:
        print(subject)
        print()
        print(plain_text)
        if notes:
            print("(%d note(s) would also be marked reported)" % len(notes))
        return

    html_body = build_html(grouped, notes, config)
    try:
        send_email(subject, plain_text, html_body)
    except smtplib.SMTPException as exc:
        sys.exit("review.py: send failed: %s" % collect.scrub(str(exc)))

    # Only mark sent/reported after a successful send. entries/records are
    # the same dict objects referenced inside summaries/processed, so
    # mutating them here is reflected when saved below.
    for _, entries in grouped:
        for entry in entries:
            entry["sent"] = True
    for _, record in notes:
        record["reported"] = True
    collect.save_summaries(summaries)
    collect.save_processed(processed)
    print("sent: %s (%d video(s), %d note(s))" % (subject, video_count, len(notes)))


if __name__ == "__main__":
    main()
