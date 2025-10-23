
variable "ssm_key_path" {
  type        = string
  description = "Path prefix to the set of SSM parameters"
}

variable "project" {
  type        = string
  description = "Project name"
}

variable "cicd" {
  type        = string
  description = "CI/CD environment name"
  default     = "pds-github"
}

variable "lambda_s3_bucket_name" {
  type        = string
  description = "Name of the S3 bucket to upload Lambda artifacts to"
}

variable "lambda_s3_bucket_partition" {
  type    = string
  default = "aws"
}

variable "lambda_function_name" {
  type        = string
  default     = "check_password_lambda"
  description = "Name to assign to the password checker Lambda"
}

variable "lambda_function_description" {
  type        = string
  default     = "PDS AWS Cognito user password validation check"
  description = "Description for the Lambda function"
}

variable "lambda_iam_role_arn" {
  type        = string
  description = "IAM role ARN to allocate to the Lambda function"
}

variable "scheduler_schedule_expression" {
  type        = string
  description = "Schedule expression for the EventBridge execution of the lambda"
  # default to every day at midnight
  default     = "cron(00 00 ? * * *)"
}

variable "scheduler_iam_role_arn" {
  type        = string
  description = "ARN of the IAM role to use for EventBridge scheduled execution"
}

variable "user_pool_id" {
  type        = string
  description = "ID of the user pool for which passwords are to be checked/validated"
}

variable "valid_period" {
  type        = string
  description = "Number of days after which passwords are considered expired"
  default     = "90"
}

variable "warn_window" {
  type        = string
  description = "Number of days PRIOR to expiration at which point imminent expiration messages are sent"
}

variable "smtp_server" {
  type        = string
  description = "The URI of the SMTP endpoint to utilize for outgoing email messages"
}

variable "smtp_username" {
  type        = string
  description = "The username of the SMTP user"
}

variable "smtp_password" {
  type        = string
  description = "The password of the SMTP user"
}

variable "smtp_sender" {
  type        = string
  description = "The effective email address for outgoing messages"
}

variable "tag_node_value" {
  description = "The value for the Node tag"
  type        = string
  default     = "ENG"
}

variable "tag_venue_value" {
  description = "The value for the Venue tag"
  type        = string
  default     = "Dev"
}

variable "tag_createdby_value" {
  description = "The value for the CreatedBy tag"
  type        = string
  default     = "pds_operations@jpl.nasa.gov"
}

# These should be used only for dev/debugging. If not defined, they default to True and False, respectively
# variable "apply_changes" {
#   type        = string
#   description = "Optionally deactivate making changes to the state of the users and send email"
#   default     = "True"
# }

# variable "develop_mode" {
#   type        = string
#   description = "Optionally active develop mode which considers valid_period and warn window as MINUTES, not days."
#   default     = "False"
# }
