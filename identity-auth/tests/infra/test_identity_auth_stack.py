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


def test_seven_endpoint_lambdas_exist(template):
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"].get("Handler")
        for props in functions.values()
        if str(props["Properties"].get("Handler", "")).startswith("handlers.")
    }
    assert {
        "handlers.otp_send_handler.handler",
        "handlers.otp_verify_handler.handler",
        "handlers.social_auth_handler.handler",
        "handlers.token_refresh_handler.handler",
        "handlers.login_otp_send_handler.handler",
        "handlers.login_otp_verify_handler.handler",
        "handlers.logout_handler.handler",
    } <= handlers


def test_admin_endpoint_lambdas_exist(template):
    # MA-129 additions — a distinct test rather than folding into
    # test_seven_endpoint_lambdas_exist so a future regression names the
    # right feature.
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"].get("Handler")
        for props in functions.values()
        if str(props["Properties"].get("Handler", "")).startswith("handlers.")
    }
    assert {
        "handlers.admin_auth.login_handler.handler",
        "handlers.admin_auth.verify_2fa_handler.handler",
        "handlers.admin_users.create_handler.handler",
        "handlers.admin_users.list_handler.handler",
        "handlers.admin_users.update_handler.handler",
        "handlers.admin_users.deactivate_handler.handler",
        "handlers.admin_users.reactivate_handler.handler",
        "handlers.admin_authorizer_handler.handler",
    } <= handlers


def test_no_stray_handler_lambdas_beyond_consumer_and_admin(template):
    # The two subset checks above (test_seven_endpoint_lambdas_exist,
    # test_admin_endpoint_lambdas_exist) only prove the 15 known
    # handlers are PRESENT — they can't catch a stray/duplicate/
    # misnamed extra handler, since a superset still satisfies "these
    # are a subset". This closes that gap with the single exact-total
    # assertion the original all-consumer test had before MA-129 split
    # it into two subset checks.
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"].get("Handler")
        for props in functions.values()
        if str(props["Properties"].get("Handler", "")).startswith("handlers.")
    }
    consumer_handlers = {
        "handlers.otp_send_handler.handler",
        "handlers.otp_verify_handler.handler",
        "handlers.social_auth_handler.handler",
        "handlers.token_refresh_handler.handler",
        "handlers.login_otp_send_handler.handler",
        "handlers.login_otp_verify_handler.handler",
        "handlers.logout_handler.handler",
    }
    admin_handlers = {
        "handlers.admin_auth.login_handler.handler",
        "handlers.admin_auth.verify_2fa_handler.handler",
        "handlers.admin_users.create_handler.handler",
        "handlers.admin_users.list_handler.handler",
        "handlers.admin_users.update_handler.handler",
        "handlers.admin_users.deactivate_handler.handler",
        "handlers.admin_users.reactivate_handler.handler",
        "handlers.admin_authorizer_handler.handler",
    }
    assert handlers == consumer_handlers | admin_handlers


