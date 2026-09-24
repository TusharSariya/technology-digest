"""Conditional leases, transactional article identities, durable delivery records."""

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config


class Busy(RuntimeError):
    pass


class Store:
    def __init__(self, table, bucket, session=None):
        session = session or boto3.Session()
        config = Config(
            retries={"mode": "standard", "total_max_attempts": 3},
            connect_timeout=5,
            read_timeout=20,
        )
        self.table = session.resource("dynamodb", config=config).Table(table)
        self.client = self.table.meta.client
        self.s3 = session.client("s3", config=config)
        # SES has no idempotency key; never automatically retry a send.
        self.ses = session.client(
            "ses",
            config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=30),
        )
        self.bucket = bucket

    def get(self, pk):
        return self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")

    def put(self, item):
        self.table.put_item(Item=item)

    def all(self, prefix):
        pages = self.client.get_paginator("scan").paginate(
            TableName=self.table.name,
            ConsistentRead=True,
            FilterExpression=Attr("pk").begins_with(prefix),
        )
        return [item for page in pages for item in page["Items"]]

    @contextmanager
    def lease(self, name):
        pk, token = "LOCK#" + name, str(uuid.uuid4())
        try:
            self.table.put_item(
                Item={"pk": pk, "token": token, "until": int(time.time()) + 960},
                ConditionExpression="attribute_not_exists(pk) OR #until < :now",
                ExpressionAttributeNames={"#until": "until"},
                ExpressionAttributeValues={":now": int(time.time())},
            )
        except self.client.exceptions.ConditionalCheckFailedException as exc:
            raise Busy(name) from exc
        try:
            yield
        finally:
            self.table.delete_item(
                Key={"pk": pk},
                ConditionExpression="#token = :token",
                ExpressionAttributeNames={"#token": "token"},
                ExpressionAttributeValues={":token": token},
            )

    def ingest(self, article, baseline, now):
        aliases = [
            "ID#" + article["id"],
            "URL#" + hashlib.sha256(article["url"].encode()).hexdigest(),
        ]
        existing = [self.get(key) for key in aliases]
        if any(existing):
            # Keep both identities, including URL changes and cross-source duplicates.
            target = next(row["article"] for row in existing if row)
            for key, row in zip(aliases, existing, strict=True):
                if not row:
                    self.table.put_item(
                        Item={"pk": key, "article": target, "expires": now + 90 * 86400},
                        ConditionExpression="attribute_not_exists(pk)",
                    )
            return False
        article = dict(
            article,
            pk="ARTICLE#" + article["id"],
            discovered=now,
            baseline=baseline,
            expires=now + 90 * 86400,
        )
        puts = [article] + [
            {"pk": key, "article": article["pk"], "expires": article["expires"]} for key in aliases
        ]
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": row,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    }
                    for row in puts
                ]
            )
        except self.client.exceptions.TransactionCanceledException:
            # Only conditional conflicts count as duplicates, not failed writes.
            if not any(self.get(key) for key in aliases):
                raise
            return False
        return True

    def write_blob(self, key, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = body.encode()
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def write_json(self, key, value):
        self.write_blob(
            key, json.dumps(value, default=lambda x: int(x) if isinstance(x, Decimal) else str(x))
        )

    def read_json(self, key):
        with self.s3.get_object(Bucket=self.bucket, Key=key)["Body"] as body:
            return json.loads(body.read())

    def finalize(self, delivery):
        if delivery["kind"] == "daily":
            for article in self.read_json(delivery["bundle"])["articles"]:
                self.table.update_item(
                    Key={"pk": article["pk"]},
                    UpdateExpression="SET daily_digest = :id",
                    ConditionExpression="attribute_exists(pk)",
                    ExpressionAttributeValues={":id": delivery["pk"]},
                )
        delivery["finalized"] = True
        self.put(delivery)
