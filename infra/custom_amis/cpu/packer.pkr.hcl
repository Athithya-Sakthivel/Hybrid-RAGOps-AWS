packer {
  required_plugins {
    amazon = {
      version = ">= 1.2.5"
      source  = "github.com/hashicorp/amazon"
    }
  }
}
variable "region" {
  type    = string
  default = "ap-south-1"
}
variable "architecture" {
  type    = string
  default = "x86_64"
  validation {
    condition     = contains(["x86_64", "arm64"], var.architecture)
    error_message = "Architecture must be 'x86_64' or 'arm64'."
  }
}
variable "instance_type" {
  type    = string
  default = "c6i.xlarge"
}
variable "arm_instance_type" {
  type    = string
  default = "c7g.xlarge"
}
variable "volume_size_gb" {
  type    = number
  default = 50
}
variable "ami_name" {
  type    = string
  default = ""
}
variable "ami_description" {
  type    = string
  default = "Ubuntu 22.04 CPU AMI with Python 3.11, OCR dependencies, and MLOps toolchain."
}
variable "source_ami" {
  type    = string
  default = ""
}
locals {
  actual_instance_type = var.architecture == "arm64" ? var.arm_instance_type : var.instance_type
}
source "amazon-ebs" "ubuntu2204_cpu" {
  region                      = var.region
  instance_type               = local.actual_instance_type
  ami_name                    = var.ami_name != "" ? var.ami_name : "cpu-ml-ami-${var.architecture}-{{timestamp}}"
  ssh_username                = "ubuntu"
  ssh_pty                     = true
  associate_public_ip_address = true
  source_ami = var.source_ami
  source_ami_filter {
    filters = {
      name                = var.architecture == "arm64" ? "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*" : "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
      architecture        = var.architecture
    }
    owners      = ["099720109477"]
    most_recent = true
  }
  launch_block_device_mappings {
    device_name = "/dev/xvda"
    volume_size = var.volume_size_gb
    volume_type = "gp3"
    throughput  = 125
    iops        = 3000
    delete_on_termination = true
  }
}
build {
  name    = "cpu-ml-ami-${var.architecture}"
  sources = ["source.amazon-ebs.ubuntu2204_cpu"]
  provisioner "shell" {
    script = "provision.sh"
    environment_vars = [
      "ARCHITECTURE=${var.architecture}",
      "VENV_PATH=/opt/venv",
      "MODEL_HOME=/opt/models",
      "RAPIDOCR_MODEL_DIR=/opt/models/rapidocr",
      "DEBIAN_FRONTEND=noninteractive"
    ]
    execute_command = "sudo -E bash -euxo pipefail '{{ .Path }}'"
  }
}
