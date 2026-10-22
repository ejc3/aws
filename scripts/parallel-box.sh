#!/usr/bin/env bash
#
# Bring the on-demand many-core Graviton boxes up and down. There are EIGHT independent
# boxes (slots 1-8; ohio-pbox.tf, parallel-box-launch.tf), each with its own persistent
# work volume, so eight parallel jobs can run at once.
#
#   pbox flavors      the menu: each flavor's vCPU, memory, local disk, what it is for, rough price
#   pbox up [N] FLAVOR
#                     launch box 1 (or N, 1-8) in the home AZ as FLAVOR (also --flavor FLAVOR).
#                     A FLAVOR IS REQUIRED: pick the smallest that fits the work's shape (CPU-,
#                     memory- or local-disk-bound, or general) and size, e.g. compute-48. It tries
#                     that flavor's instance types in turn. kvm-96 / kvm-192 are bare metal with
#                     /dev/kvm for hypervisors (fcvm/firecracker) and boot in many minutes.
#                     kvm-gpu-64 is the same plus two NVIDIA T4G bound to vfio-pci, for GPU
#                     passthrough; the home AZ does not offer it.
#                     kvm-gpu-96 is an x86 g4dn.metal with eight NVIDIA T4 (about $4/hr spot), launched
#                     with the site's x86_64 image; it reboots once at first boot for intel_iommu=on.
#                     A GPU flavor implies --fallback: home first, then every fallback region that offers
#                     the type (owner, 2026-10-22: the region does not matter for these).
#   pbox up [N] FLAVOR --fallback
#                     home AZ first; if it has no capacity, every other US AZ that has a
#                     fallback site (pbox-fallback.tf), best spot placement score first.
#                     A FALLBACK BOX STARTS WITH AN EMPTY WORK VOLUME, deleted with the box.
#   pbox up [N] FLAVOR --regions us-east-1,us-west-2
#                     the same, searching only these regions' fallback sites
#   pbox up [N] FLAVOR --open
#                     launch it OPEN to the internet: besides the SSH-only security group it gets one that lets
#                     everything in, from anywhere, over IPv4 and IPv6, and a delegated public IPv6 /80 on its
#                     interface, printed as "ipv6: <prefix>/80". Each fcvm VM started on it with
#                       sudo fcvm podman run --network routed --ipv6-prefix <prefix>/80 ...
#                     gets its own public IPv6 address in that /80. EVERY PORT IS PUBLIC: the VMs' addresses and the
#                     box's own IPv4 and IPv6 addresses alike, so anything listening on the box is reachable. Home AZ
#                     only (the fallback sites have no IPv6; --fallback and --regions are refused with it), and not for
#                     a member's box. A box already running without it stays as it is: down, then up --open.
#   pbox up [N] FLAVOR --lease DURATION
#                     keep the box from the idle watchdog until DURATION after launch (90m, 2h, 1h30m; 30m to 12h): it
#                     is not terminated for idleness before then, and is checked as usual after. For interactive and
#                     GPU work with idle stretches. A LEASED BOX COSTS MONEY FOR THE WHOLE LEASE, busy or not (a
#                     kvm-gpu-96 is about $4/hr spot, so a 12h lease is about $50). Set only at launch: a box already
#                     running keeps what it has (down, then up --lease). Not for a member's box.
#   pbox down [N]     terminate it, wherever it runs. A home box's work volume is KEPT. A box launched in the last hour
#                     by another caller (its PboxLaunchedBy tag) is refused unless --force or --instance names it.
#   pbox status       every box: running where, as which flavor? bought spot or ON-DEMAND? disk? open, with which IPv6 /80?
#                     leased until when? and how the NEXT launch is bought
#   pbox status N     just box N
#   pbox ssh [N]      connect
#   pbox ip [N]       print the IP
#   pbox ip|ssh|down [N] --instance i-...
#                     act only if the slot still holds that instance (the id= field of `up`'s last line); refused when
#                     the slot was relaunched since. Scripts carry the id from `up` and never re-resolve a slot by name.
#   pbox capacity [N] FLAVOR
#                     spot placement scores (1-10) for the flavor's types in the home AZ and every
#                     fallback AZ; launches nothing
#
# For scripts, `up` exits 0 when the box is up (stdout's last line is
#   "launched box=N region=R az=Z type=T ip=IP id=i-... work=persistent|fresh", or "running ..." when it already was,
#   with " ipv6_prefix=<prefix>/80" at the end for a box launched with --open). When the box was launched but did not
#   come up (SSH timeout, no /dev/kvm, GPUs not on vfio-pci, no IPv6 prefix), the last line is the same with
#   " failed=<reason>" at the end and the exit is 1, so a caller always learns the instance id it launched,
#   2 when no flavor (or an unknown one) was named, printing the menu and launching nothing, 3 when the home AZ
#   had no capacity and --fallback was not given (retry with it, unless --open), 4 when no fallback AZ had capacity
#   either, and 1 for any other failure.
#
# CAPACITY IS THE HARD PART. A big spot request pinned to one AZ and one instance
# type scores 1/10 for fulfilment; allowing several instance types raises that
# materially. The persistent volumes are AZ-locked, so a home launch tries every type of
# the flavor in the volume's AZ. When that is not enough, a neighbouring flavor (the same
# shape a size down, or another shape of the same size) is often free, and
# --fallback goes to the other US AZs, with a fresh, empty work volume instead of the
# persistent one: the caller decides whether that is acceptable, which is why it is never
# the default. Each attempt is reported, so a failure is visible rather than a silent
# five-minute hang.
#
# THIS SCRIPT DOES NOT RUN TERRAFORM, AND DOES NOT TOUCH THE JUMPBOX.
#
# It used to do both: a dev box SSHed to the jumpbox with a forced-command key and the
# jumpbox ran `terraform apply -target=...`, because state and the admin role live there.
# That gave every dev box a live credential aimed at the jumpbox -- the trust direction
# dev-hop-key.tf exists to prevent, on machines running agents with
# --dangerously-skip-permissions. Dev box -> jumpbox connections are not permitted.
#
# So terraform now owns only what is durable (work volume, SG, key pair, and the LAUNCH
# TEMPLATE that carries the whole launch configuration), and this script supplies the one
# thing terraform cannot know in advance -- which instance type has capacity right now --
# via ec2:RunInstances against that template. The IAM policy behind it
# (parallel-box-launch.tf) allows launching ONLY these eight tagged boxes, and passing only
# the dev-ebs-only role, and pbox-flavor-types (pbox-flavors.tf) only as a flavor's types.
# Terminating was already non-terraform: parallel-box-watchdog.tf
# has always reaped these boxes with tag-scoped ec2:TerminateInstances.
#
# A MEMBER (member-boxes.tf: the account running this is one of MEMBERS) has one box of their
# own, pbox-<person> (member-pbox.tf), with its own work volume, and these commands act on it
# with no box number: `pbox up FLAVOR`, `pbox down`, `pbox status`, `pbox ssh`, `pbox ip`,
# `pbox capacity FLAVOR`, `pbox flavors`. No kvm flavors and no fallback sites for it; it opens with the
# person's own key (fetched once into ~/.ssh/member-box from Secrets Manager).
set -uo pipefail


# THE FLAVOR MENU (pbox-flavors.tf has the same lists, which IAM allows exactly; scripts/test-pbox-flavors.py keeps the
# two equal). One line per flavor: name, vCPU, memory GiB, local NVMe (TB, at least; "-" for none promised), an
# approximate spot $/hr in us-east-2c (measured 2026-10-19: the launch prints each type's live price), then the
# instance types, tried in that order. Spot capacity differs from type to type, so each flavor has several, all with
# the flavor's vCPU and memory. All Graviton (arm64, as the launch templates' AMI is) but g4dn.metal (type_arch), all
# offered in us-east-2c but g5g.metal; a fallback AZ skips the types it does not offer. No Graviton c, m or r type has 24
# vCPU, so 32 is the smallest size: nothing is under the 24-vCPU floor. The kvm flavors are bare metal (virtualized
# Graviton has no /dev/kvm) and boot in many minutes. kvm-gpu-64 is g5g.metal, the one Graviton metal type with GPUs;
# us-east-2 does not offer it, so its price is us-west-2's. kvm-gpu-96 is g4dn.metal, x86_64: launched with the site's
# x86_64 image (--image-id, from /infra/parallel-box), and its first boot reboots once for intel_iommu=on.
FLAVORS='
compute-32   32   64   -    0.35 c8g.8xlarge c9g.8xlarge c8gn.8xlarge c8gb.8xlarge c8gd.8xlarge c9gd.8xlarge c7g.8xlarge c7gd.8xlarge c7gn.8xlarge
compute-48   48   96   -    0.50 c8g.12xlarge c9g.12xlarge c8gn.12xlarge c8gb.12xlarge c8gd.12xlarge c9gd.12xlarge c7g.12xlarge c7gd.12xlarge c7gn.12xlarge
compute-96   96   192  -    0.80 c8g.24xlarge c9g.24xlarge c8gn.24xlarge c8gb.24xlarge c8gd.24xlarge c9gd.24xlarge
compute-192  192  384  -    1.40 c8g.48xlarge c9g.48xlarge c8gn.48xlarge c8gb.48xlarge c8gd.48xlarge c9gd.48xlarge
general-32   32   128  -    0.50 m8g.8xlarge m9g.8xlarge m8gd.8xlarge m9gd.8xlarge m7g.8xlarge m7gd.8xlarge
general-48   48   192  -    0.57 m8g.12xlarge m9g.12xlarge m8gd.12xlarge m9gd.12xlarge m7g.12xlarge m7gd.12xlarge
general-96   96   384  -    1.00 m8g.24xlarge m9g.24xlarge m8gd.24xlarge m9gd.24xlarge
general-192  192  768  -    1.50 m8g.48xlarge m9g.48xlarge m8gd.48xlarge m9gd.48xlarge
memory-32    32   256  -    0.50 r8g.8xlarge r9g.8xlarge r8gd.8xlarge r9gd.8xlarge r7g.8xlarge r7gd.8xlarge
memory-48    48   384  -    0.67 r8g.12xlarge r9g.12xlarge r8gd.12xlarge r9gd.12xlarge r7g.12xlarge r7gd.12xlarge
memory-96    96   768  -    1.20 r8g.24xlarge r9g.24xlarge r8gd.24xlarge r9gd.24xlarge
memory-192   192  1536 -    2.10 r8g.48xlarge r9g.48xlarge r8gd.48xlarge r9gd.48xlarge
storage-32   32   256  7.5  0.55 i8g.8xlarge i4g.8xlarge
storage-48   48   384  11   0.85 i8g.12xlarge i8ge.12xlarge
storage-96   96   768  22   1.70 i8g.24xlarge i8ge.24xlarge
storage-192  192  1536 45   2.00 i8g.48xlarge i8ge.48xlarge
kvm-96       96   192+ -    0.70 c8g.metal-24xl c8gd.metal-24xl m8g.metal-24xl m8gd.metal-24xl
kvm-192      192  384+ -    1.20 c8g.metal-48xl c8gd.metal-48xl c9g.metal-48xl c9gd.metal-48xl m8g.metal-48xl m8gd.metal-48xl
kvm-gpu-64   64   128  -    0.60 g5g.metal
kvm-gpu-96   96   384  -    4.12 g4dn.metal
'

