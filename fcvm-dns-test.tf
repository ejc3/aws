# Operators measure DNS propagation without authority over the fcvm.dev parent or a fleet namespace.
locals {
  fcvm_dns_probe_zone = "dnstest.fcvm.dev"
  fcvm_dns_probe_source_cidrs = concat(
    [for s in local.dev_fleet_subnets : s.ipv6_cidr_block],
    [for ip in local.firecracker_dev_public_ips : "${ip}/32"],
    [for e in aws_eip.x86_dev : "${e.public_ip}/32"],
  )
}

resource "aws_route53_zone" "fcvm_dns_probe" {
  name = local.fcvm_dns_probe_zone
  tags = { Project = "dev", Managed = "terraform" }
}

# The AWS-assigned NS names are unknown at plan time. A public hosted zone has four.
resource "cloudflare_dns_record" "fcvm_dns_probe_ns" {
  count   = 4
  zone_id = var.fcvm_dev_zone_id
  name    = local.fcvm_dns_probe_zone
  type    = "NS"
  content = aws_route53_zone.fcvm_dns_probe.name_servers[count.index]
  ttl     = 3600
}

# Zone IDs are public identifiers. This record lets a dev box discover only this one without AWS list access.
resource "aws_route53_record" "fcvm_dns_probe_id" {
  zone_id = aws_route53_zone.fcvm_dns_probe.zone_id
  name    = "_zone-id.${local.fcvm_dns_probe_zone}"
  type    = "TXT"
  ttl     = 300
  records = [aws_route53_zone.fcvm_dns_probe.zone_id]
}
