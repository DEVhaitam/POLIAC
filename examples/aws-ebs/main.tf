resource "aws_instance" "vm" {
  ami           = "ami-0000000000000000f" # placeholder -- not a real, resolvable AMI
  instance_type = "t3.medium"

  root_block_device {
    volume_size = 20
    volume_type = "gp2"
  }

  tags = {
    Name = "correctexam-exp"
  }
}

resource "aws_db_instance" "db" {
  engine            = "mysql"
  instance_class    = "db.t3.medium"
  allocated_storage = 20
}