# What each shape is for: the one line an agent reads to choose.
flavor_use() {
  case "$1" in
    compute) echo "CPU-bound work (builds, tests, encoding, simulation): 2 GiB per vCPU, the cheapest per core" ;;
    general) echo "mixed work that needs more memory than compute gives: 4 GiB per vCPU" ;;
    memory)  echo "memory-bound work (big in-memory data, large links, caches): 8 GiB per vCPU" ;;
    storage) echo "local-disk-bound work: fast local NVMe at /mnt/nvme (gone with the box) and 8 GiB per vCPU" ;;
    kvm)     echo "hypervisors (fcvm, firecracker): bare metal with /dev/kvm; boots in many minutes; not for members" ;;
    kvm-gpu) echo "GPU passthrough into fcvm: kvm plus NVIDIA GPUs on vfio-pci. kvm-gpu-64: 2x T4G, arm64, not in the home AZ (its price is us-west-2's). kvm-gpu-96: 8x T4, x86_64, reboots once at first boot. Both search every fallback region on their own (an implied --fallback)" ;;
  esac
}

# The architecture of an instance type: arm64 for Graviton (family, generation, then "g": c8g, i8ge, g5g), else x86_64
# (g4dn). The launch templates boot an arm64 image; an x86_64 type is launched with the site's x86_64 image (x86_image).
type_arch() {
  local graviton='^[a-z]+[0-9]+g[a-z]*\.'
  if [[ "$1" =~ $graviton ]]; then echo arm64; else echo x86_64; fi
}

# The flavors this account may launch: a member gets every one but kvm (member-pbox.tf).
flavor_lines() {
  printf '%s\n' "$FLAVORS" | awk -v member="$MEMBER" 'NF && !(member != "" && $1 ~ /^kvm-/)'
}

flavor_types() { flavor_lines | awk -v f="$1" '$1 == f { for (i = 6; i <= NF; i++) printf "%s%s", $i, (i < NF ? " " : "") }'; }

# The flavor an instance type belongs to (each type is in exactly one), or nothing.
type_flavor() { printf '%s\n' "$FLAVORS" | awk -v t="$1" 'NF { for (i = 6; i <= NF; i++) if ($i == t) print $1 }'; }

# When `up` finds the box already running: say as what, and that a different flavor needs a `down` first.
flavor_note() {
  local f
  f="$(type_flavor "$1")"
  if [ "$f" != "$FLAVOR" ]; then
    say "NOTE: $NAME is already running as $1 (flavor ${f:-none}), not $FLAVOR; it is kept. For $FLAVOR: $SELF down ${BOX:-}, then up again."
  fi
}

# The menu, to stderr. "all" also lists each flavor's instance types.
print_menu() {
  local shape last="" f v m d p rest
  say "Flavors: pick the SMALLEST that fits the work's shape and size (bigger costs more and finds spot capacity less often)."
  if [ -n "$MEMBER" ]; then
    say "  usage: $SELF up FLAVOR        e.g.  $SELF up compute-48"
  else
    say "  usage: $SELF up [N] FLAVOR    e.g.  $SELF up 2 compute-48"
  fi
  while read -r f v m d p rest; do
    shape="${f%-*}"
    if [ "$shape" != "$last" ]; then
      say ""
      say "  $shape: $(flavor_use "$shape")"
      last="$shape"
    fi
    if [ "$d" = "-" ]; then d=""; else d="local NVMe ${d} TB+"; fi
    say "$(printf '    %-12s %3s vCPU  %5s GiB  %-19s ~$%s/hr spot' "$f" "$v" "$m" "$d" "$p")"
    [ "${1:-}" = "all" ] && say "                 types, in order: $rest"
  done < <(flavor_lines)
  say ""
  say "  Prices are approximate (us-east-2c, 2026-10); each launch prints the live spot price of the type it tries."
}
# Which key reaches the box, by what the caller actually holds:
#   - a dev box holds dev_hop and nothing else that reaches another host (dev-hop-key.tf);
#     the launch template's user_data authorizes dev_hop for exactly this reason
#   - a jumpbox holds fcvm-ec2, whose public half AWS injects via the key pair on the
#     instance (aws_key_pair.parallel_box)
# Prefer dev_hop when present so a dev box never depends on the admin key existing.
if [ -f "$HOME/.ssh/dev_hop" ]; then
  KEY="$HOME/.ssh/dev_hop"
else
  KEY="$HOME/.ssh/fcvm-ec2"
fi

SELF="$(basename "${BASH_SOURCE[0]}")"
say() { printf '%s\n' "$*" >&2; }

MEMBERS="colton connor colin skevh" # local.member_box_people
MEMBER_PBOX_REGION="us-east-2"      # local.member_pbox_region
MEMBER="${MEMBER_BOX_USER:-$(id -un 2>/dev/null)}"
case " $MEMBERS " in *" $MEMBER "*) ;; *) MEMBER="" ;; esac
IP_WAIT_TRIES=18 # x10 s: a just-terminated member box may still hold its fixed address
if [ -n "$MEMBER" ]; then
  KEY="$HOME/.ssh/member-box"
fi

# The person's own key (member-boxes.tf), fetched once straight into a 0600 file (never a shell
# variable); their box authorizes it and no other.
member_key() {
  [ -n "$MEMBER" ] && [ ! -s "$KEY" ] || return 0
  install -d -m 700 "$HOME/.ssh"
  if ! (umask 077; aws secretsmanager get-secret-value --region us-west-1 --secret-id "member-box/$MEMBER/ssh-key" \
        --query SecretString --output text > "$KEY.tmp" 2>/dev/null) || [ ! -s "$KEY.tmp" ]; then
    rm -f "$KEY.tmp"
    say "FATAL: cannot read your box key (Secrets Manager member-box/$MEMBER/ssh-key)"; exit 1
  fi
  mv "$KEY.tmp" "$KEY"
}

# ---------------------------------------------------------------------------------
# The command line, read before anything calls AWS: `flavors` and a missing flavor need no AWS at all.
# ---------------------------------------------------------------------------------
ALL_ARGS="$*"
CMD="${1:-status}"
[ $# -gt 0 ] && shift
BOX=""
MODE=""
FLAVOR=""
FALLBACK=""
ONLY_REGIONS=""
OPEN=""
LEASE=""
INSTANCE=""
FORCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    1|2|3|4|5|6|7|8) BOX="$1" ;;
    kvm) say "kvm is a flavor now: kvm-96 or kvm-192 (pbox flavors)"; exit 2 ;;
    --flavor)
      shift
      [ -n "${1:-}" ] || { say "--flavor needs a name (pbox flavors)"; exit 2; }
      FLAVOR="$1" ;;
    --flavor=*) FLAVOR="${1#--flavor=}" ;;
    --fallback) FALLBACK=1 ;;
    --open) OPEN=1 ;;
    --lease)
      shift
      [ -n "${1:-}" ] || { say "--lease needs a duration (90m, 2h, 1h30m)"; exit 1; }
      LEASE="$1" ;;
    --lease=*)
      LEASE="${1#--lease=}"
      [ -n "$LEASE" ] || { say "--lease needs a duration (90m, 2h, 1h30m)"; exit 1; } ;;
    --instance)
      shift
      [ -n "${1:-}" ] || { say "--instance needs an instance id (i-...)"; exit 1; }
      INSTANCE="$1" ;;
    --instance=*) INSTANCE="${1#--instance=}" ;;
    --force) FORCE=1 ;;
    --regions)
      shift
      [ -n "${1:-}" ] || { say "--regions needs a list (us-east-1,us-west-2)"; exit 1; }
      ONLY_REGIONS="$1"; FALLBACK=1 ;;
    --regions=*)
      ONLY_REGIONS="${1#--regions=}"; FALLBACK=1
      [ -n "$ONLY_REGIONS" ] || { say "--regions needs a list (us-east-1,us-west-2)"; exit 1; } ;;
    [0-9]*) say "unknown box '$1' (use 1-8)"; exit 1 ;;
    [a-z]*-[0-9]*) FLAVOR="$1" ;;
    *) say "unknown argument '$1' (a box 1-8, a flavor, --fallback, --regions r1,r2, --open, --lease DURATION, --instance i-..., --force)"; exit 1 ;;
  esac
  shift
