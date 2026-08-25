variable "aws_region" {
  description = "AWS region for the state bucket and GitHub Actions role."
  type        = string
}

variable "state_bucket_name" {
  description = "Globally unique name for the private S3 bucket that stores the review watermark."
  type        = string
}

variable "github_actions_role_name" {
  description = "Unique name of the least-privilege IAM role GitHub Actions assumes."
  type        = string
}

variable "github_oidc_subject" {
  description = "Exact GitHub OIDC subject allowed to assume the role, including the approved branch."
  type        = string
}

variable "github_oidc_provider_arn" {
  description = "Existing GitHub Actions OIDC provider ARN. Leave null to create it in this AWS account."
  type        = string
  default     = null
  nullable    = true
}
