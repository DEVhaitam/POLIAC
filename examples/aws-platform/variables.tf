variable "db_password" {
  type      = string
  sensitive = true
  default   = "placeholder-not-a-real-secret"
}

variable "redshift_password" {
  type      = string
  sensitive = true
  default   = "placeholder-not-a-real-secret"
}

variable "subnet_id" {
  type    = string
  default = "subnet-00000000000000000"
}

variable "ad_id" {
  type    = string
  default = "d-0000000000"
}
