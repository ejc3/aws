# io-box.tf
#
# Cheap shared scratch with genuinely fast local I/O. This is deliberately a storage-
# optimised i8ge.large rather than a small general-purpose instance with an expensive
# provisioned EBS volume:
#   - 2 Graviton4 vCPU / 16 GiB RAM
#   - one 1.25 TB third-generation Nitro NVMe SSD
#   - up to 25 Gbps network
#   - $0.0359/hr Spot in us-west-2d when selected (2026-07-26)
#
# The NVMe filesystem is EPHEMERAL. Every stop, Spot interruption, or host loss erases it.
# That is acceptable because this is a shared build/cache/scratch tier, never the only copy
# of source or artifacts. The boot service safely formats a blank instance-store device on
# every start and leaves an existing filesystem alone on ordinary reboots.
#
# us-west-1 <-> us-west-2 latency is about 20ms, so this is for bulk data, caches, and work
# executed on the box -- not a metadata-heavy live source tree. NFS stays private over an
# inter-region VPC peer: SSH is admitted from the two peered VPCs, NFSv4 only from the
# subnets its clients live in (local.io_box_nfs_client_cidrs).

locals {
  io_box_private_ip = cidrhost(data.aws_subnet.io_box.cidr_block, 10)

  # Everyone who mounts /srv/io, by the subnet they live in. The security group and the
  # exports line below both admit exactly these, never a whole VPC: across an inter-region
  # peer a security group cannot reference another group, so a CIDR is the only filter, and
  # the us-west-1 VPC also holds the games router and match engines (games-multiplayer.tf),
  # which must never reach a read-write export.
  #   dev fleet subnets (main.tf local.dev_fleet_subnets): fcvm-metal-arm (either AZ, by
  #     var.firecracker_availability_zone), fcvm-metal-x86 and nextjs-dev (subnet_a)
  #   this box's own us-west-2d subnet: the parallel boxes (parallel-box-launch.tf launches
  #     into subnet-095349c0fcef8c47f, the same subnet as data.aws_subnet.io_box)
  #   the parallel boxes' us-east-2c subnet (ohio-pbox.tf), over a peering connection
  # Adding a client in another subnet means adding that subnet here.
  io_box_nfs_client_cidrs = concat(
    [for s in local.dev_fleet_subnets : s.cidr_block],
    [data.aws_subnet.io_box.cidr_block],
    [aws_subnet.ohio_pbox.cidr_block], # the parallel boxes in us-east-2 (ohio-pbox.tf), over a peering connection
  )
}

data "aws_vpc" "west2_default" {
  provider = aws.west2
  default  = true
}

data "aws_subnet" "io_box" {
  provider = aws.west2
  id       = "subnet-095349c0fcef8c47f" # default VPC, us-west-2d
}

