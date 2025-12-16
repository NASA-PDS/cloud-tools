variable "aws_region" {
  type        = string
  description = "AWS Region"
  default     = "us-west-2"
}

variable "ssm_key_path" {
  type        = string
  description = "Path prefix to the set of SSM parameters"
}

variable "project" {
  type        = string
  description = "Project name"
  default = "pds-en"
}

variable "cicd" {
  type        = string
  description = "CI/CD environment name"
  default     = "pds-github"
}

variable "zip_file_name" {
  type        = string
  description = "Name of the archive zip file to use as the lambda source."
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
  default     = "pds_check_password_lambda"
  description = "Name to assign to the password checker Lambda"
}

variable "lambda_function_description" {
  type        = string
  default     = "PDS AWS Cognito user password validation check"
  description = "Description for the Lambda function"
}

variable "lambda_iam_role_arn" {
  type        = string
  description = "IAM role ARN to allocate to the Lambda function. Also used as the scheduler target execution role."
}

variable "scheduler_schedule_expression" {
  type        = string
  description = "Schedule expression for the EventBridge execution of the lambda"
  # default to every day at midnight
  default     = "cron(00 00 ? * * *)"
}

variable "schedule_schedule_expression_timezone" {
  type        = string
  description = "Timezone of the schedule expression"
  # Default to Pacific time for easier understanding. Sorry, Sean Kelly...
  default     = "America/Los Angeles"
}

variable "user_pool_id" {
  type        = string
  description = "ID of the user pool for which passwords are to be checked/validated"
}

# Note that this can be programmatically generated but there are just so many options
# as far as client, client scopes, auth flows, domain types it's simplest just to
# specify the whole thing as a single value to reduce code complexity.
variable "cognito_login_url" {
  type        = string
  description = "Full URL of the cognito login page for this user pool, domain and client"
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

# for the templates below, the following format variables are supported: 
#  - username : the name of the user as registered in the user pool
#  - user_email : the user's registered email address
#  - user_pool_name : the name of the pool to which the user belongs
#  - user_pool_id : the ID of the pool to which the user belongs
#  - valid_date : the date at which the user password expire (current date - valid_period)
#  - expiration_date : the data at which the user's password expired ("N/A" if it has not expired)
#  - warn_date : the date at which the warnings regarding the user's password expiring are issued
#  - valid_period : the period in days of which a user's password remains valid
#  - warn_window : the period in days prior to expiration at which warnings are sent
#  - temp_password : if the password has expired, the temporary password generated for the user
#  - temp_password_validity_days : the # of days the temporary password remains valid

variable "expired_message_template" {
  type        = string
  description = "Template (in python string.format form) for email message to send when a user's password has expired"
  default     = "The password for your PDS Cognito user {username} has expired and a temporary one: {temp_password} has been assigned. Please access the login URL in order to change your password. This temporary password will remain valid for {temp_password_validity_days} days."
}

variable "expired_subject_template" {
  type        = string
  description = "Template (in python string.format form) of subject line for email message to send when a user's password has expired"
  default     = "PDS Cognito User Password for {username} has Expired"
}

variable "warning_message_template" {
  type        = string
  description = "Template (in python string.format form) for email message to send when a user's password is about to expire"
  default     = "The password for your PDS Cognito user {username} will expire on {expiration_date} after which you will forced to change it. Please access 'forgot password' link at the login URL in order to change and reset your passworda."
}

variable "warning_subject_template" {
  type        = string
  description = "Template (in python string.format form) of subject line for email message to send when a user's password is about to expire"
  default     = "PDS Cognito User Password expiration for {username} is approaching"
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

# These should be used only for dev/debugging. If not defined, they default to True and False, respectively. See descriptions for their effects.
variable "apply_changes" {
  type        = string
  description = "Optionally deactivate making changes to the state of the users and send email"
  default     = "True"
}

variable "develop_mode" {
  type        = string
  description = "Optionally active develop mode which considers valid_period and warn window as MINUTES, not days."
  default     = "False"
}
