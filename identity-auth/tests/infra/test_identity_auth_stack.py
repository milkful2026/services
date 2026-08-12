"""Pure `cdk synth` + Template assertions — no AWS credentials, no
bootstrap, no Docker. The stack deliberately avoids Vpc.from_lookup and
Docker-bundled PythonFunction so this can run fully offline (see the
stack module's docstring)."""

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

_INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
if str(_INFRA_DIR) not in sys.path:
    sys.path.insert(0, str(_INFRA_DIR))

from identity_auth.identity_auth_stack import IdentityAuthStack  # noqa: E402


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = IdentityAuthStack(app, "MilkfulIdentityAuthStack")
    return Template.from_stack(stack)


def test_cognito_user_pool_uses_phone_and_email_as_username(template):
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"UsernameAttributes": ["email", "phone_number"]},
    )


def test_otp_requests_table_has_correct_keys_and_ttl(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": "requestId", "KeyType": "HASH"}],
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            "GlobalSecondaryIndexes": Match.array_with(
                [Match.object_like({"IndexName": "mobile-index"})]
            ),
        },
    )


def test_four_registration_endpoint_lambdas_exist(template):
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"].get("Handler")
        for props in functions.values()
        if str(props["Properties"].get("Handler", "")).startswith("handlers.")
    }
    assert handlers == {
        "handlers.otp_send_handler.handler",
        "handlers.otp_verify_handler.handler",
        "handlers.social_auth_handler.handler",
        "handlers.token_refresh_handler.handler",
    }


def test_http_api_has_four_routes_with_no_authorizer(template):
    template.resource_count_is("AWS::ApiGatewayV2::Route", 4)
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    for props in routes.values():
        assert "AuthorizerId" not in props["Properties"]

    route_keys = {props["Properties"]["RouteKey"] for props in routes.values()}
    assert route_keys == {
        "POST /v1/auth/otp/send",
        "POST /v1/auth/otp/verify",
        "POST /v1/auth/social",
        "POST /v1/auth/token/refresh",
    }


def test_execution_role_scopes_cognito_admin_actions_to_the_pool(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": Match.array_with(["cognito-idp:AdminCreateUser"]),
                                "Effect": "Allow",
                            }
                        )
                    ]
                )
            }
        },
    )


def test_event_bridge_rule_matches_otp_requested(template):
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "source": ["identity-auth"],
                "detail-type": ["identity.otp.requested"],
            }
        },
    )


def test_waf_web_acl_has_rate_based_rule(template):
    template.has_resource_properties(
        "AWS::WAFv2::WebACL",
        {
            "Rules": Match.array_with(
                [Match.object_like({"Statement": {"RateBasedStatement": Match.object_like({})}})]
            )
        },
    )
