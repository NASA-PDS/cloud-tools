variable "aws_region" {
  type        = string
  description = "AWS Region"
  default     = "us-west-2"
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
  description = "Name of the archive zip file to generate."
}

variable "lambda_s3_bucket_name" {
  type        = string
  description = "Name of the S3 bucket to upload Lambda artifacts to"
}

variable "lambda_s3_bucket_partition" {
  type    = string
  default = "aws"
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
