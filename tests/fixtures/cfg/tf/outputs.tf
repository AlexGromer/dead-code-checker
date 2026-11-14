output "ip" {
  value = aws_instance.web.private_ip
}
output "net" {
  value = module.net.id
}
