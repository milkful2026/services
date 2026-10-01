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


def test_lambdas_exist_for_each_handler(template):
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
        # MA-139
        "handlers.admin_customers.list_handler.handler",
        "handlers.admin_customers.detail_handler.handler",
        "handlers.admin_customers.suspend_handler.handler",
        "handlers.admin_customers.deactivate_handler.handler",
        "handlers.admin_customers.reactivate_handler.handler",
        "handlers.admin_customers.bulk_status_handler.handler",
        "handlers.suspension_sweep_handler.handler",
    }


def test_aurora_serverless_v2_cluster(template):
    template.has_resource_properties(
        "AWS::RDS::DBCluster",
        {"Engine": "aurora-postgresql", "ServerlessV2ScalingConfiguration": Match.object_like({})},
    )


_ADMIN_CUSTOMER_ROUTE_KEYS = {
    "GET /v1/admin/customers",
    "GET /v1/admin/customers/{id}",
    "POST /v1/admin/customers/{id}/suspend",
    "POST /v1/admin/customers/{id}/deactivate",
    "POST /v1/admin/customers/{id}/reactivate",
    "POST /v1/admin/customers/bulk-status",
}


def test_public_routes_have_jwt_authorizer_attached(template):
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 10
    public_routes = {
        k: v
        for k, v in routes.items()
        if v["Properties"]["RouteKey"] != "GET /v1/internal/users/address-state"
        and v["Properties"]["RouteKey"] not in _ADMIN_CUSTOMER_ROUTE_KEYS
    }
    assert len(public_routes) == 3
    for props in public_routes.values():
        assert props["Properties"].get("AuthorizerId") is not None

    route_keys = {props["Properties"]["RouteKey"] for props in public_routes.values()}
    assert route_keys == {"POST /users/register", "GET /delivery/slots", "GET /users/me"}


def test_admin_customer_routes_have_a_lambda_authorizer_attached(template):
    # MA-139 — these routes must use the cross-stack Lambda REQUEST
    # authorizer (user_stack.py docstring point 8), never the plain
    # Cognito JWT authorizer every public route above uses, and never
    # unauthenticated.
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    admin_routes = {
        k: v for k, v in routes.items() if v["Properties"]["RouteKey"] in _ADMIN_CUSTOMER_ROUTE_KEYS
    }
    assert len(admin_routes) == len(_ADMIN_CUSTOMER_ROUTE_KEYS)
    # AuthorizerId is itself an intrinsic ({"Ref": ...}), not a plain
    # string — dump to JSON before deduping/hashing.
    authorizer_ids = {
        json.dumps(props["Properties"].get("AuthorizerId")) for props in admin_routes.values()
    }
    assert len(authorizer_ids) == 1
    assert json.dumps(None) not in authorizer_ids

    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    lambda_authorizers = [
        props for props in authorizers.values() if props["Properties"]["AuthorizerType"] == "REQUEST"
    ]
    assert len(lambda_authorizers) == 1
    assert lambda_authorizers[0]["Properties"].get("AuthorizerResultTtlInSeconds", 0) == 0


def test_admin_authorizer_fn_arn_default_is_still_a_placeholder(template):
    # Code-review finding #7: this stack's admin_authorizer_fn_arn
    # default (user_stack.py docstring point 8) is a known, disclosed
    # placeholder -- infra/app.py does not yet override it, so a real
    # `cdk deploy` today would wire the admin customer routes to this
    # obviously-fake ARN and fail loudly at deploy time
    # (InvalidParameterValue), rather than silently leaving the admin
    # API unauthenticated. This test intentionally fails if the default
    # ever changes without a human also updating infra/app.py to pass
    # the real cross-stack ARN -- whichever change lands first should
    # update this test too, which is the point: it forces that decision
    # to be deliberate, not an accidental default-value change.
    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    lambda_authorizers = [
        props for props in authorizers.values() if props["Properties"]["AuthorizerType"] == "REQUEST"
    ]
    assert len(lambda_authorizers) == 1
    assert "PLACEHOLDER-admin-authorizer" in json.dumps(
        lambda_authorizers[0]["Properties"]["AuthorizerUri"]
    )


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
    # MA-139 added a second (REQUEST-type, Lambda) authorizer for the
    # admin customer routes — this test only cares about the original
    # JWT one.
    jwt_authorizers = [
        props for props in authorizers.values() if props["Properties"]["AuthorizerType"] == "JWT"
    ]
    assert len(jwt_authorizers) == 1
    authorizer = jwt_authorizers[0]
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


def test_suspension_sweep_has_a_daily_schedule(template):
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "cron(0 3 * * ? *)"}
    )


def test_execution_role_scopes_admin_disable_enable_user_to_pool_arn(template):
    # MA-139 — same least-privilege posture as AdminUpdateUserAttributes:
    # scoped to the consumer pool ARN only, never a broader Cognito grant.
    policies = template.find_resources("AWS::IAM::Policy")
    statement = None
    for props in policies.values():
        for stmt in props["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action")
            if isinstance(actions, list) and set(actions) == {
                "cognito-idp:AdminDisableUser",
                "cognito-idp:AdminEnableUser",
            }:
                statement = stmt
    assert statement is not None, "no AdminDisableUser/AdminEnableUser statement found"
    assert "userpool/ap-south-1_PLACEHOLDER" in json.dumps(statement["Resource"])


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
