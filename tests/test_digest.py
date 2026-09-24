import json
from datetime import datetime, timezone
from email import message_from_bytes
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from digest.handler import collect, deliver, settings
from digest.reports import message, render, select
from digest.sources import collect_source, parse_feed
from digest.store import Busy, Store

NOW = 1789992000
SCHEDULE = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
AWS = {
    "id": "aws",
    "technology": "AWS",
    "kind": "rss",
    "url": "https://aws.test/feed",
    "interval": 3600,
}
TAGS = settings()[1]


def feed(categories="", title="An announcement", uid="one", url="https://aws.test/a"):
    return f"""<rss><channel><item><guid>{uid}</guid><title>{title}</title>
    <link>{url}</link><pubDate>Mon, 21 Sep 2026 11:00:00 GMT</pubDate>
    <description>&lt;p&gt;Publisher excerpt&lt;/p&gt;</description>{categories}
    </item></channel></rss>""".encode()


def article(id="a", **extra):
    return dict(
        id=id,
        upstream_id=id,
        source="aws",
        technology="AWS",
        url="https://aws.test/" + id,
        title="New feature " + id,
        excerpt="Excerpt",
        published=NOW - 100,
        matched_tags=[TAGS[0]],
        preview=False,
        regional=False,
        **extra,
    )


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="digest",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3").create_bucket(Bucket="digest-artifacts")
        s = Store("digest", "digest-artifacts")
        s.ses = Mock()
        s.ses.send_raw_email.return_value = {"MessageId": "ses-123"}
        yield s


@pytest.mark.parametrize("tag", TAGS)
def test_each_configured_tag_independently_qualifies(tag):
    assert parse_feed(feed(f"<category>{tag}</category>"), AWS, TAGS)[0]["matched_tags"] == [tag]


@pytest.mark.parametrize(
    "cats",
    [
        "",
        "<category/>",
        "<category>general:products/amazon-bedrock</category>",
        "<category>general:products/amazon-kinesis-firehose</category>",
        "<category>general:products/amazon-s3-other</category>",
    ],
)
def test_no_matching_tag_excluded_even_if_title_names_selected_product(cats):
    assert parse_feed(feed(cats, "Amazon S3 and Lambda"), AWS, TAGS) == []


def test_multiple_elements_comma_whitespace_dedup_and_labels():
    a, b = TAGS[:2]
    result = parse_feed(
        feed(
            f"<category> {a}, unrelated, {b} </category><category>{a}</category>",
            "Preview now available in additional AWS Regions",
        ),
        AWS,
        TAGS,
    )
    assert len(result) == 1 and result[0]["matched_tags"] == sorted([a, b])
    assert result[0]["preview"] and result[0]["regional"]


def test_github_pagination_and_snapshot():
    source = {
        "id": "repo",
        "technology": "Test",
        "kind": "github",
        "url": "https://api.github.com/releases",
    }
    release = {
        "id": 1,
        "html_url": "https://github.com/org/repo/releases/tag/v1",
        "name": "v1",
        "tag_name": "v1",
        "body": "body",
        "published_at": SCHEDULE,
        "draft": False,
        "prerelease": True,
    }
    fetch = Mock(
        side_effect=[
            (json.dumps([release]).encode(), '<https://api.github.com/page2>; rel="next"'),
            (json.dumps([dict(release, id=2)]).encode(), ""),
        ]
    )
    snapshot = Mock()
    items = collect_source(source, [], snapshot, NOW, fetch)
    assert len(items) == 2 and items[0]["preview"]
    assert snapshot.call_count == 2 and fetch.call_count == 2


def test_baseline_repeat_and_late_arrival(store):
    fetch = Mock(return_value=[article()])
    collect(store, [AWS], TAGS, NOW, fetch)
    collect(store, [AWS], TAGS, NOW + 3600, fetch)
    assert len(store.all("ARTICLE#")) == 1
    assert select(store.all("ARTICLE#"), "daily", NOW + 3600) == []
    fetch.return_value = [article(), article("late")]
    collect(store, [AWS], TAGS, NOW + 7200, fetch)
    assert [a["id"] for a in select(store.all("ARTICLE#"), "daily", NOW + 7200)] == ["late"]


def test_url_and_upstream_id_dedup(store):
    assert store.ingest(article(), False, NOW)
    changed = article()
    changed["url"] = "https://aws.test/changed"
    assert not store.ingest(changed, False, NOW)
    duplicate = article("other")
    duplicate["url"] = "https://aws.test/changed"
    assert not store.ingest(duplicate, False, NOW)
    assert len(store.all("ARTICLE#")) == 1


def test_overlapping_collectors_and_digest_leases(store):
    with store.lease("collector"):
        with pytest.raises(Busy):
            collect(store, [AWS], TAGS, NOW, Mock())
    with store.lease("delivery"):
        with pytest.raises(Busy):
            deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)


def test_partial_failure_preserves_other_sources_and_retry_baseline(store):
    other = dict(AWS, id="other")
    collect(store, [AWS, other], TAGS, NOW, Mock(side_effect=[OSError("offline"), [article()]]))
    assert store.get("SOURCE#aws")["error"]
    assert store.get("SOURCE#other")["success"] == NOW
    assert len(store.all("ARTICLE#")) == 1
    collect(store, [AWS], TAGS, NOW + 3600, Mock(return_value=[article("old")]))
    assert store.get("ARTICLE#old")["baseline"]