data "aws_ami" "io_box" {
  provider    = aws.west2
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# The VPC CIDRs do not overlap: the dev fleet is 10.0.0.0/16 and the us-west-2
# default VPC is 172.31.0.0/16. Peering avoids putting NFS on a public address and
# gives this box one fixed private endpoint across every stop/start.
resource "aws_vpc_peering_connection" "io_box" {
  vpc_id      = local.vpc_id
  peer_vpc_id = data.aws_vpc.west2_default.id
  peer_region = "us-west-2"
  auto_accept = false

  tags = { Name = "dev-to-io-box" }
}

resource "aws_vpc_peering_connection_accepter" "io_box" {
  provider                  = aws.west2
  vpc_peering_connection_id = aws_vpc_peering_connection.io_box.id
  auto_accept               = true

  tags = { Name = "dev-to-io-box" }
}

# main.tf owns the us-west-1 route table with inline routes, so the west-1 half of
# this peer route lives there too. The west-2 default route table is not otherwise
# Terraform-managed, making a standalone route safe on this side.
resource "aws_route" "io_box_west2" {
  provider                  = aws.west2
  route_table_id            = data.aws_vpc.west2_default.main_route_table_id
  destination_cidr_block    = data.aws_vpc.selected.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.io_box.id

  depends_on = [aws_vpc_peering_connection_accepter.io_box]
}

resource "aws_security_group" "io_box" {
  provider    = aws.west2
  name        = "io-box"
  description = "Private SSH and NFSv4 access to the shared ephemeral I/O box"
  vpc_id      = data.aws_vpc.west2_default.id

  ingress {
    description = "SSH from the dev fleet and other us-west-2 dev boxes"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [
      data.aws_vpc.selected.cidr_block,
      data.aws_vpc.west2_default.cidr_block,
    ]
  }

  # Not the whole of either VPC: see local.io_box_nfs_client_cidrs.
  ingress {
    description = "NFSv4 from the dev fleet and parallel-box subnets only"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = local.io_box_nfs_client_cidrs
  }

  egress {
    description      = "Package installs and normal outbound access"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "io-box" }
}

resource "aws_instance" "io_box" {
  provider = aws.west2

  ami                         = data.aws_ami.io_box.id
  instance_type               = "i8ge.large"
  key_name                    = aws_key_pair.parallel_box.key_name
  availability_zone           = data.aws_subnet.io_box.availability_zone
  subnet_id                   = data.aws_subnet.io_box.id
  private_ip                  = local.io_box_private_ip
  associate_public_ip_address = true # outbound package access; clients use the private IP
  vpc_security_group_ids      = [aws_security_group.io_box.id]
  iam_instance_profile        = aws_iam_instance_profile.dev_ebs_only.name

  # Persistent Spot is the same lifecycle as the metal dev boxes: the idle watchdog
  # stops rather than terminates it, and AWS starts it again when Spot capacity permits.
  # The EBS root survives that stop; the local NVMe intentionally does not.
  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "persistent"
      instance_interruption_behavior = "stop"
    }
  }

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  # The watchdog consumes five-minute periods, which basic monitoring already
  # publishes. Keep this explicit so a copied setting cannot add paid one-minute data.
  monitoring = false

  user_data = <<-INIT
    #!/bin/bash
    set -euxo pipefail

    hostnamectl set-hostname io

    # The existing dev-hop private key stays only on the dev boxes. This host gets its
    # public half for inbound SSH, while the normal west-2 EC2 key pair remains the
    # jumpboxes' admin path.
    install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
    touch /home/ubuntu/.ssh/authorized_keys
    grep -qxF "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" /home/ubuntu/.ssh/authorized_keys || \
      echo "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y nfs-kernel-server fio rsync git htop

    # Nitro instance-store names are not stable (/dev/nvme0n1 vs nvme1n1), so find the
    # drive by its model-specific by-id link. Never guess a device and never force-format:
    # blkid must prove the selected instance-store device is blank first.
    cat > /usr/local/sbin/io-scratch-mount <<'MOUNT'
    #!/bin/bash
    set -euo pipefail

    DEV=""
    for _ in $(seq 1 60); do
      LINK=$(find /dev/disk/by-id -maxdepth 1 -type l \
        -name 'nvme-Amazon_EC2_NVMe_Instance_Storage_*' -print 2>/dev/null | sort | head -1)
      if [ -n "$LINK" ]; then
        DEV=$(readlink -f "$LINK")
        [ -b "$DEV" ] && break
      fi
      DEV=""
      sleep 1
    done

    if [ -z "$DEV" ] || [ ! -b "$DEV" ]; then
      echo "FATAL: no EC2 NVMe instance-store device found; refusing to format anything" >&2
      exit 1
    fi

    if ! blkid "$DEV" >/dev/null 2>&1; then
      echo "blank instance store at $DEV -- creating ephemeral io-scratch filesystem"
      mkfs.ext4 -L io-scratch "$DEV"
    else
      echo "$DEV already has a filesystem -- mounting as-is, NOT formatting"
    fi

    mkdir -p /srv/io
    mountpoint -q /srv/io || mount -o noatime "$DEV" /srv/io
    chmod 1777 /srv/io
    cat > /srv/io/README-EPHEMERAL.txt <<'WARNING'
    This is disposable NVMe scratch. Stop, Spot interruption, or host loss erases it.
    Keep source and unique artifacts on persistent storage.
    WARNING
    chmod 444 /srv/io/README-EPHEMERAL.txt
    MOUNT
    chmod 755 /usr/local/sbin/io-scratch-mount

    cat > /etc/systemd/system/io-scratch.service <<'UNIT'
    [Unit]
    Description=Prepare and mount the ephemeral I/O scratch disk
    After=local-fs.target
    Before=nfs-kernel-server.service

    [Service]
    Type=oneshot
    RemainAfterExit=yes
    ExecStart=/usr/local/sbin/io-scratch-mount

    [Install]
    WantedBy=multi-user.target
    UNIT

    # NFSv4 only: one fixed TCP port, private to the peered VPCs. async is deliberate
    # for throughput because the exported filesystem is explicitly disposable scratch.
    mkdir -p /etc/nfs.conf.d
    cat > /etc/nfs.conf.d/io-box.conf <<'NFSCONF'
    [nfsd]
    vers3 = n
    vers4 = y
    vers4.0 = n
    vers4.1 = y
    vers4.2 = y
    NFSCONF

    # Ubuntu's nfs-kernel-server package creates /etc/exports but not exports.d.
    # Create it explicitly before writing the drop-in (caught on the first real boot).
    # The same client subnets as the security group. user_data is ignored after creation
    # (lifecycle below), so this line reaches a box only when it is rebuilt; until then the
    # security group is what enforces the narrower list.
    mkdir -p /etc/exports.d
    cat > /etc/exports.d/io-box.exports <<'EXPORTS'
    /srv/io ${join(" ", [for c in local.io_box_nfs_client_cidrs : "${c}(rw,async,no_subtree_check,root_squash,fsid=0)"])}
    EXPORTS

    mkdir -p /etc/systemd/system/nfs-kernel-server.service.d
    cat > /etc/systemd/system/nfs-kernel-server.service.d/io-scratch.conf <<'DROPIN'
    [Unit]
    Requires=io-scratch.service
    After=io-scratch.service
    DROPIN

    systemctl daemon-reload
    systemctl enable --now io-scratch.service
    exportfs -ra
    systemctl enable nfs-kernel-server.service
    systemctl restart nfs-kernel-server.service

    echo "io-box ready: 1.25 TB ephemeral NVMe at /srv/io, NFSv4 on ${local.io_box_private_ip}"
  INIT

  tags = {
    Name    = "io-box"
    Purpose = "Shared ephemeral NVMe scratch and build cache"
    DevEBS  = "true"
  }

  lifecycle {
    # Pin the created box. A newer Canonical image or a setup-script edit must not
    # stop the box and silently erase its live scratch filesystem.
    ignore_changes = [
      ami,
      user_data,
      # A STOPPED instance with no Elastic IP reports associate_public_ip_address as
      # false, because AWS releases the auto-assigned address on stop. That field forces
      # replacement, so with the box stopped — its normal resting state — a routine
      # `terraform apply` planned "2 to add, 2 to destroy" and would have DESTROYED the
      # box and its EBS root. Nothing had changed: the config still says true, and the
      # attribute goes back to true the moment it starts.
      #
      # Every other box escapes this only by holding an Elastic IP; us-west-2 has none.
      # An EIP here would be a permanent charge for an address this box does not publish
      # (clients use the private IP; the public one is outbound package access only), so
      # ignore the field instead. It is a launch-time property — changing it in config
      # would require a rebuild anyway, which is the deliberate act, not a side effect of
      # an unrelated apply.
      associate_public_ip_address,
    ]
  }

  depends_on = [
    aws_route.io_box_west2,
    aws_vpc_peering_connection_accepter.io_box,
  ]
}

