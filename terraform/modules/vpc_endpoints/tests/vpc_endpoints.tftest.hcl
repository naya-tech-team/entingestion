mock_provider "aws" {
  override_during = plan

  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
}

variables {
  name            = "test"
  vpc_id          = "vpc-123"
  route_table_ids = ["rtb-1", "rtb-2"]
  subnet_ids      = ["subnet-1", "subnet-2"]
}

run "gateway_only_by_default" {
  command = plan

  assert {
    condition = (
      aws_vpc_endpoint.gateway["s3"].service_name == "com.amazonaws.us-west-2.s3"
      && aws_vpc_endpoint.gateway["dynamodb"].vpc_endpoint_type == "Gateway"
      && length(aws_vpc_endpoint.interface) == 0
      && length(aws_security_group.interface) == 0
    )
    error_message = "Default must be the free S3 and DynamoDB gateway endpoints only."
  }
}

run "interface_endpoints_with_private_dns" {
  command = plan

  variables {
    interface_services         = ["secretsmanager"]
    allowed_security_group_ids = { client = "sg-client" }
  }

  assert {
    condition = (
      aws_vpc_endpoint.interface["secretsmanager"].private_dns_enabled
      && aws_vpc_security_group_ingress_rule.interface["client"].from_port == 443
    )
    error_message = "Interface endpoints need private DNS and 443 from the allowed groups."
  }
}

run "rejects_unknown_gateway_service" {
  command = plan

  variables {
    gateway_services = ["sqs"]
  }

  expect_failures = [var.gateway_services]
}