done
# A GPU flavor searches every region on its own (owner, 2026-10-22: "i don't care what region any of this runs in"):
# kvm-gpu-64's g5g.metal is not offered in the home AZ at all, and a GPU box is for fcvm passthrough, not for the work
# volume. A member's box has one site and never falls back.
# Not with --open, which launches only at home (below): a kvm-gpu-96 --open box is an x86 g4dn.metal in the home AZ.
case "$FLAVOR" in kvm-gpu-*) [ -z "$MEMBER" ] && [ -z "$OPEN" ] && [ "$CMD" = "up" ] && FALLBACK=1 ;; esac
if [ -n "$MEMBER" ] && { [ -n "$BOX" ] || [ -n "$FALLBACK" ]; }; then
  say "your pbox is pbox-$MEMBER: run '$SELF $CMD' with no box number or fallback"
  exit 1
fi
if [ -n "$FALLBACK" ] && [ "$CMD" != "up" ]; then say "--fallback and --regions only apply to 'up'"; exit 1; fi
# --instance: ip, ssh and down act only on the instance the caller launched, never on whatever holds the slot now (#621).
if [ -n "$INSTANCE" ]; then
  case "$CMD" in ip|ssh|down) ;; *) say "--instance only applies to ip, ssh and down"; exit 1 ;; esac
  [[ "$INSTANCE" =~ ^i-[0-9a-f]{8,17}$ ]] || { say "--instance takes an instance id like i-0123456789abcdef0 (got '$INSTANCE')"; exit 1; }
fi
if [ -n "$FORCE" ] && [ "$CMD" != "down" ]; then say "--force only applies to 'down'"; exit 1; fi
# Who launches: PboxLaunchedBy on the owner's boxes (members' grants pin their tag keys). PBOX_CALLER names a session or
# job; by default it is this host, so `down` from another host within the first hour asks for --force or --instance.
PBOX_CALLER="${PBOX_CALLER:-$(hostname -s 2>/dev/null || echo unknown)}"
PBOX_CALLER="$(printf '%s' "$PBOX_CALLER" | tr -c 'A-Za-z0-9 _.:/=+@-' '_' | cut -c1-200)"
# --open: the owner's boxes, `up`, and the home AZ only. The members' pboxes launch with their SSH-only group, which is
# also all their IAM grant allows (member-pbox.tf). The fallback sites' VPCs have no IPv6, so there is no /80 to delegate.
if [ -n "$OPEN" ]; then
  if [ -n "$MEMBER" ]; then
    say "--open is not available for pbox-$MEMBER: it always launches with its SSH-only security group"; exit 1
  fi
  if [ "$CMD" != "up" ]; then say "--open only applies to 'up'"; exit 1; fi
  if [ -n "$FALLBACK" ]; then
    say "--open launches only in the home AZ: the fallback sites have no IPv6 to delegate. Drop --fallback / --regions."
    exit 1
  fi
fi
# --lease: the owner's boxes and `up` only. The watchdog honours a lease only on the owner's boxes (LEASE_NAMES,
# parallel-box-watchdog.tf): a member's box keeps the idle rule and its hard lifetime, and its grant allows no such tag.
LEASE_MAX_HOURS=12 # the watchdog's LEASE_MAX_HOURS
LEASE_SECS=""
if [ -n "$LEASE" ]; then
  if [ -n "$MEMBER" ]; then
    say "--lease is not available for pbox-$MEMBER: it keeps the 30-minute idle rule and its hard lifetime"; exit 1
  fi
  if [ "$CMD" != "up" ]; then say "--lease only applies to 'up'"; exit 1; fi
  if ! [[ "$LEASE" =~ ^(([0-9]{1,4})h)?(([0-9]{1,5})m)?$ ]]; then
    say "--lease takes a duration like 90m, 2h or 1h30m (got '$LEASE')"; exit 1
  fi
  LEASE_SECS=$(( (10#${BASH_REMATCH[2]:-0} * 60 + 10#${BASH_REMATCH[4]:-0}) * 60 ))
  if [ "$LEASE_SECS" -lt 1800 ]; then
    say "--lease $LEASE is under 30 minutes, which the idle watchdog already gives every box; launch without --lease"; exit 1
  fi
  if [ "$LEASE_SECS" -gt $((LEASE_MAX_HOURS * 3600)) ]; then
    say "--lease $LEASE is over the ${LEASE_MAX_HOURS}-hour maximum (the watchdog would end it at ${LEASE_MAX_HOURS}h from launch)"; exit 1
  fi
fi
LEASE_UNTIL="" # the lease's end in epoch seconds, set just before each launch attempt (start_lease)
start_lease() { [ -z "$LEASE_SECS" ] || LEASE_UNTIL=$(( $(date +%s) + LEASE_SECS )); }
# lease_line LAUNCH_TIME UNTIL: the lease as status prints it ("until HH:MM UTC (Xh Ym left)" or "ended"), capped at
# LaunchTime + LEASE_MAX_HOURS as the watchdog caps it; nothing for no lease or an unparseable tag.
lease_line() {
  local launch until now left
  [[ "${2:-}" =~ ^[0-9]{1,12}$ ]] || return 0
  until="$2"
  if [ -n "$1" ] && [ "$1" != None ] && launch=$(date -u -d "$1" +%s 2>/dev/null) \
     && [ $((launch + LEASE_MAX_HOURS * 3600)) -lt "$until" ]; then
    until=$((launch + LEASE_MAX_HOURS * 3600))
  fi
  now=$(date +%s)
  if [ "$now" -ge "$until" ]; then echo "ended"; return 0; fi
  left=$(( (until - now) / 60 ))
  echo "until $(date -u -d "@$until" +%H:%M) UTC ($((left / 60))h $((left % 60))m left)"
}

if [ -n "$FLAVOR" ] && [ "$CMD" != "up" ] && [ "$CMD" != "capacity" ]; then
  say "a flavor only applies to 'up' and 'capacity'"; exit 1
fi

if [ "$CMD" = "flavors" ]; then
  print_menu all
  exit 0
fi
# `up` and `capacity` need a flavor, and only one this account may launch: nothing is launched without one.
if [ "$CMD" = "up" ] || [ "$CMD" = "capacity" ]; then
  if [ -z "$FLAVOR" ]; then
    say "pbox $CMD needs a FLAVOR: the shape and size of the work."
    say ""
    print_menu
    exit 2
  fi
  TYPES="$(flavor_types "$FLAVOR")"
  if [ -z "$TYPES" ]; then
    if [ -n "$MEMBER" ] && printf '%s\n' "$FLAVORS" | awk -v f="$FLAVOR" '$1 == f { found = 1 } END { exit !found }'; then
      say "the $FLAVOR flavor is not available for pbox-$MEMBER (the kvm flavors are the owner's)."
    else
      say "unknown flavor '$FLAVOR'."
    fi
    say ""
    print_menu
    exit 2
  fi
  case "$FLAVOR" in kvm-*) MODE="kvm" ;; esac
fi

# Which region the boxes live in, and their fallback sites. Terraform publishes both (ohio-pbox.tf, /infra/parallel-box,
# in us-west-1), so moving them is one apply and never a script edit on every dev box. PARALLEL_BOX_REGION overrides the
# home region for a one-off.
# A member's box is always in its own region and has no fallback sites, so it reads no pointer.
POINTER=""
[ -n "$MEMBER" ] || POINTER="$(aws ssm get-parameter --region us-west-1 --name /infra/parallel-box --query Parameter.Value --output text 2>/dev/null)"
pointer() { # pointer KEY...: the value at that path in the pointer (an object's keys or a list, space-separated), or nothing
  printf '%s' "$POINTER" | python3 -c 'import json, sys
try:
    v = json.load(sys.stdin)
    for k in sys.argv[1:]:
        v = v[k]
except Exception:
    sys.exit(0)
print(" ".join(v) if isinstance(v, (dict, list)) else v if isinstance(v, str) else "")' "$@" 2>/dev/null
}
REGION="${PARALLEL_BOX_REGION:-}"
[ -z "$MEMBER" ] || REGION="$MEMBER_PBOX_REGION"
if [ -z "$REGION" ]; then
  REGION="$(pointer region)"
fi
if [ -z "$REGION" ]; then
  say "FATAL: cannot read which region the parallel boxes are in (SSM /infra/parallel-box in us-west-1)."
  say "Retry, or name it yourself:  PARALLEL_BOX_REGION=us-east-2 $SELF $ALL_ARGS"
  exit 1
fi

HOME_REGION="$REGION"


# The fallback sites (pbox-fallback.tf): their template name, and region => AZ => subnet.
FB_TEMPLATE="$(pointer fallback template)"
FB_REGIONS="$(pointer fallback regions)"
fb_subnet() { pointer fallback regions "$1" subnets "$2"; } # fb_subnet REGION AZ
fb_azs() { pointer fallback regions "$1" subnets; }        # fb_azs REGION
fb_x86_image() { pointer fallback regions "$1" x86_image; } # fb_x86_image REGION
# The home site's x86_64 image, for an x86 type (type_arch); none when PARALLEL_BOX_REGION names another region.
HOME_X86_IMAGE=""
[ "$HOME_REGION" != "$(pointer region)" ] || HOME_X86_IMAGE="$(pointer x86_image)"

# The network interface an --open launch passes in place of the template's (ohio-pbox.tf, "open" in the pointer): the
# home subnet, the SSH group and the open group, a public IPv4, one IPv6 address and one delegated IPv6 prefix (a /80).
# Every field the template's interface sets is given here too, so the box gets the same interface whether EC2 merges
# this into the template's interface 0 or replaces it. Read only for the home site: PARALLEL_BOX_REGION naming another
# region has no open interface.
NIC=()
if [ -n "$OPEN" ]; then
  OPEN_SUBNET=""
  OPEN_GROUPS=""
  if [ "$HOME_REGION" = "$(pointer region)" ]; then
    OPEN_SUBNET="$(pointer open subnet)"
    OPEN_GROUPS="$(pointer open security_groups)"
  fi
  # shellcheck disable=SC2086 # the two group ids, one argument each
  if [ -z "$OPEN_SUBNET" ] || [ "$(set -- $OPEN_GROUPS; echo $#)" != 2 ]; then
    say "FATAL: /infra/parallel-box names no open network interface for $HOME_REGION (ohio-pbox.tf not applied yet?); nothing was launched."
    exit 1
  fi
  # shellcheck disable=SC2086 # the two group ids, one argument each
  NIC=(--network-interfaces "$(python3 -c 'import json, sys
print(json.dumps([{"DeviceIndex": 0, "SubnetId": sys.argv[1], "Groups": sys.argv[2:], "AssociatePublicIpAddress": True,
                   "DeleteOnTermination": True, "Ipv6PrefixCount": 1}]))' "$OPEN_SUBNET" $OPEN_GROUPS)")
fi

# image_args TYPE IMAGE REGION: the --image-id an x86_64 type needs (the templates boot arm64), one word per line; nothing
# for an arm64 type. Fails, naming what is missing, when an x86_64 type has no published image there.
image_args() {
  [ "$(type_arch "$1")" = x86_64 ] || return 0
  if [ -z "$2" ]; then
    say "    $1 is x86_64 and /infra/parallel-box names no x86_64 image for $3 (x86_image): not launching it"
    return 1
  fi
  printf '%s\n' --image-id "$2"
}
# FB_REGIONS stays every site, so a box is found (and not launched twice) wherever it runs; --regions narrows only
# where a new launch is searched for.
SEARCH_REGIONS="$FB_REGIONS"
if [ -n "$ONLY_REGIONS" ]; then
  for r in ${ONLY_REGIONS//,/ }; do
    case " $FB_REGIONS " in *" $r "*) ;; *)
      say "no fallback site in '$r' (the sites: ${FB_REGIONS:-none published in /infra/parallel-box})"; exit 1 ;;
    esac
  done
  SEARCH_REGIONS="${ONLY_REGIONS//,/ }"
fi

# Box 1 keeps the original, unnumbered names (tag "parallel-box", launch template "parallel-box") so nothing existing
# moves; boxes 2-8 are suffixed.
set_box() {
  if [ -n "$MEMBER" ]; then
    NAME="pbox-$MEMBER"
  elif [ "$1" = "1" ]; then
    NAME="parallel-box"
  else
    NAME="parallel-box-$1"
  fi
  VOLTAG="$NAME-work" # the volume's Name tag (ohio-pbox.tf)
  LT="$NAME" # launch template name matches the box name (parallel-box-launch.tf)
}
set_box "${BOX:-1}"
BOX_LABEL="${BOX:-1}"
[ -z "$MEMBER" ] || BOX_LABEL="$NAME"

# How the NEXT launch will be bought, read from the launch template itself ($Latest, which is what `up` launches).
# It is a Terraform choice (var.parallel_box_spot) that nothing else shows, so it is looked up rather than
# assumed: "spot", "on-demand", or "unknown" when the template cannot be read.
launch_mode() {
  local m
  m=$(aws ec2 describe-launch-template-versions --region "$REGION" --launch-template-name "$LT" --versions '$Latest' \
    --query 'LaunchTemplateVersions[0].LaunchTemplateData.InstanceMarketOptions.MarketType' --output text 2>/dev/null)
  case "$m" in
    spot) echo spot ;;
    None) echo on-demand ;;   # no market options at all
    *) echo unknown ;;
  esac
}

