output "node_ips" {
  value = { for k, v in google_compute_address.node : k => v.address }
}

output "frontend_url" {
  description = "A app fica acessível por qualquer nó, na porta NodePort do frontend."
  value       = "http://${google_compute_address.node["worker1"].address}:${var.frontend_node_port}"
}
