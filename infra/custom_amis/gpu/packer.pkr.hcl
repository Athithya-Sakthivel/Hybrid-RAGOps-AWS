packer {
  required_plugins {
    amazon = {
      version = ">= 1.0.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}
variable "region" {
  type    = string
  default = "ap-south-1"
}
variable "instance_type" {
  type    = string
  default = "g4dn.xlarge"
}
variable "volume_size_gb" {
  type    = number
  default = 50
}
variable "ami_name" {
  type    = string
  default = "vllm-py311-ubuntu2204-offline-ami-{{timestamp}}"
}
variable "ami_description" {
  type    = string
  default = "Ubuntu 22.04 GPU AMI with CUDA12.8, Python3.11, vLLM, Ray and baked HF models"
}
variable "source_ami" {
  type    = string
  default = ""
}

source "amazon-ebs" "ubuntu2204_gpu" {
  region                      = var.region
  instance_type               = var.instance_type
  ami_name                    = var.ami_name
  ssh_username                = "ubuntu"
  associate_public_ip_address = true

  # If BUILD supplies source_ami via -var it will be used; otherwise the filter below is used.
  source_ami = var.source_ami

  # fallback filter to find latest Ubuntu 22.04 AMI (owner = Canonical)
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
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
  name    = "vllm-ubuntu2204-offline-ami"
  sources = ["source.amazon-ebs.ubuntu2204_gpu"]
  provisioner "shell" {
    script = "provision.sh"
  }
}
