# Terraform module for the AWS Cognito Password Checker

# TODO: Consider packaging dependencies rather than depending on AWS Lambda Python runtime.

# Zip the lambda function contents
data "archive_file" "lambda_check_password" {
  type        = "zip"
  source_dir  = "${path.root}/../src/pds/cognito"
  output_path = "${path.module}/files/check_password.zip"
  excludes    = ["${path.root}/../src/pds/cognito/userpool_mgt"]
}

data "aws_caller_identity" "current" {}

# Deploy the zips to S3
module "lambda_bucket" {
  source        = "git@github.com:NASA-PDS/pds-tf-modules.git//terraform/modules/s3/bucket"  # pragma: allowlist secret
  bucket_name   = var.lambda_s3_bucket_name
  partition     = var.lambda_s3_bucket_partition
  bucket_policy = <<POLICY
  {
     "Version": "2012-10-17",
     "Statement": [
         {
             "Sid": "AllowOnlyMCPTenantOperator",
             "Effect": "Allow",
             "Principal": {
               "AWS": [
                 "arn:${var.lambda_s3_bucket_partition}:iam::${data.aws_caller_identity.current.account_id}:role/mcp-tenantOperator"
               ]
             },
             "Action": "s3:*",
             "Resource": [
                 "arn:${var.lambda_s3_bucket_partition}:s3:::${var.lambda_s3_bucket_name}/*",
                 "arn:${var.lambda_s3_bucket_partition}:s3:::${var.lambda_s3_bucket_name}"
             ]
         },
         {
             "Sid": "AllowSSLRequestsOnly",
             "Effect": "Deny",
             "Principal": "*",
             "Action": "s3:*",
             "Resource": [
                "arn:${var.lambda_s3_bucket_partition}:s3:::${var.lambda_s3_bucket_name}",
                "arn:${var.lambda_s3_bucket_partition}:s3:::${var.lambda_s3_bucket_name}/*"
              ],
              "Condition": {
                "Bool": {
                   "aws:SecureTransport": "false"
                 }
             }
         }
     ]
  }
  POLICY
  enable_blocks = true
  enable_policy = true

  required_tags = {
    project = var.project
    cicd    = var.cicd
  }
}

module "lambda_s3_object" {
  source      = "git@github.com:NASA-PDS/pds-tf-modules.git//terraform/modules/s3/object"  # pragma: allowlist secret
  bucket      = module.lambda_bucket.bucket_id
  key         = "check_password.zip"
  source_path = data.archive_file.lambda_check_password.output_path
}

# Create the Lambda functions using the zips uploaded to S3
resource "aws_lambda_function" "lambda_check_password" {
  function_name = var.lambda_function_name
  description   = var.lambda_function_description

  s3_bucket = module.lambda_bucket.bucket_id
  s3_key    = module.lambda_s3_object.s3_object_key

  runtime = "python3.13"
  handler = "check_password_lambda.lambda_handler"

  source_code_hash = data.archive_file.lambda_service.output_base64sha256

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
    arn = aws_lambda_function.lambd_check_password.arn
    role_arn = var.scheduler_iam_role_arn
    input = jsonencode({"config_ssm_path": var.ssm_key_path})
  }
}

resource "aws_ssm_parameter" "" {
  name      = "${var.ssm_key_path}/user_pool_id" 
  type      = "String"
  value     = var.user_pool_id
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

resource "aws_ssm_parameter" "valid_period" {
  name      = "${var.ssm_key_path}/valid_period"
  type      = "String"
  value     = var.valid_period
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

resource "aws_ssm_parameter" "warn_window" {
  name      = "${var.ssm_key_path}/warn_window"
  type      = "String"
  value     = var.warn_window
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

resource "aws_ssm_parameter" "smtp_server" {
  name      = "${var.ssm_key_path}/smtp_server"
  type      = "String"
  value     = var.smtp_server
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

resource "aws_ssm_parameter" "smtp_username" {
  name      = "${var.ssm_key_path}/smtp_username"
  type      = "String"
  value     = var.smtp_username
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

resource "aws_ssm_parameter" "smtp_password" {
  name      = "${var.ssm_key_path}/smtp_password"
  type      = "String"
  value     = var.smtp_password
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

resource "aws_ssm_parameter" "smtp_sender" {
  name      = "${var.ssm_key_path}/smtp_sender"
  type      = "String"
  value     = var.smtp_sender
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