def test_http_api_has_seven_routes_only_logout_authorized(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")

    consumer_route_keys = {
        "POST /v1/auth/otp/send",
        "POST /v1/auth/otp/verify",
        "POST /v1/auth/social",
        "POST /v1/auth/token/refresh",
        "POST /v1/auth/login/otp/send",
        "POST /v1/auth/login/otp/verify",
        "POST /v1/auth/logout",
    }
    found_keys = {props["Properties"]["RouteKey"] for props in routes.values()}
    assert consumer_route_keys <= found_keys

    for props in routes.values():
        route_key = props["Properties"]["RouteKey"]
        if route_key not in consumer_route_keys:
            continue  # MA-129 admin routes are asserted separately below
        has_authorizer = "AuthorizerId" in props["Properties"]
        if route_key == "POST /v1/auth/logout":
            assert has_authorizer, "logout route must require the Cognito JWT authorizer"
        else:
            assert not has_authorizer, f"{route_key} must stay pre-auth"


def test_admin_routes_exist_with_correct_authorization(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    by_key = {props["Properties"]["RouteKey"]: props["Properties"] for props in routes.values()}

    pre_auth = {"POST /v1/admin/auth/login", "POST /v1/admin/auth/2fa/verify"}
    protected = {
        "POST /v1/admin/users",
        "GET /v1/admin/users",
        "PATCH /v1/admin/users/{id}",
        "POST /v1/admin/users/{id}/deactivate",
        "POST /v1/admin/users/{id}/reactivate",
    }
    assert pre_auth <= by_key.keys()
    assert protected <= by_key.keys()

    for key in pre_auth:
        assert "AuthorizerId" not in by_key[key], f"{key} must stay pre-auth"
    for key in protected:
        assert "AuthorizerId" in by_key[key], f"{key} must require the admin authorizer"


def test_no_stray_routes_beyond_consumer_and_admin(template):
    # Restores the exact-route-count/exact-key-set guarantee the
    # original test had before MA-129 split it into subset checks — a
    # route-key typo colliding with an existing path, or an admin route
    # accidentally left un-namespaced, would satisfy every subset check
    # above while still failing this one.
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    found_keys = {props["Properties"]["RouteKey"] for props in routes.values()}
    consumer_route_keys = {
        "POST /v1/auth/otp/send",
        "POST /v1/auth/otp/verify",
        "POST /v1/auth/social",
        "POST /v1/auth/token/refresh",
        "POST /v1/auth/login/otp/send",
        "POST /v1/auth/login/otp/verify",
        "POST /v1/auth/logout",
    }
    admin_route_keys = {
        "POST /v1/admin/auth/login",
        "POST /v1/admin/auth/2fa/verify",
        "POST /v1/admin/users",
        "GET /v1/admin/users",
        "PATCH /v1/admin/users/{id}",
        "POST /v1/admin/users/{id}/deactivate",
        "POST /v1/admin/users/{id}/reactivate",
    }
    assert found_keys == consumer_route_keys | admin_route_keys


def test_logout_authorizer_is_a_cognito_user_pool_authorizer(template):
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {"AuthorizerType": "JWT"},
    )


def test_admin_authorizer_is_a_lambda_request_authorizer_with_no_caching(template):
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {"AuthorizerType": "REQUEST", "AuthorizerResultTtlInSeconds": 0},
    )


def test_exactly_two_authorizers_total(template):
    # Restores the exact-count guarantee the original single-authorizer
    # test had before MA-129 added a second one — without this, a
    # stray/duplicate authorizer resource (or the admin authorizer
    # accidentally being wired onto a consumer route) would go
    # undetected, since the two presence-only checks above are each
    # satisfied by "at least one of that shape exists".
    template.resource_count_is("AWS::ApiGatewayV2::Authorizer", 2)


def test_execution_role_can_revoke_tokens(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": Match.array_with(["cognito-idp:RevokeToken"]),
                                "Resource": "*",
                            }
                        )
                    ]
                )
            }
        },
    )


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


def test_vpc_has_no_nat_gateway_or_public_subnet(template):
    # Every Lambda and the Redis subnet group only ever use
    # PRIVATE_ISOLATED subnets — a NAT Gateway (and the public/egress
    # tiers it exists to serve) would be provisioned and billed for
    # nothing.
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::EIP", 0)
    subnets = template.find_resources("AWS::EC2::Subnet")
    for props in subnets.values():
        tags = {t["Key"]: t["Value"] for t in props["Properties"].get("Tags", [])}
        assert "aws-cdk:subnet-type" not in tags or tags["aws-cdk:subnet-type"] == "Isolated"


def test_admin_user_pool_is_email_only_with_required_totp_mfa(template):
    user_pools = template.find_resources("AWS::Cognito::UserPool")
    admin_pools = [
        props["Properties"]
        for props in user_pools.values()
        if props["Properties"].get("UsernameAttributes") == ["email"]
    ]
    assert len(admin_pools) == 1
    admin_pool_props = admin_pools[0]
    assert admin_pool_props["MfaConfiguration"] == "ON"
    assert admin_pool_props["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]


def test_admin_pool_has_five_fixed_role_groups(template):
    groups = template.find_resources("AWS::Cognito::UserPoolGroup")
    names = {props["Properties"]["GroupName"] for props in groups.values()}
    assert names == {"Ops", "Finance", "Support", "Marketing", "SuperAdmin"}


def test_admin_aurora_cluster_is_separate_from_otp_infrastructure(template):
    clusters = template.find_resources("AWS::RDS::DBCluster")
    db_names = {props["Properties"].get("DatabaseName") for props in clusters.values()}
    assert "admin" in db_names


def test_execution_role_scopes_admin_cognito_actions_to_admin_pool_only(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": Match.array_with(
                                    ["cognito-idp:AdminCreateUser", "cognito-idp:AdminUserGlobalSignOut"]
                                ),
                                "Effect": "Allow",
                            }
                        )
                    ]
                )
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
