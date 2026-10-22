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
#                     passthrough; the home AZ does not offer it: pbox up N kvm-gpu-64 --regions us-west-2
#   pbox up [N] FLAVOR --fallback
#                     home AZ first; if it has no capacity, every other US AZ that has a
#                     fallback site (pbox-fallback.tf), best spot placement score first.
#                     A FALLBACK BOX STARTS WITH AN EMPTY WORK VOLUME, deleted with the box.
#   pbox up [N] FLAVOR --regions us-east-1,us-west-2
#                     the same, searching only these regions' fallback sites
#   pbox down [N]     terminate it, wherever it runs. A home box's work volume is KEPT.
#   pbox status       every box: running where, as which flavor? bought spot or ON-DEMAND? disk? and how the NEXT launch is bought
#   pbox status N     just box N
#   pbox ssh [N]      connect
#   pbox ip [N]       print the IP
#   pbox capacity [N] FLAVOR
#                     spot placement scores (1-10) for the flavor's types in the home AZ and every
#                     fallback AZ; launches nothing
#
# For scripts, `up` exits 0 when the box is up (stdout's last line is
#   "launched box=N region=R az=Z type=T ip=IP work=persistent|fresh", or "running ..." when it already was),
#   2 when no flavor (or an unknown one) was named, printing the menu and launching nothing, 3 when the home AZ
#   had no capacity and --fallback was not given (retry with it), 4 when no fallback AZ had capacity either,
#   and 1 for any other failure.
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
# the flavor's vCPU and memory. All Graviton (arm64, as the AMI is), all offered in us-east-2c; a fallback AZ skips the
# types it does not offer. No Graviton c, m or r type has 24 vCPU, so 32 is the smallest size: nothing is under the
# 24-vCPU floor. The kvm flavors are bare metal (virtualized Graviton has no /dev/kvm) and boot in many minutes.
# kvm-gpu-64 is g5g.metal, the one Graviton metal type with GPUs; us-east-2 does not offer it, so its price is us-west-2's.
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
'

# What each shape is for: the one line an agent reads to choose.
flavor_use() {
  case "$1" in
    compute) echo "CPU-bound work (builds, tests, encoding, simulation): 2 GiB per vCPU, the cheapest per core" ;;
    general) echo "mixed work that needs more memory than compute gives: 4 GiB per vCPU" ;;
    memory)  echo "memory-bound work (big in-memory data, large links, caches): 8 GiB per vCPU" ;;
    storage) echo "local-disk-bound work: fast local NVMe at /mnt/nvme (gone with the box) and 8 GiB per vCPU" ;;
    kvm)     echo "hypervisors (fcvm, firecracker): bare metal with /dev/kvm; boots in many minutes; not for members" ;;
    kvm-gpu) echo "GPU passthrough into fcvm: kvm plus 2x NVIDIA T4G on vfio-pci; not in the home AZ: add --regions us-west-2 (its price is there)" ;;
  esac
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
    --regions)
      shift
      [ -n "${1:-}" ] || { say "--regions needs a list (us-east-1,us-west-2)"; exit 1; }
      ONLY_REGIONS="$1"; FALLBACK=1 ;;
    --regions=*)
      ONLY_REGIONS="${1#--regions=}"; FALLBACK=1
      [ -n "$ONLY_REGIONS" ] || { say "--regions needs a list (us-east-1,us-west-2)"; exit 1; } ;;
    [0-9]*) say "unknown box '$1' (use 1-8)"; exit 1 ;;
    [a-z]*-[0-9]*) FLAVOR="$1" ;;
    *) say "unknown argument '$1' (a box 1-8, a flavor, --fallback, --regions r1,r2)"; exit 1 ;;
  esac
  shift
done
if [ -n "$MEMBER" ] && { [ -n "$BOX" ] || [ -n "$FALLBACK" ]; }; then
  say "your pbox is pbox-$MEMBER: run '$SELF $CMD' with no box number or fallback"
  exit 1
