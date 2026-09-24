"""Separate collection and digest Lambda entrypoints."""

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .reports import message, select
from .sources import collect_source, stamp
from .store import Store

CONFIG = Path(__file__).resolve().parent.parent / "config"


def settings():
    return (
        json.loads((CONFIG / "sources.json").read_text()),
        json.loads((CONFIG / "aws-tags.json").read_text())["tags"],
    )


def metric(name, value, dimension="all"):
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "TechnologyDigest",
                            "Dimensions": [["Scope"]],
                            "Metrics": [{"Name": name}],
                        }
                    ],
                },
                "Scope": dimension,
                name: value,
            }
        )
    )


def health(store, sources, now):
    failures = []
    for source in sources:
        state = store.get("SOURCE#" + source["id"]) or {}
        age = now - state.get("success", 0)
        stale = age > source["interval"] * 2
        if stale or state.get("error"):
            failures.append(source["id"] + ": " + (state.get("error") or "collection stale"))
        metric("SourceStale", int(stale), source["id"])
    metric("StaleSources", len(failures))
    return failures


def collect(store, sources, allowed, now, fetch_source=collect_source, force=False):
    errors = []
    with store.lease("collector"):
        for source in sources:
            pk = "SOURCE#" + source["id"]
            state = store.get(pk) or {"pk": pk, "baseline_cutoff": now}
            if not force and now - state.get("success", 0) < source["interval"] - 60:
                continue
            store.put(state)
            try:
                run = str(uuid.uuid4())

                def snapshot(page, body, source=source, run=run):
                    store.write_blob(
                        f"snapshots/{source['id']}/{now}-{run}/{page}",
                        body,
                        "application/octet-stream",
                    )

                articles = fetch_source(source, allowed, snapshot, now)
                for article in articles:
                    if article["published"] < now - 90 * 86400:
                        continue
                    baseline = (
                        not state.get("initialized")
                        and article["published"] <= state["baseline_cutoff"]
                    )
                    store.ingest(article, baseline, now)
                state.update(initialized=True, success=now)
                state.pop("error", None)
            except Exception as exc:
                # Failure is durable and reported; other sources still make progress.
                state["error"] = type(exc).__name__ + ": " + str(exc)[:250]
                errors.append(source["id"])
            store.put(state)
        health(store, sources, now)
        metric("CollectionFailures", len(errors))
        if os.environ.get("MONITOR_DELIVERY") == "true":
            local = datetime.fromtimestamp(now, ZoneInfo("America/St_Johns"))
            # One-hour grace after each expected delivery; also catches missed scheduler targets.
            daily = local.replace(hour=9, minute=0, second=0, microsecond=0)
            if local < daily:
                daily -= timedelta(days=1)
            weekly = local.replace(hour=9, minute=15, second=0, microsecond=0) - timedelta(
                days=local.weekday()
            )
            if local < weekly:
                weekly -= timedelta(days=7)
            baseline = min(
                (store.get("SOURCE#" + source["id"]) or {}).get("baseline_cutoff", now)
                for source in sources
            )
            missing = sum(
                (store.get("DIGEST#" + kind + "-" + dt.date().isoformat()) or {}).get("status")
                != "SENT"
                for kind, dt in [("daily", daily), ("weekly", weekly)]
                if int((dt - timedelta(hours=1)).timestamp()) >= baseline
            )
            metric("MissedDelivery", missing)
    return {"failed_sources": errors}


