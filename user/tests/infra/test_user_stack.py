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


def test_five_lambdas_exist_for_the_five_handlers(template):
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
        "handlers.get_me_handler.handler",
        "handlers.internal_address_state_handler.handler",
    }


def test_aurora_serverless_v2_cluster(template):
    template.has_resource_properties(
        "AWS::RDS::DBCluster",
        {"Engine": "aurora-postgresql", "ServerlessV2ScalingConfiguration": Match.object_like({})},
    )


def test_public_routes_have_jwt_authorizer_attached(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 4
    public_routes = {
        k: v
        for k, v in routes.items()
        if v["Properties"]["RouteKey"] != "GET /v1/internal/users/address-state"
    }
    assert len(public_routes) == 3
    for props in public_routes.values():
        assert props["Properties"].get("AuthorizerId") is not None

    route_keys = {props["Properties"]["RouteKey"] for props in public_routes.values()}
    assert route_keys == {"POST /users/register", "GET /delivery/slots", "GET /users/me"}


def test_internal_address_state_route_uses_iam_not_jwt(template):
    # MA-96: this route must never end up on the same JWT authorizer as
    # the public routes above (or, worse, no authorizer at all) — see
    # user_stack.py's docstring point 7 for why "network isolation" isn't
    # a real boundary for a Lambda + HttpApi service.
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    internal_routes = [
        v for v in routes.values() if v["Properties"]["RouteKey"] == "GET /v1/internal/users/address-state"
    ]
    assert len(internal_routes) == 1
    props = internal_routes[0]["Properties"]
    assert props["AuthorizationType"] == "AWS_IAM"
    # AWS_IAM is a built-in HttpApi authorization type, not a custom
    # Authorizer resource — unlike the JWT routes, this one must NOT
    # reference an AuthorizerId at all.
    assert props.get("AuthorizerId") is None


def test_internal_address_state_route_arn_is_exported(template):
    outputs = template.to_json().get("Outputs", {})
    assert "InternalAddressStateRouteArn" in outputs


def test_internal_caller_role_arn_grants_execute_api_invoke():
    # Separate stack instance (not the shared `template` fixture) since
    # this needs a non-default constructor argument.
    app = cdk.App()
    stack = UserStack(
        app,
        "MilkfulUserStackWithCaller",
        cognito_user_pool_id="ap-south-1_PLACEHOLDER",
        cognito_client_id="PLACEHOLDER_CLIENT_ID",
        internal_caller_role_arns=("arn:aws:iam::123456789012:role/SomeCallerRole",),
    )
    caller_template = Template.from_stack(stack)

    policies = caller_template.find_resources("AWS::IAM::Policy")
    matching = [
        stmt
        for props in policies.values()
        for stmt in props["Properties"]["PolicyDocument"]["Statement"]
        if stmt.get("Action") == "execute-api:Invoke"
    ]
    assert len(matching) == 1
    assert "v1/internal/users/address-state" in json.dumps(matching[0]["Resource"])


def test_no_internal_caller_role_arns_means_nobody_is_granted(template):
    # Default (empty) internal_caller_role_arns — the whole point of the
    # placeholder-until-Cart-exists design (user_stack.py docstring point
    # 7) is that this route is unreachable by anyone until a caller is
    # explicitly listed.
    policies = template.find_resources("AWS::IAM::Policy")
    matching = [
        stmt
        for props in policies.values()
        for stmt in props["Properties"]["PolicyDocument"]["Statement"]
        if stmt.get("Action") == "execute-api:Invoke"
    ]
    assert matching == []


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


def test_database_url_is_composed_not_a_dead_placeholder(template):
    # USER_DB_HOST/PORT/USERNAME env vars used to be injected separately
    # but nothing ever read them (config.env.Settings has no such
    # fields) — USER_DATABASE_URL itself must now be the real,
    # secret-composed connection string instead.
    functions = template.find_resources("AWS::Lambda::Function")
    for props in functions.values():
        env_vars = props["Properties"].get("Environment", {}).get("Variables", {})
        if "USER_DATABASE_URL" not in env_vars:
            continue
        assert "USER_DB_HOST" not in env_vars
        assert "USER_DB_PORT" not in env_vars
        assert "USER_DB_USERNAME" not in env_vars
        db_url = json.dumps(env_vars["USER_DATABASE_URL"])
        assert "COMPOSE_FROM_USER_DB" not in db_url
        assert "postgresql+psycopg2://" in db_url


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
