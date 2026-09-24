# Technology digest

Python Lambda runtime for a daily comprehensive technology email and an independent weekly top-ten email. Infrastructure lives in `../aws-account-infra/live/aws/staging/workloads/technology-digest` and deploy unit `aws-staging-technology-digest`. Runtime repository: https://github.com/TusharSariyaOrg/technology-digest. Infrastructure repository: https://github.com/TusharSariyaOrg/aws-account-infra.

## Development and packaging

```sh
uv sync --frozen --group dev
uv run pytest -q
uv run ruff check digest tests scripts
uv run ruff format --check digest tests scripts
./scripts/package.sh
```

The package contains pinned production dependencies, runtime and configuration. `build/artifact.json` identifies the content-addressed release zip. Upload it to the existing `lambda-releases-992382747916-us-east-1` bucket at that key, then update the infrastructure var file's key and base64 hash. OpenTofu pins the S3 object version as well. Packaging does not deploy or enable schedules.

## Sources and filtering

`config/sources.json` is the editable source registry. AWS polls hourly; other sources poll every six hours, driven by the hourly collector and per-source last-success timestamps. Failed sources retry on the next invocation. GitHub follows every `rel="next"` release page, including drafts/prerelease metadata. Drafts are excluded; prereleases remain labeled. No GitHub credential is needed for the current registry; unauthenticated API rate limits are visible source failures. The OpenTofu release page size is reduced because its asset metadata exceeds 12 MB at 100 entries.

`config/aws-tags.json` contains the exact AWS allowlist and evidence URLs. Every category element is split on commas, stripped and deduplicated. Any exact match qualifies the announcement, regardless of its primary product. There is no AWS keyword inclusion, AI classification, or service discovery. The general Kinesis tag is broad and can cause irrelevant matches; dedicated Firehose and Flink tags are not selected. RDS aliases were verified for MySQL, Oracle and SQL Server; other engines rely on the general RDS tag. No unverified aliases are silently assumed.

HashiCorp's publisher-wide official feed is restricted to titles containing Terraform. This restriction applies only to that non-AWS source. Other publishers have product-specific feeds. Runtime only reads configured official endpoints and GitHub release pagination links. Fetch errors, malformed feeds, oversized responses and incomplete pagination do not mark a source successful.

Preview and regional labels use conservative title rules after inclusion. They are presentation hints, not exhaustive classification. AWS tag coverage and finite publisher feeds are best effort, not exhaustive changelogs. Publication timestamps are required; missing dates fail that source visibly. Future-dated entries can be collected but weekly selection excludes them until their publication date.

## Persistence and delivery behavior

DynamoDB holds source checkpoints, articles, upstream-ID and canonical-URL aliases, leases and digest delivery records. Article/alias writes are transactional. Collector and delivery each have an independent global conditional lease lasting 960 seconds, longer than the 900-second Lambda timeout. An overlap fails visibly and can retry. Source snapshots, complete rendered reports, MIME messages and weekly source bundles live in private S3. Weekly bundles retain all eligible candidates as well as the selected ten.

First successful collection of each source establishes its baseline. Baseline articles are suppressed in both emails. Later discoveries are eligible for the next daily email regardless of publication age within the 90-day retention window. Feed items older than retention are ignored, preventing replays after TTL removal. Daily selection uses discovery time and explicit per-article delivery acknowledgments, not a publication-time watermark. Concurrent collection cannot cause an item to be marked delivered unless it is in the saved report. Weekly selection uses publication time within the previous seven days, independently of daily acknowledgments.

The digest ID is `daily-YYYY-MM-DD` or `weekly-YYYY-MM-DD`, using the scheduled timestamp in America/St_Johns. Retries must preserve that timestamp. Delivery states are PREPARED -> SENDING -> SENT, with AMBIGUOUS on a send exception. The complete report is persisted before sending. SDK retries are disabled for SES. A crash in SENDING also requires reconciliation. SENT records recover interrupted article acknowledgments without sending again. An older PREPARED record must be retried before a newer digest of the same kind. Routine duplicate invocations return `already_sent`.

