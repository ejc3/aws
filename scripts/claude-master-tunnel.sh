#!/usr/bin/env bash
#
# claude-master-tunnel -- reach the shared claude-master server from a machine that is outside
# the VPC but has AWS access (a Mac), through an SSM port forward. Nothing is opened to the internet.
#
#   scripts/claude-master-tunnel.sh [LOCAL_PORT]        default 8443; leave it running
#   claude-master connect --server 127.0.0.1:8443 --dir ~/.config/claude-master -- ...
#
# The session type matters. The plain AWS-StartPortForwardingSession connects to the INSTANCE'S
# loopback, where nothing listens (the server binds its private address only). This uses the
# remote-host variant aimed at that private address. The server's certificate also names
# 127.0.0.1, so a client dialling the local end of the tunnel still verifies who it is talking to.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
LOCAL_PORT=${1:-8443}
REMOTE_HOST=${CLAUDE_MASTER_SERVER_HOST:-10.0.1.50}   # claude-master-server.tf: a fixed private address
REMOTE_PORT=${CLAUDE_MASTER_SERVER_PORT:-8443}
[[ $LOCAL_PORT =~ ^[0-9]+$ ]] || { echo "LOCAL_PORT must be a number" >&2; exit 2; }

SERVER_ID=$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:Name,Values=claude-master-server Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ -n "$SERVER_ID" ] && [ "$SERVER_ID" != None ] || { echo "no running claude-master-server instance" >&2; exit 1; }

echo "forwarding 127.0.0.1:$LOCAL_PORT -> $REMOTE_HOST:$REMOTE_PORT through $SERVER_ID (Ctrl-C to stop)"
exec aws ssm start-session --region "$REGION" --target "$SERVER_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=$REMOTE_HOST,portNumber=$REMOTE_PORT,localPortNumber=$LOCAL_PORT"
