locals {
  name   = "x-${var.prefix}"
  tagmap = { Name = local.name }
  dead   = var.unused_in_local
}

data "aws_ami" "base" {
  most_recent = true
}

data "aws_region" "unused" {}

resource "aws_instance" "web" {
  ami  = data.aws_ami.base.id
  tags = local.tagmap
  lifecycle {
    ignore_changes = [tags]
  }
}

module "net" {
  source  = "./modules/net"
  cidr    = var.cidr
  version = "1.0"
}
