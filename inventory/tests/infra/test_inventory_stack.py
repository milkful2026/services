"""Pure `cdk synth` + Template assertions — no AWS credentials, no
bootstrap, no Docker daemon (confirmed unavailable in this environment;
see the stack module's docstring for why ContainerImage.from_asset isn't
used)."""

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

_INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
if str(_INFRA_DIR) not in sys.path:
    sys.path.insert(0, str(_INFRA_DIR))

from inventory.inventory_stack import InventoryStack  # noqa: E402


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = InventoryStack(app, "MilkfulInventoryStack")
    return Template.from_stack(stack)


def test_fargate_service_exists(template):
    template.resource_count_is("AWS::ECS::Service", 1)


def test_aurora_serverless_v2_cluster(template):
    template.has_resource_properties(
        "AWS::RDS::DBCluster",
        {"Engine": "aurora-postgresql", "ServerlessV2ScalingConfiguration": Match.object_like({})},
    )


def test_zone_updated_queue_has_dlq_with_redrive_policy(template):
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"RedrivePolicy": Match.object_like({"maxReceiveCount": 5})},
    )


def test_public_route_exists_and_internal_route_does_not(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {props["Properties"]["RouteKey"] for props in routes.values()}
    assert route_keys == {"GET /v1/serviceability/check"}
    assert "GET /v1/internal/serviceability/check" not in route_keys


def test_internal_alb_is_not_internet_facing(template):
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal"}
    )


def test_vpc_link_security_group_has_ingress_to_alb_security_group(template):
    # Without this, the API Gateway VpcLink (and anything else in the VPC)
    # has no ingress rule allowing it to reach the ALB's security group —
    # the service would be completely unreachable despite synthesizing
    # cleanly.
    ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")
    assert any(
        props["Properties"].get("FromPort") == 80 and props["Properties"].get("ToPort") == 80
        for props in ingress_rules.values()
    )


def test_alb_target_group_health_check_is_not_the_business_endpoint(template):
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"HealthCheckPath": "/healthz"},
    )


def test_ecr_repository_exists_for_ci_built_image(template):
    template.resource_count_is("AWS::ECR::Repository", 1)


def test_container_uses_ecr_image_not_local_asset(template):
    task_defs = template.find_resources("AWS::ECS::TaskDefinition")
    assert len(task_defs) == 1
    container_defs = next(iter(task_defs.values()))["Properties"]["ContainerDefinitions"]
    image = container_defs[0]["Image"]
    # An asset-based image would resolve to a CDK-bootstrap asset token;
    # an ECR-repository image resolves via Fn::Join/Ref against the
    # repository resource instead.
    assert "Fn::Join" in image or "Ref" in str(image)


def test_zone_updated_eventbridge_rule_matches_admin_source(template):
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": {"source": ["inventory-admin"], "detail-type": ["inventory.zone.updated"]}},
    )


def test_fargate_task_role_can_consume_zone_updated_queue(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [Match.object_like({"Action": Match.array_with(["sqs:ReceiveMessage"])})]
                )
            }
        },
    )