# Terraform normally treats a stopped aws_instance as valid and will not start it on
# apply. Model the desired running state explicitly so the supported wake path stays
# Terraform-native:
#   terraform apply -var io_box_wake=true -target=aws_ec2_instance_state.io_box
#
# Gated on a variable rather than declared unconditionally. Hardcoding state = "running"
# meant EVERY full apply woke the box — a routine unrelated apply silently started a
# machine that had been deliberately stopped, and billed for it. Waking a box should be
# something you ask for, not something you catch.
#
# Default false removes the resource from state instead of stopping the box: the
# provider's delete for aws_ec2_instance_state is a no-op (DeleteWithoutTimeout:
# schema.NoopContext), so a full apply never touches a box that is already awake and in
# use. That asymmetry is deliberate — Terraform is not a daemon, and the idle Lambda
# owns putting it back to sleep.
variable "io_box_wake" {
  description = "Start the shared I/O box on the next apply. Deliberate, and only ever passed on the command line."
  type        = bool
  default     = false
}

resource "aws_ec2_instance_state" "io_box" {
  count       = var.io_box_wake ? 1 : 0
  provider    = aws.west2
  instance_id = aws_instance.io_box.id
  state       = "running"
}

# Installed on each long-lived dev client and on every new parallel box. Automount avoids
# blocking boot while the Spot box is stopped, and the ten-minute idle unmount normally
# clears handles long before the 12-hour server watchdog runs. The bounded soft retry is
# deliberate for disposable scratch: a client that still holds a cwd/fd when the server
# stops must get an I/O error, not hang forever waiting for a filesystem that was erased.
locals {
  io_box_client_setup = <<-CLIENT
apt-get install -y nfs-common || echo "WARNING: nfs-common install failed"
mkdir -p /mnt/io

cat > /etc/systemd/system/mnt-io.mount <<'MOUNTUNIT'
[Unit]
Description=Shared ephemeral I/O scratch
After=network-online.target
Wants=network-online.target

[Mount]
What=${local.io_box_private_ip}:/
Where=/mnt/io
Type=nfs4
Options=vers=4.2,rw,soft,timeo=50,retrans=2,_netdev,noatime,nconnect=8
TimeoutSec=30
MOUNTUNIT

cat > /etc/systemd/system/mnt-io.automount <<'AUTOMOUNT'
[Unit]
Description=Automount shared ephemeral I/O scratch
# An automount is lazy: it needs no network until something opens /mnt/io, and the mount unit above carries
# that ordering. The default dependencies put an automount Before=local-fs.target, and After=network-online.target
# on top made a cycle (local-fs -> automount -> network-online -> networkd -> cloud-init-local -> local-fs).
# systemd breaks a cycle by deleting start jobs and WHICH ones is random per boot: one boot dropped this
# automount (harmless), the next dropped systemd-networkd, resolved and cloud-init, and the box came up with
# no network at all (2026-10-06, the new fcvm-metal-arm).
DefaultDependencies=no
Before=umount.target
Conflicts=umount.target

[Automount]
Where=/mnt/io
TimeoutIdleSec=10min
DirectoryMode=0755

[Install]
WantedBy=multi-user.target
AUTOMOUNT

systemctl daemon-reload
systemctl enable --now mnt-io.automount || echo "WARNING: io-box automount failed"
  CLIENT
}

