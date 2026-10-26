# fcvm-fleet-dns.tf
#
# The launch names' subzone, tiles-stage.fcvm.dev, whose authoritative DNS server is the fleet coordinator itself
# (ejc3/fcvm#1256). The coordinator names every launch of a slot or tile <label>.tiles-stage.fcvm.dev (AAAA) and
# <label>-v4.tiles-stage.fcvm.dev (A) and answers those names from its own state, so a name resolves from the moment the
# coordinator publishes it. Before this, it wrote each name into fcvm.dev through the Cloudflare API and waited for both
# Cloudflare nameservers to answer it: 8.0 to 8.1 s per relaunch on staging.
#
# - Two Elastic IPs in two AZs of us-east-2 on a network load balancer, which forwards UDP and TCP 53 (one TCP_UDP
#   listener and target group) to the coordinator task's port 5353: the container runs without root and listens on a
#   high port. Cross-zone load balancing is on, because the one task sits in one AZ and both addresses must reach it.
#   UDP keeps the client's address at the target, which the coordinator's per-prefix response rate limit needs.
# - In fcvm.dev (Cloudflare): ns1-/ns2-tiles-stage.fcvm.dev A records on the two addresses, and the NS delegation. No DS
#   record: the subzone is unsigned, so validators treat the delegation as insecure, and fcvm.dev stays signed.
# - The certificate: the coordinator adds *.tiles-stage.fcvm.dev to the wildcard certificate it renews (fcvm-fleet.tf,
#   --tls-secret). Its DNS-01 records stay in fcvm.dev at _acme-challenge.fcvm.dev; the coordinator answers
#   _acme-challenge.tiles-stage.fcvm.dev with a CNAME to that name. No new Cloudflare permission.
#
# IPv4 only. A dualstack load balancer carries UDP over IPv6 only to an IPv6 target group, and an ECS service registers a
# dual-stack Fargate task's IPv4 address. Recursive resolvers all reach nameservers over IPv4, and viewers ask their own
# resolver, so the nameserver names have A records and no AAAA (an AAAA that does not answer UDP would cost every
# IPv6-first resolver a timeout).
#
# ECS cannot map one container port for two protocols. The task maps 5353/udp; in awsvpc mode a port mapping is a
# declaration, not a filter, so the target group's TCP (its health check included) reaches the same port.
locals {
  fcvm_fleet_names_zone = "tiles-stage.fcvm.dev"
  fcvm_fleet_names_ns   = ["ns1-tiles-stage.fcvm.dev", "ns2-tiles-stage.fcvm.dev"]
  # The coordinator's DNS port inside its task; the load balancer listens on 53.
  fcvm_fleet_dns_port = 5353
  # Two AZs of the home region, one address each.
  fcvm_fleet_dns_subnets = slice(module.fcvm_fleet_east2.subnet_ids, 0, 2)
}

resource "aws_eip" "fcvm_fleet_dns" {
  count    = 2
  provider = aws.fcvm_fleet
  domain   = "vpc"
  tags     = { Name = "fcvm-fleet-dns-${count.index + 1}", Project = "fcvm-fleet" }
}

resource "aws_lb" "fcvm_fleet_dns" {
  provider                         = aws.fcvm_fleet
  name                             = "fcvm-fleet-dns"
  load_balancer_type               = "network"
  internal                         = false
  ip_address_type                  = "ipv4"
  enable_cross_zone_load_balancing = true

  dynamic "subnet_mapping" {
    for_each = local.fcvm_fleet_dns_subnets
    content {
      subnet_id     = subnet_mapping.value
      allocation_id = aws_eip.fcvm_fleet_dns[subnet_mapping.key].id
    }
  }

  tags = { Name = "fcvm-fleet-dns", Project = "fcvm-fleet" }
}

resource "aws_lb_target_group" "fcvm_fleet_dns" {
  provider             = aws.fcvm_fleet
  name                 = "fcvm-fleet-dns"
  port                 = local.fcvm_fleet_dns_port
  protocol             = "TCP_UDP"
  target_type          = "ip"
  vpc_id               = module.fcvm_fleet_east2.vpc_id
  preserve_client_ip   = "true"
  deregistration_delay = 10

  health_check {
    protocol            = "TCP"
    port                = "traffic-port"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = { Name = "fcvm-fleet-dns", Project = "fcvm-fleet" }
}

resource "aws_lb_listener" "fcvm_fleet_dns" {
  provider          = aws.fcvm_fleet
  load_balancer_arn = aws_lb.fcvm_fleet_dns.arn
  port              = 53
  protocol          = "TCP_UDP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.fcvm_fleet_dns.arn
  }
}

# ------------------------------------------------------------------------------------------------ the delegation
resource "cloudflare_dns_record" "fcvm_fleet_names_ns_address" {
  count   = 2
  zone_id = var.fcvm_dev_zone_id
  name    = local.fcvm_fleet_names_ns[count.index]
  type    = "A"
  content = aws_eip.fcvm_fleet_dns[count.index].public_ip
  proxied = false
  ttl     = 3600
  comment = "fcvm fleet coordinator: nameserver of ${local.fcvm_fleet_names_zone} (fcvm-fleet-dns.tf)"
}

resource "cloudflare_dns_record" "fcvm_fleet_names_delegation" {
  count      = 2
  zone_id    = var.fcvm_dev_zone_id
  name       = local.fcvm_fleet_names_zone
  type       = "NS"
  content    = local.fcvm_fleet_names_ns[count.index]
  ttl        = 3600
  comment    = "fcvm fleet launch names, served by the coordinator (fcvm-fleet-dns.tf); insecure delegation, no DS"
  depends_on = [cloudflare_dns_record.fcvm_fleet_names_ns_address, aws_lb_listener.fcvm_fleet_dns]
}

output "fcvm_fleet_dns" {
  description = "The launch names' subzone, its nameservers and their addresses (fcvm-fleet-dns.tf)"
  value = {
    zone        = local.fcvm_fleet_names_zone
    nameservers = { for i, n in local.fcvm_fleet_names_ns : n => aws_eip.fcvm_fleet_dns[i].public_ip }
  }
}