box_ip() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null | grep -v '^None$' || true
}

box_id() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -v '^None$' || true
}

# instance_ip ID: the public IP of that exact instance while it runs, never of whatever holds the slot now.
instance_ip() {
  aws ec2 describe-instances --region "$REGION" --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null | grep -v '^None$' || true
}

# check_instance: with --instance, refuse unless the slot (as located) still holds that instance (#621).
check_instance() {
  [ -n "$INSTANCE" ] || return 0
  local cur
  cur="$(box_id)"
  if [ "$cur" != "$INSTANCE" ]; then
    say "refusing: $NAME holds ${cur:-no running instance} now, not $INSTANCE (the slot was taken down or relaunched since); nothing was done"
    exit 1
  fi
}

# Point REGION at wherever the box runs: home first, then the fallback regions. A box that is not running anywhere
# leaves REGION at home, where `up` launches it first.
locate_box() {
  local r id
  for r in $(all_regions); do
    id=$(aws ec2 describe-instances --region "$r" \
      --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
      --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -v '^None$' || true)
    if [ -n "$id" ] && [ "${id#i-}" != "$id" ]; then REGION="$r"; return 0; fi
  done
  REGION="$HOME_REGION"
  return 1
}

# Home first, then each fallback region once (us-east-2 is both home and a fallback site for its other AZs).
all_regions() {
  local r seen=" "
  for r in "$HOME_REGION" $FB_REGIONS; do
    case "$seen" in *" $r "*) continue ;; esac
    seen="$seen$r "
    echo "$r"
  done
}

# "fresh" for a box launched at a fallback site (its launch template tags it Fallback=true), else "persistent".
box_work() {
  local f
  # shellcheck disable=SC2016 # a JMESPath literal, not a shell expansion
  f=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[0].Instances[0].Tags[?Key==`Fallback`].Value | [0]' --output text 2>/dev/null)
  [ "$f" = "true" ] && echo fresh || echo persistent
}

# The IPv6 prefix delegated to the box's primary interface (a /80, only on a box launched with --open), or nothing.
# A member's box never has one, so it is not looked up.
box_ipv6_prefix() {
  [ -z "$MEMBER" ] || return 0
  # shellcheck disable=SC2016 # a JMESPath literal, not a shell expansion
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[0].Instances[0].NetworkInterfaces[?Attachment.DeviceIndex==`0`].Ipv6Prefixes[].Ipv6Prefix | [0]' \
    --output text 2>/dev/null | grep -v '^None$' || true
}

# open_host_ipv6 ID: give an --open box's primary interface one IPv6 address of its own, besides its delegated /80. EC2
# takes only one of Ipv6AddressCount and Ipv6PrefixCount when it creates an interface ("You can specify one and only one
# of ipv6AddressCount or ipv6Addresses or ipv6Prefixes or ipv6PrefixCount", checked 2026-10-22), so the launch asks for the
# prefix and the address is assigned here. A failure is reported, not fatal: the /80 is what fcvm's VMs use.
open_host_ipv6() {
  local id="$1" eni out
  eni=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$id" \
    --query 'Reservations[0].Instances[0].NetworkInterfaces[?Attachment.DeviceIndex==`0`].NetworkInterfaceId | [0]' \
    --output text 2>/dev/null)
  if [ -n "$eni" ] && [ "$eni" != "None" ] && out=$(aws ec2 assign-ipv6-addresses --region "$REGION" \
       --network-interface-id "$eni" --ipv6-address-count 1 --query 'AssignedIpv6Addresses[0]' --output text 2>&1); then
    say "    the box's own IPv6 address: $out"
  else
    say "    could not give $id its own IPv6 address (its /80 is unaffected): ${out:-no primary interface found}"
  fi
}

# When `up --lease` finds the box already running: a lease is set at launch only (the launch grant tags at RunInstances and
# nowhere else), so it is not applied.
lease_note() {
  if [ -n "$LEASE" ]; then
    say "NOTE: $NAME is already running; --lease applies only to a new launch, so its lease is unchanged. For a lease: $SELF down ${BOX:-}, then up again with --lease."
  fi
}

# When `up --open` finds the box already running without a prefix: it was not launched open, and is kept as it is.
open_note() {
  if [ -n "$OPEN" ] && [ -z "$(box_ipv6_prefix)" ]; then
    say "NOTE: $NAME is already running without --open (no open security group, no IPv6 /80); it is kept. For --open: $SELF down ${BOX:-}, then up again with --open."
  fi
}

box_az() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text 2>/dev/null | grep -v '^None$' || true
}

work_volume() {
  aws ec2 describe-volumes --region "$REGION" \
    --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].VolumeId' --output text 2>/dev/null | grep -v '^None$' || true
}

