# parallel-box.tf
#
# On-demand 192-core Graviton spot box for embarrassingly parallel work, with a persistent 300GB data disk that OUTLIVES the
# instance. The boxes live in us-east-2 (ohio-pbox.tf: network, volumes, launch templates, watchdog); this file keeps what is
# still us-west-2's.
#
# WHY c8g.48xlarge: 192 vCPU is the ceiling for Graviton across every family (c8g/m8g/r8g/x8g all cap at 48xlarge), and c8g is
# the compute-optimized one, so it is the cheapest per core. Graviton runs one thread per core, so 192 vCPU really is 192 cores
# -- unlike Intel, where 384 vCPU is 192 cores with SMT.
#
# WHERE: they ran in us-west-2d until 2026-10-05 (us-west-1 has two AZs and was capacity-starved for 192-core instances) and
# moved to us-east-2c, about 30% cheaper for the same pool. That site -- its two work volumes, the two snapshots each move took,
# the launch templates and the security group -- was retired afterwards: the data lives on the Ohio volumes.
#
# LIFECYCLE: the INSTANCE is not terraform's; it is launched from the launch template in parallel-box-launch.tf by
# scripts/parallel-box.sh and terminated either by that script or by the idle watchdog.
#
#     scripts/parallel-box.sh up | down | status | ssh      (or `pbox ...` on a dev box)
#
# COST: the instance is the expensive part. Down means $0 compute.

# Second alias for us-west-2. mac-dev.tf already declares one ("mac"), but that name is meaningless here and the Mac config is
# disabled; a distinct alias keeps the unrelated us-west-2 workloads (the I/O box, the GPU box, the idle watchdog) from sharing a
# name.
provider "aws" {
  alias  = "west2"
  region = "us-west-2"
}

# Still used: the I/O box (io-box.tf) launches with it, in us-west-2. The parallel boxes use aws_key_pair.ohio_pbox.
resource "aws_key_pair" "parallel_box" {
  provider   = aws.west2
  key_name   = "fcvm-ec2-west2"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINwtXjjTCVgT9OR3qrnz3zDkV2GveuCBlWFXSOBG2joe fcvm-ec2"
  tags       = { Name = "fcvm-ec2-west2" }
}

output "parallel_box_work_volume" {
  description = "Persistent 300GB work volume (survives the instance), in us-east-2"
  value       = aws_ebs_volume.ohio_parallel_work["1"].id
}
