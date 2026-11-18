"""Forward the fleet's CloudWatch alarms to the fleet's existing SNS subscribers.

The alarms live in the coordinator's region (us-east-2) and the confirmed subscription lives on the fcvm-fleet-alerts
topic in the regions with a GPU host template, so this function publishes through each topic's own regional endpoint.
Only the alarms named in ALARM_ARNS are forwarded. ALARM always; OK only arrives from an alarm that lists this function
in its ok_actions (a recovery notice), and is forwarded the same way.
"""
import json
import os

import boto3

FORWARDED_STATES = ("ALARM", "OK")


def handler(event, context):
    data = event.get("alarmData", {})
    state = data.get("state", {})
    if (event.get("source") != "aws.cloudwatch"
            or event.get("alarmArn") not in json.loads(os.environ["ALARM_ARNS"])
            or state.get("value") not in FORWARDED_STATES):
        return {"notified": 0}
    name = data.get("alarmName") or event["alarmArn"].rsplit(":", 1)[-1]
    description = data.get("configuration", {}).get("description") or ""
    # SNS subjects are ASCII, one line, under 100 characters.
    subject = f"fcvm fleet: {name} is {state['value']}"
    subject = subject.encode("ascii", "replace").decode()[:99]
    message = (f"{description}\n\nState: {state['value']}\nReason: {state.get('reason', '')}\n\n"
               + json.dumps(event, sort_keys=True))
    topics = json.loads(os.environ["TOPIC_ARNS"])
    for topic in topics:
        boto3.client("sns", region_name=topic.split(":")[3]).publish(
            TopicArn=topic, Subject=subject, Message=message)
    return {"notified": len(topics)}
