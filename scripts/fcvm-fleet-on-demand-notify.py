"""Forward one accepted-on-demand-launch alarm to the fleet's existing SNS subscribers."""
import json
import os

import boto3


def handler(event, context):
    state = event.get("alarmData", {}).get("state", {})
    if (event.get("source") != "aws.cloudwatch"
            or event.get("alarmArn") != os.environ["ALARM_ARN"]
            or state.get("value") != "ALARM"):
        return {"notified": 0}
    topics = json.loads(os.environ["TOPIC_ARNS"])
    message = ("The coordinator accepted an on-demand GPU host launch. "
               "A g4dn.metal costs $7.824/hour in us-east-1 and runs until the fleet sleeps. "
               "Inspect the coordinator hosts and logs; this is a launch event, not a running-host count.\n\n"
               + json.dumps(event, sort_keys=True))
    for topic in topics:
        boto3.client("sns", region_name=topic.split(":")[3]).publish(
            TopicArn=topic, Subject="fcvm fleet: on-demand GPU host launched", Message=message)
    return {"notified": len(topics)}
