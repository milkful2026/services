"""CDK stack for the Cart service (MA-96).

Flagged architecture decisions (called out in the PR description, not
silently decided):

1. **Cross-stack Cognito reference is a placeholder** — same gap as
   `services/user/infra/user/user_stack.py`'s own module docstring point
   1. `app.py` passes placeholder values; a human must wire a real
   cross-stack reference before deploy.
2. **Cross-stack IAM grant for User's internal endpoint is NOT wired
   here.** `services/user`'s `UserStack` takes an
   `internal_caller_role_arns` constructor argument specifically so a
   caller like this one can be granted `execute-api:Invoke` on its
   internal address-state route (`HttpIamAuthorizer` — see that stack's
   own module docstring point 7). This stack's execution role ARN is
   exported via `CfnOutput` below for exactly that purpose, but actually
   adding it to `UserStack`'s `internal_caller_role_arns` is a manual,
   cross-repo/cross-deploy step a human must do — these are two separate
   CDK apps with no shared `App` instance to wire automatically. Without
   that step, every `GET /cart` call in a real deployment would fail
   with `ADDRESS_LOOKUP_UNAVAILABLE` (a 403 from API Gateway, not a
   missing route).
3. **No VPC, no Redis.** MA-121 §6/§11's Redis read-through cache is
   explicitly flagged there as needing Platform/Architecture sign-off
   before implementation — not built here, so there's nothing needing
   VPC-bound network access the way `identity-auth`'s own stack needs
   for its Redis cluster. DynamoDB and the cross-service HTTP calls
   (Catalog/User/Pricing, all fronted by their own public API Gateway
   endpoints) don't require one either.
4. **Dependency packaging**: same as `identity-auth`'s own stack —
   `Code.from_asset`, no Docker bundling. A human/CI step must attach a
   Lambda Layer (or switch to a Docker-bundled `PythonFunction`) for
   `boto3`'s non-runtime-provided siblings and `requests`/`pydantic`/
   `pydantic-settings`/`aws-lambda-powertools` before this is deployable.
5. **`CartUpdated`'s consumers aren't wired** — MA-121 §8 itself says
   none are confirmed yet (same reasoning as `identity-auth`'s own
   `OtpRequested` rule, which targets a log group instead of a real
   consumer for the same reason). This stack doesn't even create the
   rule — the publish side (outbox -> EventBridge PutEvents) works
   without one; a rule only matters once a real consumer exists to
   target.
"""

import os

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")


class CartStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cognito_user_pool_id: str,
        cognito_client_id: str,
        catalog_internal_base_url: str = "http://PLACEHOLDER-catalog-internal.local",
        user_internal_base_url: str = "http://PLACEHOLDER-user-internal.local",
        pricing_internal_base_url: str = "http://PLACEHOLDER-pricing-internal.local",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._cognito_user_pool_id = cognito_user_pool_id
        event_bus_name = "default"

        cart_table = self._build_cart_table()

        env_vars = {
            "CART_AWS_REGION": self.region,
            "CART_CART_TABLE_NAME": cart_table.table_name,
            "CART_EVENT_BUS_NAME": event_bus_name,
            "CART_CATALOG_INTERNAL_BASE_URL": catalog_internal_base_url,
            "CART_USER_INTERNAL_BASE_URL": user_internal_base_url,
            "CART_PRICING_INTERNAL_BASE_URL": pricing_internal_base_url,
        }

        execution_role = self._build_execution_role(cart_table, event_bus_name)
        common_lambda_kwargs = dict(
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset(_SRC_DIR),
            role=execution_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment=env_vars,
        )

        get_cart_fn = lambda_.Function(
            self,
            "GetCartFunction",
            handler="handlers.get_cart_handler.handler",
            **common_lambda_kwargs,
        )
        add_item_fn = lambda_.Function(
            self,
            "AddItemFunction",
            handler="handlers.add_item_handler.handler",
            **common_lambda_kwargs,
        )
        put_cart_fn = lambda_.Function(
            self,
            "PutCartFunction",
            handler="handlers.put_cart_handler.handler",
            **common_lambda_kwargs,
        )
        delete_item_fn = lambda_.Function(
            self,
            "DeleteItemFunction",
            handler="handlers.delete_item_handler.handler",
            **common_lambda_kwargs,
        )
        outbox_publisher_fn = lambda_.Function(
            self,
            "OutboxPublisherFunction",
            handler="handlers.outbox_publisher_handler.handler",
            **common_lambda_kwargs,
        )

        self._build_http_api(
            get_cart_fn, add_item_fn, put_cart_fn, delete_item_fn, cognito_client_id
        )
        self._build_outbox_scheduler(outbox_publisher_fn)

        CfnOutput(
            self,
            "ExecutionRoleArn",
            value=execution_role.role_arn,
            description=(
                "This stack's Lambda execution role ARN — see module docstring "
                "point 2. Must be added to services/user's UserStack via "
                "internal_caller_role_arns (or granted execute-api:Invoke "
                "directly against that stack's InternalAddressStateRouteArn "
                "output) before GET /cart can resolve a caller's delivery "
                "address — a manual, cross-repo step, not automated here."
            ),
        )

    def _build_cart_table(self) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            "CartTable",
            table_name="cart",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiresAt",
            removal_policy=RemovalPolicy.RETAIN,
        )
        return table

    def _build_execution_role(self, cart_table: dynamodb.Table, event_bus_name: str) -> iam.Role:
        role = iam.Role(
            self,
            "CartExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        # transact_write_items needs the same read/write actions as any
        # other DynamoDB writer — grant_read_write_data already covers it
        # (it's not a distinct IAM action from PutItem/UpdateItem/etc.).
        cart_table.grant_read_write_data(role)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/{event_bus_name}"],
            )
        )
        # execute-api:Invoke against User's internal address-state route
        # is deliberately NOT added here — see module docstring point 2.
        # This role's ARN is exported (CfnOutput above) for that grant to
        # be added on User's own side instead.
        return role

    def _build_http_api(
        self,
        get_cart_fn: lambda_.Function,
        add_item_fn: lambda_.Function,
        put_cart_fn: lambda_.Function,
        delete_item_fn: lambda_.Function,
        cognito_client_id: str,
    ) -> apigwv2.HttpApi:
        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self._cognito_user_pool_id}"
        authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "CartJwtAuthorizer", issuer, jwt_audience=[cognito_client_id]
        )

        http_api = apigwv2.HttpApi(self, "CartHttpApi", api_name="cart")
        http_api.add_routes(
            path="/cart",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "GetCartIntegration", get_cart_fn
            ),
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/cart/items",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AddItemIntegration", add_item_fn
            ),
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/cart",
            methods=[apigwv2.HttpMethod.PUT],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "PutCartIntegration", put_cart_fn
            ),
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/cart/items/{id}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "DeleteItemIntegration", delete_item_fn
            ),
            authorizer=authorizer,
        )
        return http_api

    def _build_outbox_scheduler(self, outbox_publisher_fn: lambda_.Function) -> None:
        rule = events.Rule(
            self, "OutboxPublisherSchedule", schedule=events.Schedule.rate(Duration.minutes(1))
        )
        rule.add_target(events_targets.LambdaFunction(outbox_publisher_fn))
