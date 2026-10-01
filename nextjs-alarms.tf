# Pressure warnings for the shared dev box.
#
# On 2026-08-16 this box became unusable and nobody was told: memory ran out, the kernel
# paged to /swapfile, and swap-in reads pinned the gp3 volume at its 125 MB/s ceiling. With
# the disk saturated, sshd could not finish a handshake and BOTH cloudflared tunnels dropped
# to zero connections. The instance status check stayed "ok" throughout -- from AWS's point
# of view the machine was fine -- so no existing alarm could have caught it.
#
# These alarms watch the signals that actually moved. Thresholds are deliberately below the
# level that caused the outage, so the warning arrives while the box is still usable.

locals {
  nextjs_alarm_enabled = var.enable_nextjs_dev ? 1 : 0
}

# The signature of the outage: sustained queueing on the root volume. Reads sat at the gp3
# cap for 25+ minutes with queue length ~1.7. Anything above 1 for 10 minutes means work is
# waiting on the disk, which is what starves sshd and cloudflared.
resource "aws_cloudwatch_metric_alarm" "nextjs_disk_queue" {
  count               = local.nextjs_alarm_enabled
  alarm_name          = "nextjs-dev-disk-saturated"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "VolumeQueueLength"
  namespace           = "AWS/EBS"
  period              = 300
  statistic           = "Average"
  threshold           = 1
  alarm_description   = "nextjs-dev root volume is queueing (>1 avg for 10min). This is what swap thrashing looks like: ssh and the tunnels starve next."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    VolumeId = aws_instance.nextjs_dev[0].root_block_device[0].volume_id
  }
}

# Reads at the gp3 throughput ceiling. 100 MB/s sustained over 5 minutes is 30 GB -- nothing
# legitimate on a dev box reads that much continuously, so this is paging or a runaway.
resource "aws_cloudwatch_metric_alarm" "nextjs_disk_read_flood" {
  count               = local.nextjs_alarm_enabled
  alarm_name          = "nextjs-dev-disk-read-flood"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "VolumeReadBytes"
  namespace           = "AWS/EBS"
  period              = 300
  statistic           = "Sum"
  threshold           = 32212254720 # 30 GiB per 5 min ~= 107 MB/s sustained
  alarm_description   = "nextjs-dev is reading at the volume's throughput ceiling. Check for swap thrash (free -m) before assuming a runaway process."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    VolumeId = aws_instance.nextjs_dev[0].root_block_device[0].volume_id
  }
}

# Burst exhaustion. Not what happened this time (credits stayed at 566/576), but a t4g that
# runs out of credits is throttled to 20% baseline and behaves like a hung box.
resource "aws_cloudwatch_metric_alarm" "nextjs_cpu_credits" {
  count               = local.nextjs_alarm_enabled
  alarm_name          = "nextjs-dev-cpu-credits-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUCreditBalance"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 60
  alarm_description   = "nextjs-dev is nearly out of CPU credits and will be throttled to baseline."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.nextjs_dev[0].id
  }
}

# Parity with the other dev hosts, which have had this since they were built.
resource "aws_cloudwatch_metric_alarm" "nextjs_status_check" {
  count               = local.nextjs_alarm_enabled
  alarm_name          = "nextjs-dev-status-check"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "nextjs-dev instance status check failed"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.nextjs_dev[0].id
  }
}

# The leading indicator, published by the CloudWatch agent in nextjs-user-data.tf. Memory is
# what actually ran out; the disk alarms above fire on the consequence. 85% leaves room to
# act -- to close a Claude session or stop an ndev -- before the kernel starts paging.
resource "aws_cloudwatch_metric_alarm" "nextjs_memory" {
  count               = local.nextjs_alarm_enabled
  alarm_name          = "nextjs-dev-memory-pressure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "mem_used_percent"
  namespace           = "CWAgent"
  period              = 300
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "nextjs-dev memory above 85%. Four accounts share 16GB; the next step is swapping, and swap-in saturates the disk and takes ssh and the tunnels with it."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  # NO DATA IS NOT HEALTHY. With notBreaching this alarm sat green from August to October with no datapoint
  # ever received (the role could not publish): it reports INSUFFICIENT_DATA now and says so on the topic.
  # (While the box is down it says so too, on top of the status-check alarm, which is acceptable.)
  treat_missing_data        = "missing"
  insufficient_data_actions = [aws_sns_topic.cost_alerts.arn]

  dimensions = {
    InstanceId = aws_instance.nextjs_dev[0].id
  }
}

# Swap in use at all is worth knowing on this box, because the swapfile lives on the same
# volume everything else reads from. 25% is "already paging", not "about to".
resource "aws_cloudwatch_metric_alarm" "nextjs_swap" {
  count               = local.nextjs_alarm_enabled
  alarm_name          = "nextjs-dev-swapping"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "swap_used_percent"
  namespace           = "CWAgent"
  period              = 300
  statistic           = "Average"
  threshold           = 25
  alarm_description   = "nextjs-dev is paging to /swapfile, which sits on the root volume. This is the start of the failure that took both tunnels down."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  # NO DATA IS NOT HEALTHY. With notBreaching this alarm sat green from August to October with no datapoint
  # ever received (the role could not publish): it reports INSUFFICIENT_DATA now and says so on the topic.
  # (While the box is down it says so too, on top of the status-check alarm, which is acceptable.)
  treat_missing_data        = "missing"
  insufficient_data_actions = [aws_sns_topic.cost_alerts.arn]

  dimensions = {
    InstanceId = aws_instance.nextjs_dev[0].id
  }
}
