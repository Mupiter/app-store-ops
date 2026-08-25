output "aws_region" {
  description = "Set this as the AWS_REGION GitHub Actions repository variable."
  value       = var.aws_region
}

output "state_bucket_name" {
  description = "Set this as the AWS_S3_BUCKET GitHub Actions repository variable."
  value       = aws_s3_bucket.review_state.bucket
}

output "github_actions_role_arn" {
  description = "Set this as the AWS_ROLE_ARN GitHub Actions repository variable."
  value       = aws_iam_role.github_actions.arn
}
