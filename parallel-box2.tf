# parallel-box2.tf
#
# A SECOND on-demand Graviton spot box, so two independent parallel jobs can run at the
# same time -- e.g. one driven from each metal dev box. Same shape as parallel-box.tf:
# disposable spot instance, persistent 100GB work volume that outlives it, reaped by the
# same idle watchdog (parallel-box-watchdog.tf matches both Name tags).
#
# Only the durable half lives here now: the work volume. Box 2's instance is launched
# from its own launch template (parallel-box-launch.tf) exactly like box 1's.
#
# DELIBERATE DUPLICATION, same as jumpbox2.tf duplicates jumpbox.tf: two explicit,
# independent box definitions instead of a count/for_each parameterization. count would
# couple their lifecycles (destroying index 0 renumbers index 1); independent resources
# mean `pbox up 2` / `pbox down 2` can never touch box 1 mid-job. The only intentionally
# SHARED pieces are the security group, key pair, AMI/AZ variables and the watchdog --
# all stateless -- plus the spot-capacity walk in scripts/parallel-box.sh.
#
# Address it as box 2 from any dev box or jumpbox:
#     pbox up 2 | pbox down 2 | pbox ssh 2 | pbox status
#
# COST: the volume is ~$8/month, always. The instance is ~$3.14/hr, only while up.

# Its own persistent disk -- two boxes working one disk is exactly the contention this
# second box exists to avoid. Same prevent_destroy reasoning as parallel_work.
resource "aws_ebs_volume" "parallel_work_2" {
  provider          = aws.west2
  availability_zone = var.parallel_box_az
  size              = 100
  type              = "gp3"
  encrypted         = true

  tags = {
    Name    = "parallel-box-2-work"
    Purpose = "persistent scratch for the second on-demand parallel box"
    # Which copy is the real disk. The Ohio volumes are COPIES made when ohio-pbox.tf was applied, so exactly one of the two
    # is live: the one in local.parallel_box_region. scripts/parallel-box.sh refuses to start a box on a volume that is not
    # live, so a flip back to the other region cannot silently fork the work disk onto stale data.
    Live = local.parallel_box_region == "us-west-2" ? "true" : "false"
  }

  lifecycle {
    prevent_destroy = true
  }
}

output "parallel_box_2_work_volume" {
  description = "Persistent 100GB work volume for box 2 (survives the instance)"
  value       = aws_ebs_volume.parallel_work_2.id
}
