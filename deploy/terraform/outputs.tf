output "control_url" {
  value = google_cloud_run_v2_service.svc["control"].uri
}

output "gateway_url" {
  value = google_cloud_run_v2_service.svc["gateway"].uri
}

output "tools_url" {
  value = google_cloud_run_v2_service.svc["tools"].uri
}

output "image" {
  value = local.image
}

output "oracle_key_version" {
  value = local.key_ver["oracle"]
}

output "agent_service_account" {
  value = google_service_account.sa["agent"].email
}

output "approver_service_account" {
  value = google_service_account.approver.email
}
