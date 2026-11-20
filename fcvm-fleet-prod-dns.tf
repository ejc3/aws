# fcvm-fleet-prod-dns.tf
#
# The prod fleet's launch names' subzone, tiles.fcvm.dev, served by the prod coordinator itself, as staging's
# tiles-stage.fcvm.dev is by staging's (fcvm-fleet-dns.tf, whose notes apply here): its own network load balancer with
# two Elastic IPs in two AZs of us-east-2, UDP and TCP 53 to the prod task's port 5353, and in fcvm.dev the
# ns1-/ns2-tiles.fcvm.dev A records and the NS delegation (no DS record). Everything behind var.fcvm_fleet_prod_enabled.
#
# The prod certificate covers *.tiles.fcvm.dev and nothing else (--tls-scope subzone, fcvm-fleet-prod.tf): its DNS-01
# TXT record is answered by this server, at _acme-challenge.tiles.fcvm.dev, so the prod coordinator needs no Cloudflare
# token. The page (ejc3/fcvm fleet/web, env.production, FLEET_TILES_SUBZONE) expects this subzone.
#
# Cost while on: the load balancer (about $16.43 a month plus its capacity units in us-east-2) and two public IPv4
# addresses ($3.65 a month each).
locals {
  fcvm_fleet_prod_names_zone = "tiles.fcvm.dev"
  fcvm_fleet_prod_names_ns   = ["ns1-tiles.fcvm.dev", "ns2-tiles.fcvm.dev"]
  # The same two AZs of the home region as staging's load balancer; each stack has its own addresses.
  fcvm_fleet_prod_dns_subnets = slice(module.fcvm_fleet_east2.subnet_ids, 0, 2)
}

resource "aws_eip" "fcvm_fleet_prod_dns" {
  count    = 2 * local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  domain   = "vpc"
  tags     = { Name = "${local.fcvm_fleet_prod_stack.prefix}-dns-${count.index + 1}", Project = "fcvm-fleet", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_lb" "fcvm_fleet_prod_dns" {
  count                            = local.fcvm_fleet_prod
  provider                         = aws.fcvm_fleet
  name                             = "${local.fcvm_fleet_prod_stack.prefix}-dns"
  load_balancer_type               = "network"
  internal                         = false
  ip_address_type                  = "ipv4"
  enable_cross_zone_load_balancing = true

  dynamic "subnet_mapping" {
    for_each = local.fcvm_fleet_prod_dns_subnets
    content {
      subnet_id     = subnet_mapping.value
      allocation_id = aws_eip.fcvm_fleet_prod_dns[subnet_mapping.key].id
    }
  }

  tags = { Name = "${local.fcvm_fleet_prod_stack.prefix}-dns", Project = "fcvm-fleet", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_lb_target_group" "fcvm_fleet_prod_dns" {
  count                = local.fcvm_fleet_prod
  provider             = aws.fcvm_fleet
  name                 = "${local.fcvm_fleet_prod_stack.prefix}-dns"
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

  tags = { Name = "${local.fcvm_fleet_prod_stack.prefix}-dns", Project = "fcvm-fleet", Fleet = local.fcvm_fleet_prod_stack.fleet }
}

resource "aws_lb_listener" "fcvm_fleet_prod_dns" {
  count             = local.fcvm_fleet_prod
  provider          = aws.fcvm_fleet
  load_balancer_arn = aws_lb.fcvm_fleet_prod_dns[0].arn
  port              = 53
  protocol          = "TCP_UDP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.fcvm_fleet_prod_dns[0].arn
  }
}

# ------------------------------------------------------------------------------------------------ the delegation
resource "cloudflare_dns_record" "fcvm_fleet_prod_names_ns_address" {
  count   = 2 * local.fcvm_fleet_prod
  zone_id = var.fcvm_dev_zone_id
  name    = local.fcvm_fleet_prod_names_ns[count.index]
  type    = "A"
  content = aws_eip.fcvm_fleet_prod_dns[count.index].public_ip
  proxied = false
  ttl     = 3600
  comment = "fcvm prod fleet coordinator: nameserver of ${local.fcvm_fleet_prod_names_zone} (fcvm-fleet-prod-dns.tf)"
}

resource "cloudflare_dns_record" "fcvm_fleet_prod_names_delegation" {
  count      = 2 * local.fcvm_fleet_prod
  zone_id    = var.fcvm_dev_zone_id
  name       = local.fcvm_fleet_prod_names_zone
  type       = "NS"
  content    = local.fcvm_fleet_prod_names_ns[count.index]
  ttl        = 3600
  comment    = "fcvm prod fleet launch names, served by the prod coordinator (fcvm-fleet-prod-dns.tf); insecure delegation, no DS"
  depends_on = [cloudflare_dns_record.fcvm_fleet_prod_names_ns_address, aws_lb_listener.fcvm_fleet_prod_dns]
}
