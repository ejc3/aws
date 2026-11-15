#!/bin/bash
# The release publisher keeps this bootstrap across image changes.
# Trust the actual boot image and the live netfilter state, not the configured
# release, the template default or what the image's build said.
#
#   fcvm-minecraft-java-guard close
#       before the agent starts: TCP 25565 is dropped to the host and to every guest.
#   fcvm-minecraft-java-guard open APPROVED GPUS WINDOWS SERVERS
#       after the agent starts (fcvm-minecraft-java-open.service): opens 25565 to the
#       four Minecraft guests only once all of these hold, and otherwise leaves it closed
#       and says why:
#       - the booted image is APPROVED (comma-separated) or its build said its servers
#         carry the join limits (MINECRAFT_JOIN_LIMITS=enforced in minecraft.env, written
#         by ejc3/fcvm fleet/ami/host/build.sh); that is a precondition, not the proof;
#       - the slot layout matches the deployment's counts;
#       - the agent's FCVM-MC-JOIN chain is live in ip6tables and holds exactly its two
#         rules, whole: connlimit (4 per source /64) and hashlimit (12/min, burst 6, per
#         source /64), each rejecting with a TCP reset, and nothing else; and FORWARD's
#         first rule is the jump to it for tcp dport 25565 (the agent inserts it first), so
#         no earlier rule can return, accept or jump elsewhere for that traffic. Podman's
#         netavark inserts its own FORWARD jump at position 1 when the renderer containers
#         start, after the agent: when that jump is the only rule ahead, the guard closes
#         Java and moves the agent's jump back to position 1 before it checks again. It
#         moves past nothing else.
#       The agent owns the chain (ejc3/fcvm src/fleet/agent/joinguard.rs installs it at
#       start, before any server); this only reads it, waiting up to
#       FCVM_JAVA_GUARD_WAIT seconds (default 300) for it to appear. Java is IPv6 only:
#       the security group admits no IPv4 to 25565, so there is no IPv4 chain to check.
set -euo pipefail
set +x

say() { echo "fcvm-minecraft-java-guard: $*" >&2; }

# Replace only our table, in one transaction. A separate earlier base chain
# denies an unapproved image even if another host rule allows the same packet.
# An opened image still goes through the agent's connection limits.
apply() {
  {
    cat <<'NFT'
add table ip6 fcvm_minecraft_java
flush table ip6 fcvm_minecraft_java
add chain ip6 fcvm_minecraft_java input { type filter hook input priority -130; policy accept; }
add chain ip6 fcvm_minecraft_java forward { type filter hook forward priority -130; policy accept; }
add rule ip6 fcvm_minecraft_java input tcp dport 25565 drop
NFT
    if [[ -n "${1:-}" ]]; then
      printf 'add rule ip6 fcvm_minecraft_java forward ip6 daddr { %s } tcp dport 25565 return\n' "$1"
    fi
    echo 'add rule ip6 fcvm_minecraft_java forward tcp dport 25565 drop'
  } | /usr/sbin/nft -f -
}

mode=${1:?missing mode: close or open}
if [[ "$mode" == close ]]; then
  apply ""
  exit 0
fi
[[ "$mode" == open ]] || { say "unknown mode $mode"; exit 2; }
shift

# A comma-separated list of the reviewed host images that carry the join limits.
approved=${1:?missing approved host images}
[[ "$approved" =~ ^ami-[a-f0-9]{17}(,ami-[a-f0-9]{17})*$ ]]
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
    active=$(read_imds ami-id 2>/dev/null) && [[ "$active" =~ ^ami-[a-f0-9]{17}$ ]] &&
    { [[ ",$approved," == *",$active,"* ]] ||
      /usr/bin/grep -qx 'MINECRAFT_JOIN_LIMITS=enforced' /etc/fcvm-fleet/minecraft.env 2>/dev/null; } &&
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
if [[ -z "$destinations" ]]; then
  say "closed: the booted image is not approved and carries no join-limit marker, or the slot layout does not match"
  apply ""
  exit 0
fi

# The live limits. Prints nothing when they hold, else the first thing missing.
limits_missing() {
  local join forward
  join=$(/usr/sbin/ip6tables -S FCVM-MC-JOIN 2>/dev/null) || { echo "no FCVM-MC-JOIN chain"; return; }
  forward=$(/usr/sbin/ip6tables -S FORWARD 2>/dev/null) || { echo "cannot read FORWARD"; return; }
  JOIN="$join" FORWARD="$forward" /usr/bin/python3 - <<'PYTHON'
import os

# The agent's rules (ejc3/fcvm src/fleet/agent/joinguard.rs) as ip6tables -S prints them on the
# host image (Ubuntu noble, ip6tables 1.8.10, nf_tables). Compared whole: an extra match, another
# key (destination instead of source), other numbers or another target is not these rules. A
# different iptables that prints them otherwise keeps Java closed.
syn = "-p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN"
reset = "-j REJECT --reject-with tcp-reset"
want = ["-N FCVM-MC-JOIN",
        f"-A FCVM-MC-JOIN {syn} -m connlimit --connlimit-above 4 --connlimit-mask 64 --connlimit-saddr {reset}",
        f"-A FCVM-MC-JOIN {syn} -m hashlimit --hashlimit-above 12/min --hashlimit-burst 6 --hashlimit-mode srcip "
        f"--hashlimit-name fcvm-mc-join --hashlimit-srcmask 64 {reset}"]
jump = "-A FORWARD -p tcp -m tcp --dport 25565 -j FCVM-MC-JOIN"
join = [line.strip() for line in os.environ["JOIN"].splitlines() if line.strip()]
forward = [line.strip() for line in os.environ["FORWARD"].splitlines() if line.startswith("-A FORWARD ")]
if join != want:
    print("FCVM-MC-JOIN is not exactly the agent's connlimit (4 per source /64) and hashlimit (12/min, burst 6, "
          "per source /64) rules with a TCP reset")
elif not forward or forward[0] != jump:
    netavark = '-A FORWARD -m comment --comment "netavark firewall plugin rules" -j NETAVARK_FORWARD'
    if jump in forward and forward[:forward.index(jump)] == [netavark]:
        print("move: netavark's jump is ahead of the jump to FCVM-MC-JOIN")
    else:
        print("FORWARD's first rule is not the jump to FCVM-MC-JOIN for tcp dport 25565")
PYTHON
}

# Put the agent's jump back at position 1, ahead of netavark's. Java is closed first, so
# nothing reaches the servers on 25565 while the jump is briefly absent.
move_jump_first() {
  apply ""
  /usr/sbin/ip6tables -D FORWARD -p tcp --dport 25565 -j FCVM-MC-JOIN &&
    /usr/sbin/ip6tables -I FORWARD 1 -p tcp --dport 25565 -j FCVM-MC-JOIN
}

deadline=$(( $(date +%s) + ${FCVM_JAVA_GUARD_WAIT:-300} ))
while :; do
  missing=$(limits_missing)
  if [[ "$missing" == move:* ]] && [[ -z "${moved:-}" ]]; then
    moved=1
    if move_jump_first; then
      say "moved the jump to FCVM-MC-JOIN ahead of netavark's"
      continue
    fi
    missing="could not move the jump to FCVM-MC-JOIN ahead of netavark's"
  fi
  if [[ -z "$missing" ]]; then
    apply "$destinations"
    say "opened 25565 to $destinations: the join limits are live"
    exit 0
  fi
  if [[ "$(date +%s)" -ge "$deadline" ]]; then
    apply ""
    say "closed: $missing"
    exit 0
  fi
  sleep 3
done