def test_daily_weekly_independent_and_repeated_send_suppressed(store):
    store.ingest(article(), False, NOW - 10)
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    deliver(store, "weekly", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    assert store.ses.send_raw_email.call_count == 2
    assert (
        len(store.read_json("reports/weekly-" + SCHEDULE[:10] + "/sources.json")["articles"]) == 1
    )


def test_late_collection_after_cutoff_goes_to_next_digest(store):
    store.ingest(article(), False, NOW + 1)
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW + 2)
    assert not store.get("ARTICLE#a").get("daily_digest")
    assert len(select(store.all("ARTICLE#"), "daily", NOW + 86400)) == 1


def test_ambiguous_send_blocks_retry_and_next_day(store):
    store.ingest(article(), False, NOW)
    store.ses.send_raw_email.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    with pytest.raises(RuntimeError):
        deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    assert store.ses.send_raw_email.call_count == 1
    assert not store.get("ARTICLE#a").get("daily_digest")


def test_crash_after_ses_ack_finalizes_without_resend(store):
    store.ingest(article(), False, NOW)
    finalize = store.finalize
    store.finalize = Mock(side_effect=RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    store.finalize = finalize
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    assert store.ses.send_raw_email.call_count == 1
    assert store.get("ARTICLE#a")["daily_digest"]


def test_prepared_previous_date_must_be_retried_first(store):
    store.put({"pk": "DIGEST#daily-2026-09-01", "status": "PREPARED", "kind": "daily"})
    with pytest.raises(RuntimeError, match="Retry prepared"):
        deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    store.ses.send_raw_email.assert_not_called()


def test_preview_does_not_change_delivery_state(store):
    store.ingest(article(), False, NOW)
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW, dry_run=True)
    assert store.all("DIGEST#") == []
    assert not store.get("ARTICLE#a").get("daily_digest")
    store.ses.send_raw_email.assert_not_called()


def test_weekly_limit_window_and_ranking():
    items = [
        article(str(i), baseline=False, discovered=NOW, expires=NOW + 86400) for i in range(15)
    ]
    items[-1]["title"] = "Critical security release"
    result = select(items, "weekly", NOW)
    assert len(result) == 10 and result[0]["id"] == "14"
    items[0]["published"] = NOW - 8 * 86400
    assert items[0] not in select(items, "weekly", NOW)


def test_html_escaped_sections_failures_and_busy_day_attachment():
    item = article()
    item.update(title="<script>alert(1)</script>", regional=True, preview=True)
    text, html = render([item], "Digest", ["source unavailable"])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Regional previews" in text and "Partial coverage" in html
    raw, _, _ = message(
        [dict(item, excerpt="x" * 1200)] * 300, "Digest", [], "a@test.com", "b@test.com", "id"
    )
    parsed = message_from_bytes(raw)
    assert sum(p.get_content_disposition() == "attachment" for p in parsed.walk()) == 2


def test_retention_expired_upstream_entries_not_reintroduced(store):
    old = article()
    old["published"] = NOW - 91 * 86400
    collect(store, [AWS], TAGS, NOW, Mock(return_value=[old]))
    assert store.all("ARTICLE#") == []


def test_weekly_saves_all_candidates_for_future_ai(store):
    for i in range(15):
        store.ingest(article(str(i)), False, NOW)
    deliver(store, "weekly", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    bundle = store.read_json("reports/weekly-" + SCHEDULE[:10] + "/sources.json")
    assert len(bundle["articles"]) == 10
    assert len(bundle["candidates"]) == 15


def test_send_uses_configuration_set_for_delivery_events(store, monkeypatch):
    monkeypatch.setenv("SES_CONFIGURATION_SET", "technology-digest")
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    options = store.ses.send_raw_email.call_args.kwargs
    assert options["ConfigurationSetName"] == "technology-digest"
    assert options["Tags"] == [{"Name": "workload", "Value": "technology-digest"}]


def test_manual_sample_includes_baseline_without_consuming_daily_items(store):
    for i in range(5):
        store.ingest(article(str(i)), True, NOW)
    result = deliver(
        store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW, sample_id="review-1"
    )
    bundle = store.read_json("reports/sample-daily-review-1/sources.json")
    assert len(bundle["articles"]) == 3
    assert result["status"] == "sent"
    assert "sample" in store.ses.send_raw_email.call_args.kwargs["RawMessage"]["Data"].decode()
    assert all(not a.get("daily_digest") for a in store.all("ARTICLE#"))
    assert store.get("DIGEST#sample-daily-review-1")["kind"] == "sample"
    repeated = deliver(
        store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW, sample_id="review-1"
    )
    assert repeated["status"] == "already_sent"
    assert store.ses.send_raw_email.call_count == 1


def test_manual_sample_separate_from_existing_daily_delivery(store):
    store.ingest(article(), True, NOW)
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW)
    deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW, sample_id="review-2")
    assert store.ses.send_raw_email.call_count == 2


@pytest.mark.parametrize("sample_id", ["", "../escape", "x" * 65])
def test_manual_sample_rejects_invalid_id(store, sample_id):
    with pytest.raises(ValueError, match="sample_id"):
        deliver(store, "daily", SCHEDULE, "a@test.com", "b@test.com", [], NOW, sample_id=sample_id)