Daily output includes all eligible entries grouped by technology and announcement/preview/regional subsections. Excerpts use publisher text shortened to 420 characters. Weekly ranking is deterministic: security, breaking changes, general availability, releases and performance receive decreasing weights; regional items and previews receive small penalties. Ties use publication date and upstream identity. No local AI is invoked.

Large reports become gzip-compressed HTML and plain-text attachments. Every complete report is also retained in S3. An email exceeding the conservative SES size limit fails before sending or acknowledging articles. Publication history cannot be recovered if it disappears from upstream feeds while collection is down.

## Operations

Default schedules: daily 08:00, Monday weekly 08:15, America/St_Johns; AWS collection hourly. Logs retain 14 days; articles/reports/snapshots retain 90 days. S3 versioning retains overwritten noncurrent versions for one day, so physical deletion can lag slightly. Source checkpoints have no TTL. DynamoDB point-in-time recovery and deletion protection are enabled.

Before enabling schedules, invoke `digest.handler.collector` with `{}` to establish source baselines, then invoke the digester with a dry run:

```json
{"kind":"daily","scheduled_time":"2026-09-24T12:00:00Z","dry_run":true}
```

The returned S3 prefix contains HTML, plain text, MIME and normalized sources. Use a current timestamp for a real verification send and set `dry_run` to false. Baseline entries remain suppressed. Check the SES `Delivery` metric in namespace `AWS/SES`, dimension `workload=technology-digest`, before enabling schedules. Delivery means the recipient mail server accepted the message; it does not prove inbox placement or reading.

Alarms reuse the existing `court-booker-api-alerts` SES-backed notification channel. Its current email template carries court-booker branding; the message includes the full `technology-digest-*` CloudWatch alarm name. Monitored signals include Lambda errors, queued failures, source failures/staleness, ambiguous sends, missed expected digest dates, collector silence, and SES bounce/complaint/rejection. EventBridge target failures and Lambda asynchronous failures go to the encrypted 14-day SQS failure queue. A successfully invoked Lambda may still fail later; both paths are covered. After repairing the cause, replay using the original scheduled timestamp. Do not blindly replay ambiguous sends.

### Ambiguous SES reconciliation

1. Disable schedules while inspecting an uncertain send. Wait until the delivery lease expires or the invocation finishes.
2. Read the `DIGEST#<id>` record and `reports/<id>/message.eml`. Correlate the `X-Technology-Digest-ID` header with the recipient mailbox and SES events. A timeout does not prove failure.
3. If delivery is confirmed, set the record's status to SENT, record the SES message ID/evidence and leave `finalized` false. Invoke the original digest event; it will finish acknowledgments without sending.
4. If non-delivery is established, set status to PREPARED and record the reconciliation evidence. Invoke the original event to reuse its exact saved article manifest.
5. If the result remains unknown, leave AMBIGUOUS/SENDING untouched. This intentionally blocks later emails of the same kind to avoid duplicate content. Resume schedules after reconciliation.

Never delete a delivery record to force a resend. PREPARED records can be safely retried with their original scheduled timestamp. Status records and report bundles expire after 90 days; reconcile uncertainties well before that boundary.

### Manually email a populated sample

Invoke `technology-digest-digester` with a current scheduled timestamp and a unique `sample_id`:

```json
{"kind":"daily","scheduled_time":"2026-09-24T12:00:00Z","sample_id":"review-20260924-1"}
```

The sample includes up to three recent stored articles per technology, including baseline entries. Its subject clearly identifies it as a sample. It has its own durable delivery record and does not acknowledge articles for daily delivery or satisfy daily/weekly delivery monitoring. Repeating the same kind and `sample_id` suppresses duplicate sends. Use a new ID only when deliberately requesting another sample. Add `dry_run: true` to render and archive without emailing.
