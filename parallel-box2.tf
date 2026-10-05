# parallel-box2.tf
#
# A SECOND on-demand Graviton spot box, so two independent parallel jobs can run at the same time -- e.g. one driven from each
# metal dev box. Same shape as parallel-box.tf, in us-east-2: a disposable spot instance, a persistent 100GB work volume that
# outlives it (ohio-pbox.tf), reaped by the same idle watchdog, launched from its own launch template (parallel-box-launch.tf).
#
# DELIBERATE DUPLICATION, as jumpbox2.tf duplicates jumpbox.tf: two explicit, independent box definitions instead of a
# count/for_each parameterization of the instance. `pbox up 2` / `pbox down 2` can never touch box 1 mid-job. The only
# intentionally SHARED pieces are the security group, key pair, AMI and the watchdog -- all stateless.
#
# Address it as box 2 from any dev box or jumpbox:
#     pbox up 2 | pbox down 2 | pbox ssh 2 | pbox status

output "parallel_box_2_work_volume" {
  description = "Persistent 100GB work volume for box 2 (survives the instance), in us-east-2"
  value       = aws_ebs_volume.ohio_parallel_work["2"].id
}
