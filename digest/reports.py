"""Escaped HTML, plain text, deterministic ranking, complete report attachments."""

import gzip
import html
from collections import defaultdict
from email.message import EmailMessage
from email.policy import SMTP


def rank(article):
    text = (article["title"] + " " + article["excerpt"]).lower()
    score = 0
    for word, points in [
        ("security", 8),
        ("breaking", 7),
        ("generally available", 5),
        ("release", 3),
        ("performance", 2),
    ]:
        if word in text:
            score += points
    score -= 2 * article["regional"] + article["preview"]
    return (-score, -article["published"], article["id"])


def select(articles, kind, cutoff):
    eligible = [
        a
        for a in articles
        if not a["baseline"] and a["discovered"] <= cutoff and a["expires"] > cutoff
    ]
    if kind == "daily":
        return sorted(
            [a for a in eligible if not a.get("daily_digest")],
            key=lambda a: (a["technology"], a["published"], a["id"]),
        )
    return sorted(
        [a for a in eligible if cutoff - 7 * 86400 <= a["published"] <= cutoff], key=rank
    )[:10]


def render(articles, title, failures):
    lines = [title, "", "AWS coverage is based on publisher tags and is best effort.", ""]
    body = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>',
        html.escape(title),
        "</title><body><h1>",
        html.escape(title),
        "</h1><p>AWS coverage is based on publisher tags and is best effort.</p>",
    ]
    if failures:
        warning = "Partial coverage: " + "; ".join(failures)
        lines.extend([warning, ""])
        body.append("<p><strong>" + html.escape(warning) + "</strong></p>")
    groups = defaultdict(list)
    for item in articles:
        section = (
            "Regional previews"
            if item["preview"] and item["regional"]
            else "Previews"
            if item["preview"]
            else "Regional announcements"
            if item["regional"]
            else "Announcements"
        )
        groups[(item["technology"], section)].append(item)
    if not articles:
        lines.append("No new entries in this digest.")
        body.append("<p>No new entries in this digest.</p>")
    for (technology, section), items in sorted(groups.items()):
        lines.extend([technology + " / " + section, ""])
        body.append("<h2>" + html.escape(technology + " / " + section) + "</h2>")
        for item in items:
            excerpt = item["excerpt"]
            if len(excerpt) > 420:
                excerpt = excerpt[:420].rsplit(" ", 1)[0] + "..."
            tags = ", ".join(item["matched_tags"])
            lines.extend([item["title"], excerpt, item["url"]])
            body.append(
                '<article><h3><a href="'
                + html.escape(item["url"], quote=True)
                + '">'
                + html.escape(item["title"])
                + "</a></h3><p>"
                + html.escape(excerpt)
                + "</p>"
            )
            if tags:
                lines.append("Matched tags: " + tags)
                body.append("<p><small>Matched tags: " + html.escape(tags) + "</small></p>")
            lines.append("")
            body.append("</article>")
    body.append("</body></html>")
    return "\n".join(lines), "".join(body)


def message(articles, title, failures, sender, recipient, digest_id):
    text, html_body = render(articles, title, failures)
    msg = EmailMessage(policy=SMTP)
    msg["From"], msg["To"], msg["Subject"] = sender, recipient, title
    msg["X-Technology-Digest-ID"] = digest_id
    if len(html_body.encode()) > 85_000:
        msg.set_content(
            f"{title}\n\nComplete report ({len(articles)} entries) attached.\n"
            + "\n".join(failures)
        )
        msg.add_attachment(
            gzip.compress(html_body.encode()),
            maintype="application",
            subtype="gzip",
            filename="complete-report.html.gz",
        )
        msg.add_attachment(
            gzip.compress(text.encode()),
            maintype="application",
            subtype="gzip",
            filename="complete-report.txt.gz",
        )
    else:
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
    raw = msg.as_bytes()
    if len(raw) > 9_500_000:
        raise ValueError(
            "Report exceeds SES size limit; preserved in S3, no items marked delivered"
        )
    return raw, text, html_body
