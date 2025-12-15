# Terraform module for the AWS Lambda zip file for the Cognito Password Checker
terraform {
  backend "s3" {
    bucket = "pds-prod-cognito-lambda"
    key    = "terraform/check_password_zip.tfstate"
    region = "us-west-2"
  }
}


# TODO: Consider packaging dependencies rather than depending on AWS Lambda Python runtime.

# Zip the lambda function contents
data "archive_file" "lambda_check_password" {
  type        = "zip"
  source_dir  = "${path.root}/../../../../src/pds/cognito"
  output_path = "${path.module}/files/${var.zip_file_name}"
  excludes    = [ "userpool_mgt" ]
}

data "aws_caller_identity" "current" {}

# Deploy the zips to S3
module "lambda_bucket" {
  source        = "git::https://github.com/NASA-PDS/pds-tf-modules.git//terraform/modules/s3/bucket"  # pragma: allowlist secret
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
  enable_blocks = false
  enable_policy = true

  required_tags = {
    project = var.project
    cicd    = var.cicd
  }
}

module "lambda_s3_object" {
  source        = "git::https://github.com/NASA-PDS/pds-tf-modules.git//terraform/modules/s3/object"  # pragma: allowlist secret
  bucket      = module.lambda_bucket.bucket_id
  key         = "${var.zip_file_name}"
  source_path = data.archive_file.lambda_check_password.output_path
}
