variable "unused_var" {
  type = string
}

locals {
  unused_local = 1
}

variable "used_var" {
  type = string
}

output "o" {
  value = var.used_var
}
