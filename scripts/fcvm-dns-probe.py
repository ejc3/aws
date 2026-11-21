#!/usr/bin/env python3
"""Measure authoritative and recursive DNS propagation from a metal dev box."""
from concurrent.futures import ThreadPoolExecutor
import re
import subprocess
import time
import uuid

ZONE = "dnstest.fcvm.dev"


def lookup(name, kind, server=None):
    args = ["dig", "+short", "+tries=1", "+time=2"]
    if server:
        args.append("@" + server)
    result = subprocess.run([*args, name, kind], text=True, capture_output=True)
    return result.stdout.strip().splitlines() if not result.returncode else []


def probe(client, timeout=120):
    ids = lookup("_zone-id." + ZONE, "TXT")
    servers = sorted(set(lookup(ZONE, "NS")))
    if len(ids) != 1 or not re.fullmatch(r'"Z[A-Z0-9]+"', ids[0]) or len(servers) != 4:
        raise RuntimeError("cannot resolve the probe zone ID and its four nameservers")
    marker = uuid.uuid4().hex
    name = "probe-" + marker + "." + ZONE
    record = {"Name": name, "Type": "TXT", "TTL": 30, "ResourceRecords": [{"Value": '"' + marker + '"'}]}

    def change(action):
        client.change_resource_record_sets(HostedZoneId=ids[0].strip('"'),
                                          ChangeBatch={"Changes": [{"Action": action, "ResourceRecordSet": record}]})

    start = time.monotonic()
    change("CREATE")
    try:
        # A recursor that asks a lagging authority can cache NXDOMAIN past our whole run.
        with ThreadPoolExecutor(max_workers=len(servers)) as workers:
            for group in (servers, ["1.1.1.1", "8.8.8.8"]):
                pending = set(group)
                deadline = time.monotonic() + timeout
                while pending and time.monotonic() < deadline:
                    ordered = sorted(pending)
                    for server, answers in zip(ordered, workers.map(lambda ns: lookup(name, "TXT", ns), ordered)):
                        if record["ResourceRecords"][0]["Value"] in answers:
                            print(f"{server}: {time.monotonic() - start:.2f} seconds", flush=True)
                            pending.remove(server)
                    if pending:
                        time.sleep(1)
                if pending:
                    raise RuntimeError("timed out waiting for " + ", ".join(sorted(pending)))
    finally:
        change("DELETE")


if __name__ == "__main__":
    import boto3
    probe(boto3.client("route53"))
