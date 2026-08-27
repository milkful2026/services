"""Pure `cdk synth` + Template assertions — no AWS credentials, no
bootstrap, no Docker. Mirrors services/user/tests/infra/test_user_stack.py.
"""

import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

_INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
if str(_INFRA_DIR) not in sys.path:
    sys.path.insert(0, str(_INFRA_DIR))

from cart.cart_stack import CartStack  # noqa: E402


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = CartStack(
        app,
        "MilkfulCartStack",
        cognito_user_pool_id="ap-south-1_PLACEHOLDER",
        cognito_client_id="PLACEHOLDER_CLIENT_ID",
    )
    return Template.from_stack(stack)


def test_five_lambdas_exist_for_the_five_handlers(template):
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"].get("Handler")
        for props in functions.values()
        if str(props["Properties"].get("Handler", "")).startswith("handlers.")
    }
    assert handlers == {
        "handlers.get_cart_handler.handler",
        "handlers.add_item_handler.handler",
        "handlers.put_cart_handler.handler",
        "handlers.delete_item_handler.handler",
        "handlers.outbox_publisher_handler.handler",
    }


def test_cart_table_has_the_right_key_schema_and_ttl(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "TimeToLiveSpecification": {"AttributeName": "expiresAt", "Enabled": True},
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_all_four_routes_use_jwt_authorizer(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 4
    for props in routes.values():
        assert props["Properties"]["AuthorizationType"] == "JWT"
        assert props["Properties"].get("AuthorizerId") is not None

    route_keys = {props["Properties"]["RouteKey"] for props in routes.values()}
    assert route_keys == {
        "GET /cart",
        "POST /cart/items",
        "PUT /cart",
        "DELETE /cart/items/{id}",
    }


def test_jwt_authorizer_references_cognito_issuer(template):
    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    authorizer = next(iter(authorizers.values()))
    assert authorizer["Properties"]["AuthorizerType"] == "JWT"
    assert "ap-south-1_PLACEHOLDER" in json.dumps(authorizer["Properties"]["JwtConfiguration"])


def test_execution_role_can_put_events_on_default_bus(template):
    policies = template.find_resources("AWS::IAM::Policy")
    statement = None
    for props in policies.values():
        for stmt in props["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Action") == "events:PutEvents":
                statement = stmt
    assert statement is not None, "no events:PutEvents statement found"
    assert "event-bus/default" in json.dumps(statement["Resource"])


def test_execution_role_arn_is_exported_for_users_cross_stack_grant(template):
    # See cart_stack.py's own module docstring point 2 — this output is
    # how services/user's UserStack (internal_caller_role_arns) is meant
    # to learn this role's ARN, since these are two separate CDK apps.
    outputs = template.to_json().get("Outputs", {})
    assert "ExecutionRoleArn" in outputs


def test_outbox_publisher_has_a_one_minute_schedule(template):
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "rate(1 minute)"}
    )


def test_no_vpc_is_provisioned(template):
    # cart_stack.py's own module docstring point 3 — no Redis/VPC-bound
    # dependency exists in this pass, unlike identity-auth/user/inventory.
    assert template.find_resources("AWS::EC2::VPC") == {}
