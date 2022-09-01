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
            "name": (
                f"{secret_client.secret_path(gcloud_project, name)}/versions/latest"
            ),
        },
    )
    return response.payload.data.decode()

# fmt: off
(
    slack_bot_token,
    slack_signing_secret,
    gkwillie_token,
) = get_secret("approve_button_slack").split("\n")
# fmt: on
bolt_app = slack_bolt.App(token=slack_bot_token, signing_secret=slack_signing_secret)
sent_count = prometheus_client.Counter(
    "sent_offers",
    "Total number of approval offers sent",
)
approved_count = prometheus_client.Counter(
    "approved_prs",
    "Total number of approved PRs",
)
pr_re = re.compile("https://github.com/(athenianco/[^/]+)/pull/(\d+)")
log = logging.getLogger("approve_button")


@bolt_app.message(pr_re)
def message_url(message, say):
    for match in pr_re.finditer(message["text"]):
        repo = match.group(1)
        number = int(match.group(2))
        response = requests.get(
            f"https://api.github.com/repos/{repo}/pulls/{number}",
            headers={
                "Authorization": f"token {gkwillie_token}",
                "Accept": "application/vnd.github.v3.diff",
            },
        )
        if response.ok:
            diff_lines = [line for line in response.text.split("\n") if line[:1] in ("+", "-")]
            if len(diff_lines) > 8:
                diff_lines = diff_lines[:8] + ["..."]
            diff_lines = "\n".join(diff_lines)
            diff = [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{diff_lines}```",
                },
            }]
        else:
            log.error("failed to diff %s#%d: %s", repo, number, response.text)
            diff = []
        say(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Approve this PR? *{repo}#{number}*",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "approve",
                            "style": "primary",
                            "text": {
                                "type": "plain_text",
                                "text": "Approve",
                                "emoji": True,
                            },
                        },
                    ],
                },
                *diff,
            ],
            text=f"Approve this PR? {repo}#{number}",
            metadata={
                "event_type": "approve_offer",
                "event_payload": {
                    "repository": repo,
                    "number": number,
                    "ts": message["ts"],
                },
            },
        )
        sent_count.inc()


@bolt_app.action("approve")
def action_approve(body, ack, say):
    ack()
    message = body["message"]
    metadata = message["metadata"]["event_payload"]

    def fail(reason: str) -> None:
        say(
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Failed to approve *{metadata['repository']}#{metadata['number']}*:",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": reason,
                    },
                },
            ],
            text=f"Failed to approve *{metadata['repository']}#{metadata['number']}*",
        )

    try:
        token = get_secret(f"github_token_{body['user']['id']}")
        response = requests.post(
            f"https://api.github.com/repos/{metadata['repository']}/pulls/{metadata['number']}/reviews",
            headers={"Authorization": f"token {token}"},
            json={"event": "APPROVE"},
        )
    except Exception as e:
        fail(f"{type(e).__name__}: {str(e)}")
        return
    if response.ok:
        bolt_app.client.reactions_add(
            channel=body["channel"]["id"], timestamp=metadata["ts"], name="heavy_check_mark",
        )
        bolt_app.client.chat_delete(channel=body["channel"]["id"], ts=message["ts"])
    else:
        fail(response.text)
        return
    approved_count.inc()


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
    # can configure startup instructions by adding `entrypoint` to app.yaml.
    app.run(host="0.0.0.0", port=8080, debug=True)