status_one() {
  set_box "$1"
  if [ -n "$MEMBER" ]; then echo "$NAME:"; else echo "parallel-box $1:"; fi
  locate_box || true
  IP="$(box_ip)"
  if [ -n "$IP" ]; then
    T=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)
    L=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].InstanceLifecycle' --output text 2>/dev/null)
    case "$L" in spot) BOUGHT="spot" ;; None|"") BOUGHT="ON-DEMAND (about 3x spot)" ;; *) BOUGHT="$L" ;; esac
    F="$(type_flavor "$T")"
    echo "  state:  RUNNING at $IP ($T, flavor ${F:-none}, $BOUGHT)"
    P="$(box_ipv6_prefix)"
    if [ -n "$P" ]; then
      echo "  open:   launched with --open: every port of the box and its VMs is reachable from the internet"
      echo "  ipv6:   $P (fcvm --network routed --ipv6-prefix $P)"
    fi
    # shellcheck disable=SC2016,SC2046 # a JMESPath literal; the two text fields are lease_line's two arguments
    LZ="$(lease_line $(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].[LaunchTime,Tags[?Key==`PboxLeaseUntil`].Value | [0]]' --output text 2>/dev/null))"
    [ -z "$LZ" ] || echo "  lease:  $LZ"
    if [ "$(box_work)" = "fresh" ]; then
      echo "  where:  FALLBACK site $(box_az) -- its /mnt/work is a fresh volume, deleted with the box"
    elif [ "$REGION" != "$HOME_REGION" ] || [ -n "$FB_REGIONS" ]; then
      echo "  where:  home, $(box_az)"
    fi
    ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
      "ubuntu@$IP" 'echo "  cores:  $(nproc)"; echo "  load:   $(uptime | sed "s/.*load average: //")"; echo "  work:   $(df -h /mnt/work | awk "NR==2{print \$3\" used of \"\$2}")"' 2>/dev/null \
      || echo "  (running, but SSH not answering yet)"
  else
    echo "  state:  down (\$0 compute)"
  fi
  REGION="$HOME_REGION"
  echo "  volume: $(aws ec2 describe-volumes --region "$REGION" \
    --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].[VolumeId,Size,State]' --output text 2>/dev/null) (persistent)"
  NEXT=$(launch_mode)
  case "$NEXT" in
    on-demand) echo "  next launch: ON-DEMAND, about 3x the spot price (launch template: parallel_box_spot=false). Apply parallel_box_spot=true to go back to spot." ;;
    *) echo "  next launch: $NEXT (launch template)" ;;
  esac
}

# Spot placement scores (1-10, how likely a request for one of $TYPES is to be fulfilled) of single AZs, as lines
# "score region az". Arguments: the regions. The API answers with zone IDs; they are mapped to this account's names.
placement_scores() {
  local r
  # shellcheck disable=SC2086 # $TYPES is a list of words on purpose
  aws ec2 get-spot-placement-scores --region "$HOME_REGION" --target-capacity 1 --single-availability-zone \
    --region-names "$@" --instance-types $TYPES \
    --query 'SpotPlacementScores[].[Score,Region,AvailabilityZoneId]' --output text 2>/dev/null > "$TMP/scores" || return 1
  for r in "$@"; do
    aws ec2 describe-availability-zones --region "$r" \
      --query 'AvailabilityZones[].[ZoneId,ZoneName]' --output text 2>/dev/null
  done > "$TMP/zones"
  awk 'NR == FNR { name[$1] = $2; next } ($3 in name) { print $1, $2, name[$3] }' "$TMP/zones" "$TMP/scores"
}

# The fallback AZs to try, best score first: lines "score region az". An AZ the scores do not mention (or every AZ,
# when the scores cannot be read) is kept with score "?", after the scored ones, in the published order.
fallback_order() {
  local r az
  # shellcheck disable=SC2086 # a list of regions
  placement_scores $SEARCH_REGIONS > "$TMP/scored" || : > "$TMP/scored"
  for r in $SEARCH_REGIONS; do
    for az in $(fb_azs "$r"); do
      awk -v r="$r" -v z="$az" '$2 == r && $3 == z { print; found = 1 } END { if (!found) print "? " r " " z }' "$TMP/scored"
    done
  done | sort -s -t' ' -k1,1nr
}

# Run on a GPU box: wait for the boot script, then list each NVIDIA function and fail unless every one is vfio-pci's,
# has its /dev/vfio group node, and its whole IOMMU group is usable: VFIO opens a group only when every device in it is
# unbound or on vfio-pci, pci-stub or pcieport (a bridge), so a GPU sharing a group with the NIC or a disk would report
# ready here and then fail in fcvm. An x86 box needs the Intel IOMMU on its kernel command line, which its first boot
# adds before rebooting once (pbox_gpu_iommu_reboot, pbox-flavors.tf): caught between that reboot request and the
# reboot (the marker names this boot), the check exits 75 so the caller waits and asks again. Single-quoted: it expands
# on the box.
# shellcheck disable=SC2016
GPU_CHECK='timeout 900 cloud-init status --wait >/dev/null 2>&1; n=0; bad=0
if [ "$(uname -m)" = x86_64 ] && ! grep -qw intel_iommu=on /proc/cmdline; then
  if [ -s /var/lib/pbox-iommu-reboot ] && [ "$(cat /var/lib/pbox-iommu-reboot)" = "$(cat /proc/sys/kernel/random/boot_id)" ]; then
    echo "  gpu:   rebooting for intel_iommu=on"; exit 75
  fi
  echo "  gpu:   this x86 box did not boot with intel_iommu=on (see /etc/default/grub.d/90-pbox-iommu.cfg)"; bad=1
fi
for d in /sys/bus/pci/devices/*; do
  [ "$(cat "$d/vendor")" = 0x10de ] || continue
  n=$((n + 1)); drv=$(readlink "$d/driver"); grp=$(readlink "$d/iommu_group")
  echo "  gpu:   ${d##*/} driver=${drv##*/} iommu_group=${grp##*/}"
  [ "${drv##*/}" = vfio-pci ] && [ -n "$grp" ] && [ -c "/dev/vfio/${grp##*/}" ] || bad=1
  for m in "/sys/kernel/iommu_groups/${grp##*/}/devices"/*; do
    [ -e "$m" ] || continue; md=$(readlink "$m/driver"); md=${md##*/}
    case "$md" in ""|vfio-pci|pci-stub|pcieport) ;; *) echo "  group ${grp##*/} also holds ${m##*/} on $md: not passable"; bad=1 ;; esac
  done
done
[ "$n" -gt 0 ] && [ "$bad" -eq 0 ]'

# on_box CMD: run CMD on the box over SSH (stdout to stderr) and return its status, waiting while the box reboots: an x86
# GPU box reboots once during its first boot (see GPU_CHECK), so a dropped or refused connection (255), or GPU_CHECK's 75,
# is asked again every 10 s for up to 20 minutes.
on_box() {
  local rc tries
  for tries in $(seq 1 120); do
    ssh -i "$KEY" -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=no \
      -o BatchMode=yes "ubuntu@$IP" "$1" >&2 2>/dev/null
    rc=$?
    case "$rc" in 255|75) ;; *) return "$rc" ;; esac
    [ "$tries" -eq 1 ] && say "    the box is rebooting or not answering (an x86 GPU box reboots once at first boot); waiting..."
    sleep 10
  done
  return "$rc"
}

# The machine-readable last line of `up`, always with the instance id (#621). $1 work, $2 "running" or empty, $3 a
# failure reason (then the caller exits 1).
up_line() {
  if [ "${2:-}" = "running" ]; then
    echo "running box=$BOX_LABEL region=$REGION az=$CHOSEN_AZ ip=${IP:-none} id=$ID work=$1${P:+ ipv6_prefix=$P}${3:+ failed=$3}"
  else
    echo "launched box=$BOX_LABEL region=$REGION az=$CHOSEN_AZ type=$CHOSEN ip=${IP:-none} id=$ID work=$1${P:+ ipv6_prefix=$P}${3:+ failed=$3}"
  fi
}

