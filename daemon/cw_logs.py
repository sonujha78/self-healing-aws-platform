"""
Pushes structured healing-event log lines to CloudWatch Logs, in addition
to the local rotating file log. Best-effort: if CloudWatch is unreachable,
the daemon keeps running off local logs rather than crashing.
"""

import time
import logging

import boto3
from botocore.exceptions import ClientError

from daemon.logging_config import CLOUDWATCH_LOG_GROUP, CLOUDWATCH_LOG_STREAM

logger = logging.getLogger("self_healing_daemon")


class CloudWatchLogPusher:
    def __init__(self, region: str = None):
        self.client = boto3.client("logs", region_name=region)
        self._sequence_token = None
        self._ensure_group_and_stream()

    def _ensure_group_and_stream(self):
        try:
            self.client.create_log_group(logGroupName=CLOUDWATCH_LOG_GROUP)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                logger.warning("Could not create log group: %s", e)

        try:
            self.client.create_log_stream(
                logGroupName=CLOUDWATCH_LOG_GROUP,
                logStreamName=CLOUDWATCH_LOG_STREAM,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                logger.warning("Could not create log stream: %s", e)

    def push(self, message: str):
        event = {
            "logGroupName": CLOUDWATCH_LOG_GROUP,
            "logStreamName": CLOUDWATCH_LOG_STREAM,
            "logEvents": [{"timestamp": int(time.time() * 1000), "message": message}],
        }
        if self._sequence_token:
            event["sequenceToken"] = self._sequence_token

        try:
            resp = self.client.put_log_events(**event)
            self._sequence_token = resp.get("nextSequenceToken")
        except ClientError as e:
            # Sequence token can go stale - refresh and retry once.
            if e.response["Error"]["Code"] == "InvalidSequenceTokenException":
                self._sequence_token = e.response["Error"]["Message"].split()[-1]
                event["sequenceToken"] = self._sequence_token
                try:
                    resp = self.client.put_log_events(**event)
                    self._sequence_token = resp.get("nextSequenceToken")
                except ClientError as e2:
                    logger.warning("Failed to push to CloudWatch Logs after retry: %s", e2)
            else:
                logger.warning("Failed to push to CloudWatch Logs: %s", e)
