#!/usr/bin/env bash
# Run on the primary FCVM ARM host. The worker's own profile serves the benchmarks.
set -euo pipefail
set +x
usage() { printf '%s\n' 'usage: benchmark-worker.sh up | status | down INSTANCE_ID | ssh [COMMAND ...]' >&2; exit 2; }
action=${1:-}
[ "$#" -eq 0 ] || shift
case "$action" in
  up|status) [ "$#" -eq 0 ] || usage ;;
  down) [[ "$#" -eq 1 && "$1" =~ ^i-[0-9a-f]{17}$ ]] || usage ;;
  ssh) ;;
  *) usage ;;
esac
umask 077
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
export AWS_PAGER=""
region=us-east-2
account=$(aws sts get-caller-identity --region "$region" --query Account --output text)
[[ "$account" =~ ^[0-9]{12}$ ]] || exit 1
creds=$(aws sts assume-role --region "$region" --role-arn "arn:aws:iam::$account:role/benchmark-worker-operator" \
  --role-session-name benchmark-worker --duration-seconds 900 --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
IFS=$'\t' read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN <<< "$creds"
unset creds
: "${AWS_ACCESS_KEY_ID:?missing operator access key}" "${AWS_SECRET_ACCESS_KEY:?missing operator secret}" "${AWS_SESSION_TOKEN:?missing operator session token}"
# Only AWS subprocesses get the operator credentials. Never pass them to SSH.
operator() {
  AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN \
    aws --region "$region" "$@"
}
export -n AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
request=$action
if [ "$action" = ssh ]; then request=status; fi
payload=$(jq -cn --arg action "$request" --arg instance "${1:-}" \
  '{action:$action} + (if $action == "down" then {instance:$instance} else {} end)')
result=$(operator lambda invoke --function-name benchmark-worker --cli-binary-format raw-in-base64-out --payload "$payload" "$tmp/response")
if jq -e 'has("FunctionError")' <<< "$result" >/dev/null; then
  jq -r '.errorMessage // "benchmark worker control failed"' "$tmp/response" >&2
  exit 1
fi
if [ "$action" != ssh ]; then
  jq . "$tmp/response"
  exit
fi
if [ "$(jq -r .state "$tmp/response")" != running ]; then
  echo 'Benchmark worker is not running; use benchmark-worker.sh up or status.' >&2
  exit 1
fi
iid=$(jq -er .instance "$tmp/response")
ip=$(jq -er .public_ipv4 "$tmp/response")
[[ "$iid" =~ ^i-[0-9a-f]{17}$ && "$ip" =~ ^[0-9.]+$ ]] || exit 1
hostkey=$(operator ec2 get-console-output --instance-id "$iid" --latest --output json |
  jq -r '.Output // ""' |
  awk '/BENCHMARK_HOST_KEY ssh-ed25519 / {sub(/^.*BENCHMARK_HOST_KEY /, ""); sub(/\r$/, ""); key=$0} END {print key}')
if [[ ! "$hostkey" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+$ ]]; then
  echo 'The worker has not published its SSH host key yet. Wait for its first boot.' >&2
  exit 1
fi
printf '%s %s\n' "$ip" "$hostkey" > "$tmp/known_hosts"
ssh-keygen -q -t ed25519 -N '' -f "$tmp/id"
operator ec2-instance-connect send-ssh-public-key --instance-id "$iid" \
  --instance-os-user ubuntu --ssh-public-key "file://$tmp/id.pub" | jq -e '.Success == true' >/dev/null
ssh -F /dev/null -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$tmp/known_hosts" \
  -o GlobalKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -i "$tmp/id" "ubuntu@$ip" "$@"