def deliver(store, kind, scheduled, sender, recipient, sources, now, dry_run=False, sample_id=None):
    if kind not in ("daily", "weekly"):
        raise ValueError("kind must be daily or weekly")
    if sample_id is not None and not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", sample_id):
        raise ValueError("sample_id must contain 1-64 letters, digits or hyphens")
    delivery_kind = "sample" if sample_id else kind
    cutoff = stamp(scheduled)
    date = datetime.fromtimestamp(cutoff, ZoneInfo("America/St_Johns")).date().isoformat()
    digest_id = f"{kind}-{date}"
    if sample_id:
        digest_id = f"sample-{kind}-{sample_id}"
    pk = "DIGEST#" + digest_id
    with store.lease("delivery"):
        # Complete interrupted acknowledgments before constructing another daily batch.
        for prior in store.all("DIGEST#"):
            if prior["status"] == "SENT" and not prior.get("finalized") and not dry_run:
                store.finalize(prior)
            if prior["status"] in ("SENDING", "AMBIGUOUS") and prior["kind"] == delivery_kind:
                metric("AmbiguousSend", 1)
                raise RuntimeError("Reconcile uncertain SES send: " + prior["pk"])
        for prior in store.all("DIGEST#"):
            if (
                prior["status"] == "PREPARED"
                and prior["kind"] == delivery_kind
                and prior["pk"] != pk
            ):
                raise RuntimeError("Retry prepared digest before a new one: " + prior["pk"])
        record = store.get(pk)
        if record and record["status"] == "SENT":
            return {"status": "already_sent", "digest_id": digest_id}
        if record:
            bundle = store.read_json(record["bundle"])
        else:
            bundle = {
                "articles": select(store.all("ARTICLE#"), kind, cutoff),
                "failures": health(store, sources, now),
                "cutoff": cutoff,
                "kind": kind,
                "digest_id": digest_id,
            }
        if kind == "weekly" and not record:
            bundle["candidates"] = [
                a
                for a in store.all("ARTICLE#")
                if not a["baseline"]
                and a["discovered"] <= cutoff
                and cutoff - 7 * 86400 <= a["published"] <= cutoff
            ]
        if sample_id and not record:
            # Include baseline entries only in a separately tracked demonstration email.
            candidates = sorted(
                [
                    a
                    for a in store.all("ARTICLE#")
                    if a["expires"] > now and a["published"] <= cutoff
                ],
                key=lambda a: (-a["published"], a["id"]),
            )
            chosen = []
            counts = {}
            for article in candidates:
                technology = article["technology"]
                if counts.get(technology, 0) < 3:
                    chosen.append(article)
                    counts[technology] = counts.get(technology, 0) + 1
            bundle["articles"] = chosen
            bundle["sample"] = True
        title = (
            f"Technology {kind} {'sample (baseline included)' if sample_id else 'digest'} - {date}"
        )
        raw, text, html = message(
            bundle["articles"], title, bundle["failures"], sender, recipient, digest_id
        )
        prefix = ("previews/" if dry_run else "reports/") + digest_id
        store.write_json(prefix + "/sources.json", bundle)
        store.write_blob(prefix + "/report.txt", text, "text/plain; charset=utf-8")
        store.write_blob(prefix + "/report.html", html, "text/html; charset=utf-8")
        store.write_blob(prefix + "/message.eml", raw, "message/rfc822")
        if dry_run:
            return {"status": "preview", "key": prefix, "items": len(bundle["articles"])}
        record = record or {
            "pk": pk,
            "kind": delivery_kind,
            "status": "PREPARED",
            "bundle": prefix + "/sources.json",
            "created": now,
            "expires": now + 90 * 86400,
        }
        store.put(record)
        # From here onward a crash is ambiguous, including one just before the API call.
        record["status"] = "SENDING"
        store.put(record)
        try:
            send_options = {}
            if os.environ.get("SES_CONFIGURATION_SET"):
                send_options = {
                    "ConfigurationSetName": os.environ["SES_CONFIGURATION_SET"],
                    "Tags": [{"Name": "workload", "Value": "technology-digest"}],
                }
            response = store.ses.send_raw_email(
                Source=sender,
                Destinations=[recipient],
                RawMessage={"Data": raw},
                **send_options,
            )
        except Exception:
            record["status"] = "AMBIGUOUS"
            store.put(record)
            metric("AmbiguousSend", 1)
            raise
        record.update(status="SENT", message_id=response["MessageId"], sent=now)
        store.put(record)
        store.finalize(record)
        metric("Delivered", 1, delivery_kind)
        return {"status": "sent", "digest_id": digest_id, "message_id": response["MessageId"]}


def collector(event, context):
    sources, allowed = settings()
    store = Store(os.environ["TABLE_NAME"], os.environ["BUCKET_NAME"])
    result = collect(store, sources, allowed, int(time.time()), force=event.get("force", False))
    # A partial collection fails visibly and reaches the async failure destination.
    if result["failed_sources"]:
        raise RuntimeError("Failed sources: " + ", ".join(result["failed_sources"]))
    return result


def digester(event, context):
    sources, _ = settings()
    return deliver(
        Store(os.environ["TABLE_NAME"], os.environ["BUCKET_NAME"]),
        event["kind"],
        event["scheduled_time"],
        os.environ["SENDER"],
        os.environ["RECIPIENT"],
        sources,
        int(time.time()),
        event.get("dry_run", False),
        event.get("sample_id"),
    )
