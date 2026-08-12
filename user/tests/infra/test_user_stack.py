"""Pure `cdk synth` + Template assertions — no AWS credentials, no
bootstrap, no Docker."""

import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

_INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
if str(_INFRA_DIR) not in sys.path:
    sys.path.insert(0, str(_INFRA_DIR))

from user.user_stack import UserStack  # noqa: E402


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = UserStack(
        app,
        "MilkfulUserStack",
        cognito_user_pool_id="ap-south-1_PLACEHOLDER",
        cognito_client_id="PLACEHOLDER_CLIENT_ID",
    )
    return Template.from_stack(stack)


def test_three_lambdas_exist_for_the_three_handlers(template):
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"].get("Handler")
        for props in functions.values()
        if str(props["Properties"].get("Handler", "")).startswith("handlers.")
    }
    assert handlers == {
        "handlers.register_handler.handler",
        "handlers.delivery_slots_handler.handler",
        "handlers.outbox_publisher_handler.handler",
    }


def test_aurora_serverless_v2_cluster(template):
    template.has_resource_properties(
        "AWS::RDS::DBCluster",
        {"Engine": "aurora-postgresql", "ServerlessV2ScalingConfiguration": Match.object_like({})},
    )


def test_both_routes_have_jwt_authorizer_attached(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 2
    for props in routes.values():
        assert props["Properties"].get("AuthorizerId") is not None

    route_keys = {props["Properties"]["RouteKey"] for props in routes.values()}
    assert route_keys == {"POST /users/register", "GET /delivery/slots"}


def test_jwt_authorizer_references_cognito_issuer(template):
    # The issuer URL is built via string interpolation with self.region,
    # so CDK synthesizes it as an Fn::Join intrinsic, not a plain string
    # — a regex Matcher can't match an intrinsic object, so this checks
    # the raw synthesized JSON for the pool ID substring instead.
    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    authorizer = next(iter(authorizers.values()))
    assert authorizer["Properties"]["AuthorizerType"] == "JWT"
    assert "ap-south-1_PLACEHOLDER" in json.dumps(authorizer["Properties"]["JwtConfiguration"])


def test_outbox_publisher_has_a_one_minute_schedule(template):
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "rate(1 minute)"}
    )


def test_execution_role_scopes_admin_update_user_attributes_to_pool_arn(template):
    # Same reasoning as the issuer test above — the pool ARN is built via
    # string interpolation and synthesizes as Fn::Join, not a plain
    # string, so this finds the statement by Action and checks the raw
    # JSON for the pool ID substring rather than regex-matching an
    # intrinsic.
    policies = template.find_resources("AWS::IAM::Policy")
    statement = None
    for props in policies.values():
        for stmt in props["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Action") == "cognito-idp:AdminUpdateUserAttributes":
                statement = stmt
    assert statement is not None, "no AdminUpdateUserAttributes statement found"
    assert "userpool/ap-south-1_PLACEHOLDER" in json.dumps(statement["Resource"])
