import logging
import os
import re
import sys

import prometheus_client
import requests
import slack_bolt
from flask import Flask, request
from flogging import flogging
from google.cloud import secretmanager
from slack_bolt.adapter.flask import SlackRequestHandler

flogging.setup(logging.INFO, not sys.stdin.isatty())
secret_client = secretmanager.SecretManagerServiceClient()
gcloud_project = os.getenv("GOOGLE_CLOUD_PROJECT")


def get_secret(name: str) -> str:
    response = secret_client.access_secret_version(
        request={
            "name": f"{secret_client.secret_path(gcloud_project, name)}/versions/latest",
        },
    )
    return response.payload.data.decode()


# fmt: off
(
    slack_bot_token,
    slack_signing_secret,
    sentry_token,
) = get_secret("sentry_summary_slack").split("\n")
# fmt: on

bolt_app = slack_bolt.App(token=slack_bot_token, signing_secret=slack_signing_secret)
log = logging.getLogger("sentry_summary")
stats_period_re = re.compile(r"\d+[hd]")
sent_performance_count = prometheus_client.Counter(
    "sent_sentry_performance_reports",
    "Total number of performance reports sent",
)


def format_value(name, value):
    if name in ("p50()", "p95()"):
        return "%.3fs" % (float(value) / 1000)
    if name == "failure_rate()":
        return "%.3f" % float(value)
    return value


@bolt_app.message(re.compile(r"performance|(request.*slow)|(slow.*request)"))
def report_api_performance(message, say):
    if "api" not in message["text"]:
        return
    log.info("message: %s", message)
    thread_ts = message.get("thread_ts", message["ts"])
    if stats_period := stats_period_re.search(message["text"]):
        stats_period = stats_period.group()
    else:
        stats_period = "24h"
    limit = 20
    response = requests.get(
        "https://sentry.io/api/0/organizations/athenianco/events/",
        params=[
            ("environment", "production"),
            ("field", "transaction"),
            ("field", "p50()"),
            ("field", "p95()"),
            ("field", "failure_rate()"),
            ("field", "count_unique(user)"),
            ("field", "count(user)"),
            ("per_page", limit),
            ("project", "1867351"),
            ("query", "transaction.duration:<15m event.type:transaction"),
            ("sort", "-p95"),
            ("statsPeriod", stats_period),
        ],
        headers={
            "content-type": "application/json",
            "authorization": f"bearer {sentry_token}",
        },
        timeout=10,
    )
    if not response.ok:
        log.error("%d: %s", response.status_code, response.text)
        say(text=response.text or "<empty response>", thread_ts=thread_ts)
        return
    fields = [
        {
            "type": "mrkdwn",
            "text": f"*`{item['transaction']}`*\n"
            + "\n".join(
                f"*{key.replace('()', '')}:* {format_value(key, item[key])}"
                for key in (
                    "p50()",
                    "p95()",
                    "failure_rate()",
                    "count_unique(user)",
                    "count(user)",
                )
            ),
        }
        for item in response.json()["data"]
    ]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"API performance report from Sentry, last {stats_period}, "
                    f" sorted by p95 descending, limit {limit}"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": fields[:10],
        },
    ]
    say(blocks=blocks, text=blocks[0]["text"]["text"], thread_ts=thread_ts)
    for i in range(1, (len(fields) + 9) // 10):
        say(
            blocks=[
                {
                    "type": "section",
                    "fields": fields[i * 10 : i * 10 + 10],
                },
            ],
            text=f"(chunk {i})",
            thread_ts=thread_ts,
        )
    sent_performance_count.inc()


app = Flask(__name__)
slack_handler = SlackRequestHandler(bolt_app)


@app.route("/")
def index():
    return (
        prometheus_client.generate_latest(),
        200,
        {"content-type": prometheus_client.CONTENT_TYPE_LATEST},
    )


@app.route("/slack/events", methods=["POST"])
def slack_events():
    return slack_handler.handle(request)


if __name__ == "__main__":
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. You
    # can configure startup instructions by adding `entrypoint` to approve_button.yaml.
    app.run(host="0.0.0.0", port=8080, debug=True)