fi
if [ -n "$FALLBACK" ] && [ "$CMD" != "up" ]; then say "--fallback and --regions only apply to 'up'"; exit 1; fi
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
pointer() { # pointer KEY...: the value at that path in the pointer (an object's keys, space-separated), or nothing
  printf '%s' "$POINTER" | python3 -c 'import json, sys
try:
    v = json.load(sys.stdin)
    for k in sys.argv[1:]:
        v = v[k]
except Exception:
    sys.exit(0)
print(" ".join(v) if isinstance(v, dict) else v if isinstance(v, str) else "")' "$@" 2>/dev/null
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

# Run on a GPU box: wait for the boot script, then list each NVIDIA function and fail unless every one is vfio-pci's and
# has its /dev/vfio group node. Single-quoted: it expands on the box.
# shellcheck disable=SC2016
GPU_CHECK='timeout 900 cloud-init status --wait >/dev/null 2>&1; n=0; bad=0
for d in /sys/bus/pci/devices/*; do
  [ "$(cat "$d/vendor")" = 0x10de ] || continue
  n=$((n + 1)); drv=$(readlink "$d/driver"); grp=$(readlink "$d/iommu_group")
  echo "  gpu:   ${d##*/} driver=${drv##*/} iommu_group=${grp##*/}"
  [ "${drv##*/}" = vfio-pci ] && [ -n "$grp" ] && [ -c "/dev/vfio/${grp##*/}" ] || bad=1
done
[ "$n" -gt 0 ] && [ "$bad" -eq 0 ]'

# Wait until the box answers SSH, then report it. $1: what the work volume is ("persistent" or "fresh").
wait_ready() {
  # Metal firmware init takes many minutes on top of the normal boot -- give kvm
  # mode 25 min before declaring failure (virtualized boxes keep the 10 min bound).
  WAIT_TRIES=60
  [ "$MODE" = "kvm" ] && WAIT_TRIES=150
  for i in $(seq 1 "$WAIT_TRIES"); do
    IP="$(box_ip)"
    if [ -n "$IP" ] && ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
         -o BatchMode=yes "ubuntu@$IP" true 2>/dev/null; then
      say "Ready at $IP"
      ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP" \
        'echo "  cores: $(nproc)"; echo "  work:  $(df -h /mnt/work 2>/dev/null | awk "NR==2{print \$2\" total, \"\$4\" free\"}" || echo "not mounted yet")"' >&2 2>/dev/null || true
      if [ "$MODE" = "kvm" ]; then
        # The entire point of metal: prove /dev/kvm exists and make it usable by
        # ubuntu (group change applies from the next SSH session on).
        if ssh -i "$KEY" -o StrictHostKeyChecking=no -o BatchMode=yes "ubuntu@$IP" \
             'test -c /dev/kvm && sudo usermod -aG kvm ubuntu' 2>/dev/null; then
          say "  kvm:   /dev/kvm present; ubuntu in kvm group (applies on next login)"
        else
          say "FAILED: $CHOSEN came up WITHOUT a usable /dev/kvm."
          say "That should be impossible on a *.metal type -- investigate before using."
          exit 1
        fi
      fi
      case "$(type_flavor "$CHOSEN")" in kvm-gpu-*)
        # The other point of a GPU flavor: every NVIDIA function bound to vfio-pci, in an IOMMU group with its
        # /dev/vfio node. The boot script binds them (pbox_gpu_vfio_setup, pbox-flavors.tf), so wait for it first.
        if ssh -i "$KEY" -o StrictHostKeyChecking=no -o BatchMode=yes "ubuntu@$IP" "$GPU_CHECK" >&2; then
          say "  gpu:   every NVIDIA function is on vfio-pci with its /dev/vfio group (list: /etc/pbox-gpu)"
        else
          say "FAILED: $CHOSEN came up without its GPUs on vfio-pci (above; the boot log is /var/log/cloud-init-output.log)."
          exit 1
        fi
        ;;
      esac
      if [ "${2:-}" = "running" ]; then
        echo "running box=$BOX_LABEL region=$REGION az=$CHOSEN_AZ ip=$IP work=$1"
      else
        echo "launched box=$BOX_LABEL region=$REGION az=$CHOSEN_AZ type=$CHOSEN ip=$IP work=$1"
      fi
      exit 0
    fi
    [ $((i % 6)) -eq 0 ] && say "    still booting (${i}0s)..."
    sleep 10
  done
  say "Launched but SSH did not come up in $((WAIT_TRIES / 6)) min; check: $SELF status"
  exit 1
}

# One launch attempt. Sets OUT and returns 0 with the instance id in OUT, or 1; a failure that is not about capacity
# also sets REAL_ERROR, so a permissions bug is not reported as a busy afternoon.
try_launch() { # try_launch TYPE AZ RUN-INSTANCES-ARGS...
  local t="$1" az="$2" rc
  shift 2
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
  # Capacity is the expected failure and is worth distinguishing from a real error
  # (a bad policy, a deleted template) -- otherwise a permissions bug looks exactly
  # like a busy afternoon and gets retried 28 times.
  case "$OUT" in
    *Unsupported*)
      say "    $t is Unsupported in $az (usually: not offered there) -- trying the next one" ;;
    *InsufficientInstanceCapacity*|*InsufficientHostCapacity*|*capacity-not-available*)
      say "    no capacity for $t in $az -- trying the next one" ;;
    *VcpuLimitExceeded*|*MaxSpotInstanceCountExceeded*|*InstanceLimitExceeded*)
      # A regional quota is as final for this region as missing capacity: try elsewhere, not "fix that first".
      say "    over the account's quota for $t in $REGION -- trying the next one" ;;
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
    box_ip
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
        attach_work "$ID" || exit 1
        CHOSEN_AZ="$(box_az)"
        wait_ready persistent running
      fi
      IP="$(box_ip)"
      flavor_note "$(aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
        --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)"
      say "Already running at ${IP:-(no IP yet)} in $REGION"
      echo "running box=$BOX_LABEL region=$REGION az=$(box_az) ip=$IP work=$(box_work)"
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
      # Name tag its IAM policy and its idle watchdog both depend on.
      if try_launch "$T" "$AZ" --launch-template "LaunchTemplateName=$LT,Version=\$Latest"; then
        ID="$OUT"; CHOSEN="$T"; CHOSEN_AZ="$AZ"; break
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
        # The full tag set is passed, not only Name: whether EC2 merges these with the template's tags or replaces
        # them per resource type, the box ends up named and tagged Fallback=true (what `status` and `down` read).
        if try_launch "$T" "$FAZ" --launch-template "LaunchTemplateName=$FB_TEMPLATE,Version=\$Latest" \
             --subnet-id "$SUBNET" \
             --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Fallback,Value=true},{Key=DevEBS,Value=true}]" \
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
    ID="$(box_id)"
    if [ -z "$ID" ]; then
      say "$NAME is already down."
      exit 0
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
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "Box is down. Run: $SELF up ${BOX:+$BOX }FLAVOR  ($SELF flavors)"; exit 1; }
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;

  *)
    awk 'NR >= 3 && /^# CAPACITY IS THE HARD PART/ { exit } NR >= 3' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac
