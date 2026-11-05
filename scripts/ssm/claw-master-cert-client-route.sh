#!/bin/bash
# SSM prepends this to the three client documents, never to the signer.
# Next.js accounts' homes are visible on the host, but their tools and UID
# boundary live in the matching container. Do not touch their homes as host root.
CT_DIR=/etc/nextjs-ct
if [ "${CM_CERT_IN_CONTAINER:-}" != 1 ] && { [ -e "$CT_DIR" ] || [ -L "$CT_DIR" ]; }; then
  CT_ACCOUNT='{{ Account }}'
  if [ -L "$CT_DIR" ] || [ -L "$CT_DIR/config.json" ] || [ ! -f "$CT_DIR/config.json" ] ||
    ! /usr/bin/jq -e --arg account "$CT_ACCOUNT" '.users | type == "object" and has($account)' "$CT_DIR/config.json" >/dev/null; then
    echo "CERT_ROUTE=not-a-configured-container" >&2
    exit 1
  fi
  if [ "$(/usr/bin/machinectl show "$CT_ACCOUNT" -p State --value)" != running ]; then
    echo "CERT_ROUTE=container-not-running" >&2
    exit 1
  fi
  # The SSM agent runs the rendered _script.sh as a file. Stream that exact
  # file, rather than forwarding its parameters or the SSM environment.
  exec /usr/bin/systemd-run --machine="$CT_ACCOUNT" --wait --pipe --collect --quiet --service-type=exec \
    --property=RuntimeMaxSec=120s --setenv=CM_CERT_IN_CONTAINER=1 /bin/bash -s < "$0"
fi
