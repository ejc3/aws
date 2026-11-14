#!/bin/bash
# The release publisher keeps this bootstrap across image changes.
# Trust the actual boot image, not the configured release or template default.
set -euo pipefail
set +x

approved=${1:?missing approved host image}
[[ "$approved" =~ ^ami-[a-f0-9]{17}$ ]]
imds=http://169.254.169.254/latest
destinations=
read_imds() {
  printf 'X-aws-ec2-metadata-token: %s\n' "$token" |
    /usr/bin/curl --noproxy '*' -fsS --max-time 2 -H @- "$imds/meta-data/$1"
}

# These counts come from the same deployment that sets the agent arguments.
# The exact approved agent numbers its slots by actual GPU and Windows counts.
# Verify that layout before allowing its Minecraft guests, excluding the gold VM.
if token=$(/usr/bin/curl --noproxy '*' -fsS --max-time 2 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$imds/api/token" 2>/dev/null) &&
    [[ -n "$token" ]] &&
    active=$(read_imds ami-id 2>/dev/null) && [[ "$active" == "$approved" ]] &&
    mac=$(read_imds mac 2>/dev/null) && [[ "$mac" =~ ^([a-f0-9]{2}:){5}[a-f0-9]{2}$ ]] &&
    prefix=$(read_imds "network/interfaces/macs/$mac/ipv6-prefix" 2>/dev/null); then
  destinations=$(/usr/bin/python3 - "$prefix" "$2" "$3" "$4" <<'PYTHON'
import ipaddress
import os
import shlex
import sys
from pathlib import Path

try:
    prefix = ipaddress.IPv6Network(sys.argv[1])
    gpus, windows, servers = map(int, sys.argv[2:])
    if prefix.prefixlen != 80 or not (0 < gpus <= 8 and 0 < windows <= 4 and 0 < servers <= 4):
        sys.exit(1)
    args = shlex.split(os.environ["FLEET_AGENT_ARGS"])
    windows_args = shlex.split(os.environ["WINDOWS_AGENT_ARGS"])
    if (args[args.index("--windows") + 1] != str(windows)
            or args[args.index("--minecraft") + 1] != str(servers)
            or "--gpus" in args or "--sysfs-root" in args
            or not windows_args[windows_args.index("--windows-image") + 1]):
        sys.exit(1)
    pci = Path("/sys/bus/pci")
    inventory = 0
    for device in (pci / "drivers/vfio-pci").glob("????:??:??.?"):
        dev = pci / "devices" / device.name
        if (dev / "vendor").read_text().strip() == "0x10de" and (dev / "class").read_text().strip().startswith("0x03"):
            inventory += 1
    if inventory != gpus:
        sys.exit(1)
    first = 2 + gpus + windows
    print(", ".join(str(prefix[i]) for i in range(first, first + servers)))
except (ValueError, KeyError, IndexError, OSError):
    sys.exit(1)
PYTHON
  ) || destinations=
fi
unset token

# Replace only our table, in one transaction. A separate earlier base chain
# denies an unapproved image even if another host rule allows the same packet.
# A known image still goes through the agent's connection limits.
{
  cat <<'NFT'
add table ip6 fcvm_minecraft_java
flush table ip6 fcvm_minecraft_java
add chain ip6 fcvm_minecraft_java input { type filter hook input priority -130; policy accept; }
add chain ip6 fcvm_minecraft_java forward { type filter hook forward priority -130; policy accept; }
add rule ip6 fcvm_minecraft_java input tcp dport 25565 drop
NFT
  if [[ -n "$destinations" ]]; then
    printf 'add rule ip6 fcvm_minecraft_java forward ip6 daddr { %s } tcp dport 25565 return\n' "$destinations"
  fi
  echo 'add rule ip6 fcvm_minecraft_java forward tcp dport 25565 drop'
} | /usr/sbin/nft -f -
