# Terraform module for the AWS Cognito Password Checker
terraform {
  backend "s3" {
    bucket = "pds-prod-cognito-lambda"
    key    = "terraform/check_password.tfstate"
    region = "us-west-2"
  }
}

data "aws_caller_identity" "current" {}

data "aws_s3_bucket" "lambda_bucket" {
   bucket = "${var.lambda_s3_bucket_name}"
}

data "aws_s3_object" "lambda_s3_object" {
   bucket = data.aws_s3_bucket.lambda_bucket.id
   key = "${var.zip_file_name}"
}

# Create the Lambda function using the zip uploaded to S3
resource "aws_lambda_function" "lambda_check_password" {
  function_name = var.lambda_function_name
  description   = var.lambda_function_description

  s3_bucket = data.aws_s3_bucket.lambda_bucket.id
  s3_key    = data.aws_s3_object.lambda_s3_object.key

  runtime = "python3.13"
  handler = "check_password_lambda.lambda_handler"

  # source_code_hash = data.archive_file.lambda_check_password.output_base64sha256

  role = var.lambda_iam_role_arn

  # Timeout value - 15 minutes (the max)
  timeout = 900

  # Memory value
  memory_size = 1024
}

resource "aws_cloudwatch_log_group" "lambda_check_password" {
  name = "/aws/lambda/${aws_lambda_function.lambda_check_password.function_name}"

  retention_in_days = 30
}

resource "aws_scheduler_schedule" "invoke_lambda_schedule" {
  name = "${var.lambda_function_name}_scheduler"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression = var.scheduler_schedule_expression
  target {
    arn = aws_lambda_function.lambda_check_password.arn
    role_arn = var.lambda_iam_role_arn
    input = jsonencode({"config_ssm_path": var.ssm_key_path})
  }
}

resource "aws_ssm_parameter" "user_pool_id" {
  name      = "${var.ssm_key_path}/user_pool_id" 
  type      = "String"
  value     = var.user_pool_id

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "cognito_login_url" {
  name      = "${var.ssm_key_path}/cognito_login_url" 
  type      = "String"
  value     = var.cognito_login_url

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "valid_period" {
  name      = "${var.ssm_key_path}/valid_period"
  type      = "String"
  value     = var.valid_period

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "warn_window" {
  name      = "${var.ssm_key_path}/warn_window"
  type      = "String"
  value     = var.warn_window

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "smtp_server" {
  name      = "${var.ssm_key_path}/smtp_server"
  type      = "String"
  value     = var.smtp_server

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "smtp_username" {
  name      = "${var.ssm_key_path}/smtp_username"
  type      = "String"
  value     = var.smtp_username

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "smtp_password" {
  name      = "${var.ssm_key_path}/smtp_password"
  type      = "String"
  value     = var.smtp_password

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "smtp_sender" {
  name      = "${var.ssm_key_path}/smtp_sender"
  type      = "String"
  value     = var.smtp_sender

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "expired_message_template" {
  name      = "${var.ssm_key_path}/expired_message_template"
  type      = "String"
  value     = var.expired_message_template

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "expired_subject_template" {
  name      = "${var.ssm_key_path}/expired_subject_template"
  type      = "String"
  value     = var.expired_subject_template

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "warning_message_template" {
  name      = "${var.ssm_key_path}/warning_message_template"
  type      = "String"
  value     = var.warning_message_template

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "warning_subject_template" {
  name      = "${var.ssm_key_path}/warning_subject_template"
  type      = "String"
  value     = var.warning_subject_template

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "apply_changes" {
  name      = "${var.ssm_key_path}/apply_changes"
  type      = "String"
  value     = var.apply_changes

  overwrite = true

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}

resource "aws_ssm_parameter" "develop_mode" {
  name      = "${var.ssm_key_path}/develop_mode"
  type      = "String"
  value     = var.develop_mode

  overwrite = true

  tags = {
    Name = var.lambda_function_name
    Node = var.tag_node_value
    Venue = var.tag_venue_value
    Project = "PDS"
    Service = "SSM"
    CreatedBy = var.tag_createdby_value
  }
}
