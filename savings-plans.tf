# One-year, all-upfront EC2 Instance Savings Plan for the t4g family in us-west-1.
#
# It covers the always-on on-demand servers: nextjs-dev as t4g.xlarge and the admin jumpbox
# kept at t4g.large. At this offering's rates those are $0.0940/h and $0.0470/h, so the
# commitment is $0.1410/h and the upfront payment is 0.141 x 8,760 h = $1,235.16. Any t4g
# size in us-west-1 draws on it; moving either box to another family, or to spot, leaves the
# commitment paid but unused until the term ends.
#
# A purchased plan cannot be cancelled, and removing this resource only drops it from state.
# AWS accepts a return within 7 days of purchase and in the same calendar month, for plans of
# $100/h or less. prevent_destroy keeps a refactor from silently orphaning it.
resource "aws_savingsplans_savings_plan" "t4g_us_west_1" {
  # "1 year All Upfront t4g EC2 Instance Savings Plan in us-west-1"
  savings_plan_offering_id = "6025df76-1651-4314-9748-2302a8efe3c4"
  commitment               = "0.141"
  upfront_payment_amount   = "1235.16"

  tags = {
    Name    = "t4g-us-west-1"
    Purpose = "Always-on t4g servers: nextjs-dev and the admin jumpbox"
  }

  lifecycle {
    prevent_destroy = true
  }
}