# Wait until the box answers SSH, then report it. $1: what the work volume is ("persistent" or "fresh").
wait_ready() {
  P=""
  # Metal firmware init takes many minutes on top of the normal boot -- give kvm
  # mode 25 min before declaring failure (virtualized boxes keep the 10 min bound).
  WAIT_TRIES=60
  [ "$MODE" = "kvm" ] && WAIT_TRIES=150
  for i in $(seq 1 "$WAIT_TRIES"); do
    # By the instance it launched (or found), not by the slot's name: another job can relaunch the slot meanwhile.
    IP="$(instance_ip "$ID")"
    if [ -n "$IP" ] && ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
         -o BatchMode=yes "ubuntu@$IP" true 2>/dev/null; then
      say "Ready at $IP"
      ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP" \
        'echo "  cores: $(nproc)"; echo "  work:  $(df -h /mnt/work 2>/dev/null | awk "NR==2{print \$2\" total, \"\$4\" free\"}" || echo "not mounted yet")"' >&2 2>/dev/null || true
      if [ "$MODE" = "kvm" ]; then
        # The entire point of metal: prove /dev/kvm exists and make it usable by
        # ubuntu (group change applies from the next SSH session on).
        if on_box 'test -c /dev/kvm && sudo usermod -aG kvm ubuntu'; then
          say "  kvm:   /dev/kvm present; ubuntu in kvm group (applies on next login)"
        else
          say "FAILED: $CHOSEN came up WITHOUT a usable /dev/kvm."
          say "That should be impossible on a *.metal type -- investigate before using."
          up_line "$1" "${2:-}" no-kvm
          exit 1
        fi
      fi
      case "$(type_flavor "$CHOSEN")" in kvm-gpu-*)
        # The other point of a GPU flavor: every NVIDIA function bound to vfio-pci, in an IOMMU group with its
        # /dev/vfio node. The boot script binds them (pbox_gpu_vfio_setup, pbox-flavors.tf), so wait for it first, and
        # on x86 for its one reboot (on_box waits it out).
        if on_box "$GPU_CHECK"; then
          say "  gpu:   every NVIDIA function is on vfio-pci with its /dev/vfio group (list: /etc/pbox-gpu)"
        else
          say "FAILED: $CHOSEN came up without its GPUs on vfio-pci (above; the boot log is /var/log/cloud-init-output.log)."
          up_line "$1" "${2:-}" gpu-not-vfio
          exit 1
        fi
        ;;
      esac
      # An --open box: its delegated IPv6 /80, which fcvm's routed mode takes as --ipv6-prefix.
      P="$(box_ipv6_prefix)"
      if [ -n "$P" ]; then
        say "  open:  every port of this box and its VMs is reachable from the internet (IPv4 and IPv6)"
        say "  ipv6:  $P (fcvm --network routed --ipv6-prefix $P)"
      elif [ -n "$OPEN" ] && [ "${2:-}" != "running" ]; then
        say "FAILED: $NAME was launched with --open but EC2 reports no IPv6 prefix on its interface. It is running: $SELF status ${BOX:-}, $SELF down ${BOX:-} --instance $ID"
        up_line "$1" "${2:-}" no-ipv6-prefix
        exit 1
      fi
      if [ -n "$LEASE_UNTIL" ] && [ "${2:-}" != "running" ]; then
        say "  lease: $(lease_line "" "$LEASE_UNTIL"): the idle watchdog leaves it running until then, and it costs money until then"
      fi
      up_line "$1" "${2:-}"
      exit 0
    fi
    [ $((i % 6)) -eq 0 ] && say "    still booting (${i}0s)..."
    sleep 10
  done
  say "Launched but SSH did not come up in $((WAIT_TRIES / 6)) min; check: $SELF status ${BOX:-} (act on it with --instance $ID)"
  up_line "$1" "${2:-}" ssh-timeout
  exit 1
}

# The EC2 quotas an instance type counts against, by its family (the letters before the generation digit): its spot vCPU
# quota, its on-demand vCPU quota and the family's name in the Service Quotas console. Every type pbox launches is G or
# Standard; the rest are here so a new flavor is not misreported.
quota_codes() { # quota_codes TYPE -> "SPOT_CODE ON_DEMAND_CODE FAMILY"
  case "${1%%[0-9]*}" in
    g|vt) echo "L-3819A6DF L-DB2E81BA G-and-VT" ;;
    p) echo "L-7212CCBC L-417A185B P" ;;
    inf) echo "L-B5D1601B L-1945791B Inf" ;;
    dl) echo "L-85EED4F7 L-6E869C2A DL" ;;
    trn) echo "L-6B0D517C L-2C3B7624 Trn" ;;
    f) echo "L-88CF9481 L-74FC7D96 F" ;;
    x) echo "L-E3A00192 L-7295265B X" ;;
    *) echo "L-34B43A08 L-1216C47A Standard" ;;
  esac
}

# quota_value CODE: the quota's value in $REGION ("?" when it cannot be read).
quota_value() {
  local v
  v=$(aws service-quotas get-service-quota --region "$REGION" --service-code ec2 --quota-code "$1" \
    --query 'Quota.Value' --output text 2>/dev/null) || v=""
  printf '%s' "${v%.0}" | grep -E '^[0-9]+$' || printf '?'
}

# clear_stale_spot_requests: cancel this region's spot requests that are still open or active although their instance
# is gone (terminated, shutting down, or no longer listed). EC2 can leave a one-time request "active" for half an hour
# after its instance terminates (2026-10-22: parallel-box-4's g5g.metal), and until then it counts against the spot
# quota, so the next box is refused with MaxSpotInstanceCountExceeded. A request whose instance is running or stopped,
# or that has no instance yet (a launch in progress), is left alone; IAM also denies the persistent dev boxes' requests
# (pbox-flavors.tf). Prints how many were cancelled.
clear_stale_spot_requests() {
  local n=0 sir iid st
  while read -r sir iid; do
    [ -n "$sir" ] && [ -n "$iid" ] && [ "$iid" != "None" ] || continue
    st=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$iid" \
      --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null) || st="missing"
    case "$st" in
      terminated|shutting-down|missing|None|"")
        if aws ec2 cancel-spot-instance-requests --region "$REGION" --spot-instance-request-ids "$sir" >/dev/null 2>&1; then
          say "    cancelled $sir, a stale spot request (its instance $iid is ${st:-gone}) still counting against the quota"
          n=$((n + 1))
        else
          say "    could not cancel the stale spot request $sir (instance $iid is ${st:-gone})"
        fi ;;
    esac
  done <<< "$(aws ec2 describe-spot-instance-requests --region "$REGION" --filters Name=state,Values=open,active \
      --query 'SpotInstanceRequests[].[SpotInstanceRequestId,InstanceId]' --output text 2>/dev/null)"
  echo "$n"
}

# One launch attempt. Sets OUT and returns 0 with the instance id in OUT, or 1; a failure that is not about capacity
# also sets REAL_ERROR, so a permissions bug is not reported as a busy afternoon.
try_launch() { # try_launch TYPE AZ RUN-INSTANCES-ARGS...
  local t="$1" az="$2" rc spot_q od_q fam retried=""
  shift 2
  read -r spot_q od_q fam <<< "$(quota_codes "$t")"
  while :; do
    for _ in $(seq 1 "$IP_WAIT_TRIES"); do
      OUT=$(aws ec2 run-instances --region "$REGION" --instance-type "$t" --count 1 "$@" \
        --query 'Instances[0].InstanceId' --output text 2>&1)
      rc=$?
      # Only a member's box has a fixed address, which a just-terminated box may hold for a minute.
      case "$OUT" in
        *InvalidIPAddress.InUse*) say "    the previous $NAME still holds its fixed address; waiting..."; sleep 10 ;;
        *) break ;;
      esac
    done
    if [ "$rc" -eq 0 ] && [ -n "$OUT" ] && [ "${OUT#i-}" != "$OUT" ]; then
      return 0
    fi
    # A quota refusal may be a stale spot request's doing: clear those once and try the same type again.
    case "$OUT" in
      *MaxSpotInstanceCountExceeded*|*VcpuLimitExceeded*)
        if [ -z "$retried" ]; then
          retried=1
          if [ "$(clear_stale_spot_requests)" -gt 0 ]; then
            say "    retrying $t in $az"
            continue
          fi
        fi ;;
    esac
    break
  done
  # Capacity is the expected failure and is worth distinguishing from a real error
  # (a bad policy, a deleted template) -- otherwise a permissions bug looks exactly
  # like a busy afternoon and gets retried 28 times.
  case "$OUT" in
    *Unsupported*)
      say "    $t is Unsupported in $az (usually: not offered there) -- trying the next one" ;;
    *InsufficientInstanceCapacity*|*InsufficientHostCapacity*|*capacity-not-available*)
      say "    no capacity for $t in $az -- trying the next one" ;;
    # A regional quota is as final for this region as missing capacity: try elsewhere, not "fix that first". Each names
    # its limit, so nobody has to guess which of the three it was.
    *MaxSpotInstanceCountExceeded*)
      say "    MaxSpotInstanceCountExceeded: $t would put $REGION over its spot vCPU quota for $fam instances"
      say "      ($spot_q = $(quota_value "$spot_q") vCPU, counting running spot instances AND open or active spot requests) -- trying the next one" ;;
    *VcpuLimitExceeded*)
      say "    VcpuLimitExceeded: $t would put $REGION over its on-demand vCPU quota for $fam instances"
      say "      ($od_q = $(quota_value "$od_q") vCPU) -- trying the next one" ;;
    *InstanceLimitExceeded*)
      say "    InstanceLimitExceeded: $REGION's on-demand instance-count limit for $t (an older per-type limit, not a vCPU quota) -- trying the next one" ;;
    *)
      REAL_ERROR=1
      say "    launch failed for $t in $az:"
      printf '%s\n' "$OUT" | sed 's/^/      /' >&2 ;;
  esac
  say ""
  return 1
}