output "io_box_private_ip" {
  description = "Fixed private address of the shared I/O box over VPC peering"
  value       = aws_instance.io_box.private_ip
}

output "io_box_ssh_command" {
  description = "SSH to the I/O box from a dev-hop-enabled dev server"
  value       = "ssh io"
}

output "io_box_wake_command" {
  description = "Terraform-native wake command to run on the jumpbox after an idle stop"
  value       = "terraform apply -var io_box_wake=true -target=aws_ec2_instance_state.io_box"
}

output "io_box_mount" {
  description = "Shared ephemeral scratch mount installed on every dev server"
  value       = "/mnt/io -> ${aws_instance.io_box.private_ip}:/ (NFSv4; data is erased whenever the box stops)"
}

# The export file follows the security group, on every start.
#
# The box ignores user_data after creation (lifecycle above), so /etc/exports.d/io-box.exports holds the client list as it was when
# the box was built, and a client added later (the parallel boxes' Ohio subnet) was admitted by the security group and then refused
# by the server. This State Manager association runs when the box registers with SSM after a start and every day while it is up: it
# renders the same line the boot script writes, from the same local.io_box_nfs_client_cidrs, replaces the file only if it differs,
# and reloads the exports. The security group and the exports can no longer disagree, and no one has to edit the box by hand.
# A stopped box is simply not targeted; it converges the moment it is up.
resource "aws_ssm_association" "io_box_exports" {
  provider            = aws.west2
  name                = "AWS-RunShellScript"
  association_name    = "io-box-exports"
  schedule_expression = "rate(1 day)"

  targets {
    key    = "tag:Name"
    values = ["io-box"]
  }

  parameters = {
    commands = <<-SCRIPT
      set -eu
      f=/etc/exports.d/io-box.exports
      mkdir -p /etc/exports.d
      new=$(mktemp)
      cat > "$new" <<'EXPORTS'
      /srv/io ${join(" ", [for c in local.io_box_nfs_client_cidrs : "${c}(rw,async,no_subtree_check,root_squash,fsid=0)"])}
      EXPORTS
      if cmp -s "$new" "$f"; then
        echo "io-box exports already current"
      else
        install -m 644 "$new" "$f"
        exportfs -ra
        echo "io-box exports updated"
      fi
      rm -f "$new"
      exportfs -v | head -5
    SCRIPT
  }
}