# attach_work ID: make sure the box's work volume is attached to instance ID, attaching it if
# it is not: the same checks for a fresh launch and for a box an earlier `up` left behind (an
# interrupted launch, a failed first attach), whose boot is waiting for the disk. Two `up`s racing
# to attach to the same box both succeed. A box that cannot get its disk is terminated: it would
# only wait for it, at a many-core price.
attach_work() {
  local id="$1" vol state az att out
  read -r vol state az att <<< "$(aws ec2 describe-volumes --region "$REGION" --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].[VolumeId,State,AvailabilityZone,Attachments[0].InstanceId]' --output text 2>/dev/null | grep '^vol-')"
  [ -n "${vol:-}" ] || { say "FATAL: no work volume tagged $VOLTAG in $REGION"; return 1; }
  for _ in $(seq 1 24); do
    if [ "$att" = "$id" ]; then say "Work volume $vol is attached to $id."; return 0; fi
    [ "$state" = "available" ] && break
    say "    work volume $vol is $state${att:+ (attached to $att)}; waiting for it to be free..."
    sleep 5
    read -r vol state az att <<< "$(aws ec2 describe-volumes --region "$REGION" --filters "Name=tag:Name,Values=$VOLTAG" \
      --query 'Volumes[0].[VolumeId,State,AvailabilityZone,Attachments[0].InstanceId]' --output text 2>/dev/null | grep '^vol-')"
  done
  if [ "$state" = "available" ]; then
    # EC2 rejects an attach while the instance is pending; the boot's mount loop waits for the device.
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$id" 2>/dev/null
    if out=$(aws ec2 attach-volume --region "$REGION" --volume-id "$vol" --instance-id "$id" --device /dev/sdf 2>&1); then
      say "Attached work volume $vol to $id."
      return 0
    fi
    read -r vol state az att <<< "$(aws ec2 describe-volumes --region "$REGION" --filters "Name=tag:Name,Values=$VOLTAG" \
      --query 'Volumes[0].[VolumeId,State,AvailabilityZone,Attachments[0].InstanceId]' --output text 2>/dev/null | grep '^vol-')"
    if [ "$att" = "$id" ]; then say "Work volume $vol is attached to $id."; return 0; fi
    say "FAILED to attach $vol to $id:"; printf '%s\n' "$out" | sed 's/^/  /' >&2
  else
    say "FAILED: work volume $vol is still $state${att:+ (attached to $att)}. Is another $NAME running? $SELF status"
  fi
  if aws ec2 terminate-instances --region "$REGION" --instance-ids "$id" >/dev/null 2>&1; then
    say "Terminated $id; nothing was changed on the volume."
  else
    say "Could not terminate $id either: run $SELF down"
  fi
  return 1
}

# wait_volume_free VOL: before a launch, wait until the work volume is free. Right after `down` it is still detaching
# from the terminated box for a minute or so, and a box launched then would come up without /mnt/work (issue #512).
# Waiting here, before run-instances, costs nothing; a volume still attached after that is another box's, and the
# launch is refused loudly instead.
wait_volume_free() {
  local vol="$1" state att
  for _ in $(seq 1 36); do
    read -r state att <<< "$(aws ec2 describe-volumes --region "$REGION" --volume-ids "$vol" \
      --query 'Volumes[0].[State,Attachments[0].InstanceId]' --output text 2>/dev/null)"
    [ "$state" = "available" ] && return 0
    say "    work volume $vol is ${state:-unknown}${att:+ (attached to $att)}; waiting for it to be free before launching..."
    sleep 5
  done
  say "FAILED: work volume $vol is still ${state:-unknown}${att:+ (attached to $att)} after 3 minutes; nothing was launched. $SELF status"
  return 1
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

case "$CMD" in
  ip)
    locate_box || true
    check_instance
    if [ -n "$INSTANCE" ]; then instance_ip "$INSTANCE"; else box_ip; fi
    ;;

  up)
    member_key
    if locate_box; then
      # A box on its persistent work volume (a member's, or one of the owner's slots at home) is not taken as ready: it
      # may be the box of an interrupted launch, waiting for its volume. A fallback box's fresh volume is its own.
      if [ -n "$MEMBER" ] || [ "$(box_work)" = persistent ]; then
        ID="$(box_id)"
        say "$NAME already exists ($ID); making sure its work volume is attached and it answers SSH."
        CHOSEN="$(aws ec2 describe-instances --region "$REGION" \
          --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
          --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)"
        flavor_note "$CHOSEN"
        open_note
        lease_note
        attach_work "$ID" || exit 1
        CHOSEN_AZ="$(box_az)"
        wait_ready persistent running
      fi
      IP="$(box_ip)"
      flavor_note "$(aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
        --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)"
      open_note
      lease_note
      P="$(box_ipv6_prefix)"
      say "Already running at ${IP:-(no IP yet)} in $REGION"
      echo "running box=$BOX_LABEL region=$REGION az=$(box_az) ip=$IP id=$(box_id) work=$(box_work)${P:+ ipv6_prefix=$P}"
      exit 0
    fi

    VOL="$(work_volume)"
    [ -n "$VOL" ] || { say "FATAL: no work volume tagged $VOLTAG in $REGION"; exit 1; }
    AZ=$(aws ec2 describe-volumes --region "$REGION" --volume-ids "$VOL" \
      --query 'Volumes[0].AvailabilityZone' --output text 2>/dev/null)
    say "Work volume $VOL is in $AZ -- the box launches there (EBS is AZ-locked)."
    wait_volume_free "$VOL" || exit 1
    MODE_NOW=$(launch_mode)
    case "$MODE_NOW" in
      on-demand)
        say "PURCHASE MODE: ON-DEMAND (the launch template says parallel_box_spot=false)."
        say "  This costs about 3x spot, and the \"spot\" figure shown per type below is for reference only, NOT what you pay."
        say "  To go back to spot, an admin applies parallel_box_spot=true; a later apply without the variable also does."
        ;;
      spot) say "PURCHASE MODE: spot. Spot capacity differs from type to type; trying each of $FLAVOR's types in turn." ;;
      *) say "PURCHASE MODE: unknown (could not read the launch template). Trying each type in turn." ;;
    esac
    if [ -n "$OPEN" ]; then
      say "OPEN: the box gets the open security group (everything in, from anywhere) and a delegated IPv6 /80."
    fi
    if [ -n "$LEASE" ]; then
      say "LEASE: $LEASE from launch. The idle watchdog leaves the box running until then, and it costs money the whole time."
    fi
    say ""

    ID=""
    REAL_ERROR=""
    for T in $TYPES; do
      CORES=$(aws ec2 describe-instance-types --region "$REGION" --instance-types "$T" \
        --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null)
      # head -1: --max-items makes the CLI emit a pagination token on a SECOND line, which
      # otherwise lands in the price and prints as "$2.219800\nNone/hr".
      PRICE=$(aws ec2 describe-spot-price-history --region "$REGION" --instance-types "$T" \
        --product-descriptions "Linux/UNIX" --availability-zone "$AZ" --max-items 1 \
        --query 'SpotPriceHistory[0].SpotPrice' --output text 2>/dev/null | head -1)
      if [ "$MODE_NOW" = "spot" ]; then
        say "--> trying $T (${CORES:-?} cores, \$${PRICE:-?}/hr) in $AZ ..."
      else
        # on-demand, or unknown because the template could not be read: the spot figure is a reference, never the price
        say "--> trying $T (${CORES:-?} cores, $MODE_NOW; spot reference \$${PRICE:-?}/hr, NOT necessarily what you pay) in $AZ ..."
      fi

      # Everything except the instance type comes from the launch template, so a typo
      # here cannot land the box in the wrong subnet, on the wrong AMI, or without the
      # Name tag its IAM policy and its idle watchdog both depend on. The one exception is
      # an x86_64 type's image (image_args), which IAM pins by exact ARN like the template's.
      if ! IMG_OUT="$(image_args "$T" "$HOME_X86_IMAGE" "$REGION")"; then REAL_ERROR=1; say ""; continue; fi
      read -r -d '' -a IMG <<< "$IMG_OUT"
      # --open replaces the template's interface 0 with the open one (NIC, above); IAM allows only this subnet and these groups.
      # A lease is one more instance tag. EC2 adds a launch's tags to the template's; Name and DevEBS (which the IAM grants
      # read) are repeated so they hold whichever way the two combine. The launch grant allows any tag key at RunInstances
      # (parallel-box-launch.tf).
      # The owner's boxes also carry PboxLaunchedBy (who launched it, for `down`); a member's grant pins its tag keys
      # (member-pbox.tf), so a member's launch passes no extra tags.
      TAGS=()
      start_lease
      if [ -z "$MEMBER" ]; then
        EXTRA_TAGS="{Key=PboxLaunchedBy,Value=$PBOX_CALLER}"
        [ -z "$LEASE" ] || EXTRA_TAGS="$EXTRA_TAGS,{Key=PboxLeaseUntil,Value=$LEASE_UNTIL}"
        TAGS=(--tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=DevEBS,Value=true},$EXTRA_TAGS]")
      fi
      if try_launch "$T" "$AZ" --launch-template "LaunchTemplateName=$LT,Version=\$Latest" ${IMG[@]+"${IMG[@]}"} ${NIC[@]+"${NIC[@]}"} ${TAGS[@]+"${TAGS[@]}"}; then
        ID="$OUT"; CHOSEN="$T"; CHOSEN_AZ="$AZ"
        [ -z "$OPEN" ] || open_host_ipv6 "$ID"
        break
      fi
    done

    if [ -n "$ID" ]; then
      say ""
      say "Launched $CHOSEN as $ID. Attaching the work volume..."

      # The volume must be attached AFTER the instance exists -- a launch template cannot
      # carry a pre-existing volume -- and EC2 rejects an attach while the instance is
      # still pending. The boot-time mount loop waits for the device, so it is racing this
      # attach and wins as long as we do it promptly.
      # The same routine for every box: wait for the volume, attach it, and terminate a box that cannot get it.
      attach_work "$ID" || exit 1

      say "Waiting for SSH and the work disk to mount..."
      wait_ready persistent
    fi

    say ""
    case "$MODE_NOW" in
      spot|on-demand) say "FAILED: no ${MODE_NOW} capacity in $AZ for any of: $TYPES" ;;
      *) say "FAILED: no capacity in $AZ for any of: $TYPES (purchase mode unknown)" ;;
    esac
    if [ -n "$REAL_ERROR" ]; then
      say "At least one launch failed for a reason other than capacity (above): fix that first."
      exit 1
    fi

    if [ -n "$MEMBER" ]; then
      say "Capacity changes hour to hour: try again later, or another flavor that still fits the work"
      say "  (\`$SELF flavors\`; \`$SELF capacity FLAVOR\` shows the spot placement score)."
      exit 3
    fi
    if [ -n "$OPEN" ]; then
      say "Options:"
      say "  - retry later; capacity fluctuates hour to hour"
      say "  - another flavor that still fits the work ($SELF flavors;  $SELF capacity ${BOX:-1} FLAVOR  scores one without launching)"
      say "  --open launches only in the home AZ (the fallback sites have no IPv6), so --fallback is not an option with it."
      exit 3
    fi
    if [ -z "$FALLBACK" ]; then
      say "Options:"
      say "  - retry later; capacity fluctuates hour to hour"
      say "  - another flavor that still fits the work: the same shape a size down, or another shape of the same size"
      say "      ($SELF flavors;  $SELF capacity ${BOX:-1} FLAVOR  scores one without launching)"
      say "  - launch in another US AZ with an EMPTY work volume (deleted with the box):"
      say "      $SELF up ${BOX:-1} $FLAVOR --fallback      (or --regions us-east-1,us-west-2)"
      say "    \`$SELF capacity ${BOX:-1} $FLAVOR\` shows which AZs score best."
      exit 3
    fi

    # ------------------------------------------------------------- fallback sites
    if [ -z "$SEARCH_REGIONS" ] || [ -z "$FB_TEMPLATE" ]; then
      say "FAILED: no fallback sites are published in /infra/parallel-box (pbox-fallback.tf not applied?)."
      exit 4
    fi
    say ""
    say "FALLBACK: trying the other US AZs. The box gets a FRESH, EMPTY work volume there, deleted with the box;"
    say "  nothing from the home volume ($VOL) is on it, and /srv/io is not mounted."
    fallback_order > "$TMP/order"
    say "AZs by spot placement score (10 = most likely), for any of the types:"
    sed 's/^/    /' "$TMP/order" >&2
    say ""

    while read -r SCORE R FAZ <&3; do
      REGION="$R"
      SUBNET="$(fb_subnet "$R" "$FAZ")"
      [ -n "$SUBNET" ] || continue
      # Only the types this AZ offers (us-west-1 has no storage types, for one). When the lookup itself fails, every
      # type is tried: a missing one is a quick "Unsupported".
      # shellcheck disable=SC2086 # $TYPES is a list of words on purpose
      if OFFERED="$(aws ec2 describe-instance-type-offerings --region "$R" --location-type availability-zone \
          --filters "Name=location,Values=$FAZ" "Name=instance-type,Values=${TYPES// /,}" \
          --query 'InstanceTypeOfferings[].InstanceType' --output text 2>/dev/null)"; then
        OFFERED=" $(printf '%s' "$OFFERED" | tr '\t\n' '  ') "
        if [ -z "${OFFERED// /}" ] || [ "${OFFERED// /}" = "None" ]; then
          say "--> $FAZ ($R, score $SCORE) offers none of $FLAVOR's types; skipping"
          continue
        fi
      else
        OFFERED=""
      fi
      say "--> $FAZ ($R, score $SCORE)"
      for T in $TYPES; do
        if [ -n "$OFFERED" ] && [ "${OFFERED#* "$T" }" = "$OFFERED" ]; then continue; fi
        say "--> trying $T in $FAZ ..."
        if ! IMG_OUT="$(image_args "$T" "$(fb_x86_image "$R")" "$R")"; then REAL_ERROR=1; say ""; continue; fi
        read -r -d '' -a IMG <<< "$IMG_OUT"
        start_lease
        # The lease tag is built apart: a `}` inside ${LEASE:+...} ends the expansion early and leaves a stray brace.
        LEASE_TAG=""
        [ -z "$LEASE" ] || LEASE_TAG=",{Key=PboxLeaseUntil,Value=$LEASE_UNTIL}"
        # The full tag set is passed, not only Name: whether EC2 merges these with the template's tags or replaces
        # them per resource type, the box ends up named and tagged Fallback=true (what `status` and `down` read).
        if try_launch "$T" "$FAZ" --launch-template "LaunchTemplateName=$FB_TEMPLATE,Version=\$Latest" \
             --subnet-id "$SUBNET" ${IMG[@]+"${IMG[@]}"} \
             --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Fallback,Value=true},{Key=DevEBS,Value=true},{Key=PboxLaunchedBy,Value=$PBOX_CALLER}$LEASE_TAG]" \
                                  "ResourceType=volume,Tags=[{Key=Name,Value=$NAME},{Key=Role,Value=fallback}]" \
                                  "ResourceType=network-interface,Tags=[{Key=Name,Value=$NAME}]"; then
          ID="$OUT"; CHOSEN="$T"; CHOSEN_AZ="$FAZ"; break 2
        fi
      done
    done 3< "$TMP/order"

    if [ -n "$ID" ]; then
      say ""
      say "Launched $CHOSEN as $ID in $CHOSEN_AZ ($REGION), with a fresh work volume."
      say "Waiting for SSH and the work disk to mount..."
      wait_ready fresh
    fi
    REGION="$HOME_REGION"
    if [ -n "$REAL_ERROR" ]; then
      say "FAILED: no fallback launch succeeded, and at least one failed for a reason other than capacity (above)."
      exit 1
    fi
    say "FAILED: no capacity in the home AZ or any fallback AZ ($SEARCH_REGIONS) for any of: $TYPES"
    exit 4
    ;;

  capacity)
    VOL="$(work_volume)"
    HOME_AZ=$(aws ec2 describe-volumes --region "$REGION" --volume-ids "$VOL" \
      --query 'Volumes[0].AvailabilityZone' --output text 2>/dev/null)
    say "Spot placement scores (1-10) for one $FLAVOR instance, any of: $TYPES"
    # shellcheck disable=SC2046 # a list of regions
    if ! placement_scores $(all_regions) > "$TMP/all"; then
      say "FAILED to read spot placement scores (ec2:GetSpotPlacementScores)."
      exit 1
    fi
    sort -s -t' ' -k1,1nr "$TMP/all" | while read -r SCORE R Z; do
      if [ "$Z" = "$HOME_AZ" ]; then
        echo "$SCORE $R $Z home (persistent work volume)"
      elif [ -n "$(fb_subnet "$R" "$Z")" ]; then
        echo "$SCORE $R $Z fallback (fresh work volume)"
      fi
    done
    ;;

  down)
    locate_box || true
    check_instance
    ID="$(box_id)"
    if [ -z "$ID" ]; then
      say "$NAME is already down."
      exit 0
    fi
    # A box another caller launched in the last hour is probably someone's work in progress (#621): refuse unless the
    # caller names it (--instance) or insists (--force).
    if [ -z "$INSTANCE" ] && [ -z "$FORCE" ]; then
      read -r BY LT_AT <<< "$(aws ec2 describe-instances --region "$REGION" --instance-ids "$ID" \
        --query 'Reservations[0].Instances[0].[Tags[?Key==`PboxLaunchedBy`].Value | [0], LaunchTime]' --output text 2>/dev/null)"
      if [ -n "$BY" ] && [ "$BY" != "None" ] && [ "$BY" != "$PBOX_CALLER" ]; then
        AGE_MIN=$(( ( $(date +%s) - $(date -d "$LT_AT" +%s 2>/dev/null || echo 0) ) / 60 ))
        if [ "$AGE_MIN" -lt 60 ]; then
          say "refusing: $NAME ($ID) was launched ${AGE_MIN} min ago by $BY, not by $PBOX_CALLER; it is probably someone else's."
          say "If it is yours: $SELF down ${BOX:-} --instance $ID   (or --force)"
          exit 1
        fi
      fi
    fi
    WORK="$(box_work)"
    say "Terminating $NAME ($ID) in $REGION."
    # Terminate detaches the work volume on its own; delete_on_termination is false for
    # it (it is not part of the launch template's block device mappings at all), so the
    # disk survives. Only the 30GB root goes away, which is what it is for. A fallback
    # box's fresh work volume IS in its template, and goes with it.
    if ! aws ec2 terminate-instances --region "$REGION" --instance-ids "$ID" >/dev/null 2>&1; then
      say "FAILED to terminate $ID"
      exit 1
    fi
    if [ "$WORK" = "fresh" ]; then
      say "Down. It was a fallback box: its fresh work volume is deleted with it."
    else
      say "Down. Volume retained: $(work_volume)"
    fi
    ;;

  status)
    # No explicit box -> show every slot; a numbered ask shows just that one. A member has one.
    member_key
    if [ -n "$MEMBER" ]; then
      status_one "$NAME"
    elif [ -z "$BOX" ]; then
      for n in 1 2 3 4 5 6 7 8; do status_one "$n"; done
    else
      status_one "$BOX"
    fi
    ;;

  ssh)
    member_key
    locate_box || true
    check_instance
    if [ -n "$INSTANCE" ]; then IP="$(instance_ip "$INSTANCE")"; else IP="$(box_ip)"; fi
    [ -n "$IP" ] || { say "Box is down. Run: $SELF up ${BOX:+$BOX }FLAVOR  ($SELF flavors)"; exit 1; }
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;

  *)
    awk 'NR >= 3 && /^# CAPACITY IS THE HARD PART/ { exit } NR >= 3' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac
