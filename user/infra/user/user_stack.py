"""CDK stack for the User service (MA-93).

Flagged architecture decisions (called out in the PR description, not
silently decided):

1. **Cross-stack Cognito reference is a placeholder.** This stack needs
   MA-92's real Cognito User Pool ID/Client ID to build the JWT
   authorizer and the Cognito attribute-sync IAM policy — but MA-92's
   stack doesn't export them (no `CfnOutput`/SSM Parameter/cross-stack
   reference exists yet). `app.py` passes placeholder values; a human
   must wire a real cross-stack reference (SSM Parameter Store export
   from MA-92's stack is the lowest-friction option) before deploy.
2. **`custom:default_pincode` doesn't exist on MA-92's actual pool
   schema** — see `cognito_attribute_adapter.py`'s docstring. This
   stack's IAM policy for `AdminUpdateUserAttributes` is scoped
   correctly regardless, but the calls will fail until MA-92's stack
   adds that custom attribute (custom attributes are creation-time-only).
3. **Third dedicated VPC.** Same reasoning as MA-92/MA-95 — this is now
   the third service to provision its own VPC rather than share one;
   worth flagging more prominently in the PR that a real shared-VPC (or
   Transit Gateway / VPC peering) decision is overdue, not solving it
   here.
4. **`DATABASE_URL` composition.** Unlike MA-95's stack, this one composes
   it directly: `SecretValue.unsafe_unwrap()` tokens for each of the
   generated secret's discrete JSON fields (host/port/username/password/
   dbname) are interpolated into a single f-string, which CDK resolves at
   deploy time like any other token-bearing string.
5. **Inventory reachability not wired** — `inventory_client_adapter`
   needs a real URL to Inventory's internal ALB, which lives in MA-95's
   own separate VPC. Passed as a placeholder env var here.
6. **Aurora Postgres Serverless v2**, not provisioned — consistent with
   MA-95, cost-appropriate for a new, low-traffic service.
7. **Internal address-state route (MA-96) uses `HttpIamAuthorizer`, not
   network isolation.** An earlier revision of MA-96's impl plan assumed
   this route would be safe unauthenticated because it's "never exposed
   outside the VPC" — false for this service (Lambda + HttpApi has no
   VPC boundary of its own, unlike Inventory's Fargate-behind-private-ALB
   setup that reasoning was borrowed from). `HttpIamAuthorizer` means API
   Gateway rejects any request without a valid SigV4 signature from a
   principal holding `execute-api:Invoke` on this route's ARN.
   `internal_caller_role_arns` is how a caller's execution role gets that
   grant — same "placeholder until the other side exists" shape as this
   stack's own Cognito cross-stack reference (point 1 above): defaults to
   empty (route is unreachable by anyone), and a human wires Cart
   Service's real role ARN in here once MA-96's own CDK stack is
   deployed and that ARN is known.
8. **MA-139's `/v1/admin/customers*` routes reference Identity & Auth's
   MA-129 admin authorizer Lambda cross-stack, by ARN.** Per MA-139 §6
   point 2, these routes are "behind the existing admin JWT authorizer
   path (MA-129's authorizer, already role-aware — extended, not
   duplicated)" — i.e. the SAME Lambda function identity-auth's own
   stack already deploys for its own `/v1/admin/*` routes, referenced
   here via `HttpLambdaAuthorizer.from_lambda_function_arn` (API Gateway
   supports one Lambda authorizer backing routes across multiple HTTP
   APIs), not a second copy of that authorizer's Cognito/Aurora/IP-
   allowlist logic. `admin_authorizer_fn_arn` is a placeholder
   constructor parameter — same "placeholder until the other side
   exists" shape as points 1 and 7 above — a human wires identity-auth's
   real authorizer function ARN in once that stack exports it (e.g. via
   `CfnOutput`/SSM Parameter Store, the same lowest-friction option point
   1 already recommends for the Cognito pool ID export this stack also
   needs).
9. **FR-7's suspension sweep is a new, separate EventBridge Scheduler
   rule**, deliberately not reusing `_build_outbox_scheduler`'s rule —
   spec §11 point 3 flags this as a small new operational surface, kept
   apart from Subscription Service's own Daily Run schedule so the two
   unrelated jobs' failure/redrive domains aren't coupled. Runs daily at
   03:00 UTC (~08:30 IST) — an arbitrary off-peak default per spec §12.2,
   not a fixed decision; a human should confirm this doesn't collide with
   Subscription's own Daily Run cut-off window before relying on it.
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from constructs import Construct

import os

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")


class UserStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cognito_user_pool_id: str,
        cognito_client_id: str,
        inventory_internal_base_url: str = "http://PLACEHOLDER-inventory-internal-alb.local",
        internal_caller_role_arns: tuple[str, ...] = (),
        # MA-139 — placeholder until identity-auth's stack exports its
        # real MA-129 admin authorizer function ARN (module docstring
        # point 8). An obviously-fake ARN rather than None so a forgotten
        # wire-up fails loudly at deploy time (InvalidParameterValue),
        # not silently with an unauthenticated admin API.
        admin_authorizer_fn_arn: str = (
            "arn:aws:lambda:ap-south-1:000000000000:function:PLACEHOLDER-admin-authorizer"
        ),
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._cognito_user_pool_id = cognito_user_pool_id

        vpc = self._build_vpc()
        db_cluster, db_security_group = self._build_database(vpc)

        execution_role = self._build_execution_role(db_cluster)
        lambda_security_group = ec2.SecurityGroup(
            self, "LambdaSecurityGroup", vpc=vpc, description="User service Lambdas"
        )
        db_security_group.add_ingress_rule(lambda_security_group, ec2.Port.tcp(5432), "Lambda -> Aurora")

        # Composed directly from the generated secret's discrete JSON
        # fields via SecretValue tokens (resolved at deploy time) — CDK
        # supports interpolating these into an f-string like any other
        # token, so the module docstring's point 4 gap is actually
        # resolved here rather than left as a runtime-breaking placeholder.
        secret = db_cluster.secret
        db_username = secret.secret_value_from_json("username").unsafe_unwrap()
        db_password = secret.secret_value_from_json("password").unsafe_unwrap()
        db_host = secret.secret_value_from_json("host").unsafe_unwrap()
        db_port = secret.secret_value_from_json("port").unsafe_unwrap()
        db_name = secret.secret_value_from_json("dbname").unsafe_unwrap()

        common_env = {
            "USER_AWS_REGION": self.region,
            "USER_COGNITO_USER_POOL_ID": cognito_user_pool_id,
            "USER_INVENTORY_INTERNAL_BASE_URL": inventory_internal_base_url,
            "USER_EVENT_BUS_NAME": "default",
            "USER_DATABASE_URL": (
                f"postgresql+psycopg2://{db_username}:{db_password}@{db_host}:{db_port}/{db_name}"
            ),
        }
        common_lambda_kwargs = dict(
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset(_SRC_DIR),
            role=execution_role,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[lambda_security_group],
            timeout=Duration.seconds(10),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment=common_env,
        )

        register_fn = lambda_.Function(
            self, "RegisterFunction", handler="handlers.register_handler.handler", **common_lambda_kwargs
        )
        delivery_slots_fn = lambda_.Function(
            self,
            "DeliverySlotsFunction",
            handler="handlers.delivery_slots_handler.handler",
            **common_lambda_kwargs,
        )
        outbox_publisher_fn = lambda_.Function(
            self,
            "OutboxPublisherFunction",
            handler="handlers.outbox_publisher_handler.handler",
            **common_lambda_kwargs,
        )
        get_me_fn = lambda_.Function(
            self, "GetMeFunction", handler="handlers.get_me_handler.handler", **common_lambda_kwargs
        )
        address_state_fn = lambda_.Function(
            self,
            "InternalAddressStateFunction",
            handler="handlers.internal_address_state_handler.handler",
            **common_lambda_kwargs,
        )

        # MA-139 — admin customer-status endpoints (spec §4 FR-1..FR-6).
        admin_customers_list_fn = lambda_.Function(
            self,
            "AdminCustomersListFunction",
            handler="handlers.admin_customers.list_handler.handler",
            **common_lambda_kwargs,
        )
        admin_customers_detail_fn = lambda_.Function(
            self,
            "AdminCustomersDetailFunction",
            handler="handlers.admin_customers.detail_handler.handler",
            **common_lambda_kwargs,
        )
        admin_customers_suspend_fn = lambda_.Function(
            self,
            "AdminCustomersSuspendFunction",
            handler="handlers.admin_customers.suspend_handler.handler",
            **common_lambda_kwargs,
        )
        admin_customers_deactivate_fn = lambda_.Function(
            self,
            "AdminCustomersDeactivateFunction",
            handler="handlers.admin_customers.deactivate_handler.handler",
            **common_lambda_kwargs,
        )
        admin_customers_reactivate_fn = lambda_.Function(
            self,
            "AdminCustomersReactivateFunction",
            handler="handlers.admin_customers.reactivate_handler.handler",
            **common_lambda_kwargs,
        )
        admin_customers_bulk_status_fn = lambda_.Function(
            self,
            "AdminCustomersBulkStatusFunction",
            handler="handlers.admin_customers.bulk_status_handler.handler",
            **common_lambda_kwargs,
        )
        # MA-139 FR-7 — scheduled daily sweep, not an HTTP route.
        suspension_sweep_fn = lambda_.Function(
            self,
            "SuspensionSweepFunction",
            handler="handlers.suspension_sweep_handler.handler",
            **common_lambda_kwargs,
        )

        admin_customer_fns = (
            admin_customers_list_fn,
            admin_customers_detail_fn,
            admin_customers_suspend_fn,
            admin_customers_deactivate_fn,
            admin_customers_reactivate_fn,
            admin_customers_bulk_status_fn,
            suspension_sweep_fn,
        )

        for fn in (
            register_fn,
            delivery_slots_fn,
            outbox_publisher_fn,
            get_me_fn,
            address_state_fn,
            *admin_customer_fns,
        ):
            secret.grant_read(fn)

        http_api = self._build_http_api(
            register_fn,
            delivery_slots_fn,
            get_me_fn,
            address_state_fn,
            cognito_client_id,
            admin_customers_list_fn=admin_customers_list_fn,
            admin_customers_detail_fn=admin_customers_detail_fn,
            admin_customers_suspend_fn=admin_customers_suspend_fn,
            admin_customers_deactivate_fn=admin_customers_deactivate_fn,
            admin_customers_reactivate_fn=admin_customers_reactivate_fn,
            admin_customers_bulk_status_fn=admin_customers_bulk_status_fn,
            admin_authorizer_fn_arn=admin_authorizer_fn_arn,
        )
        self._grant_internal_callers(http_api, internal_caller_role_arns)
        self._build_outbox_scheduler(outbox_publisher_fn)
        self._build_suspension_sweep_scheduler(suspension_sweep_fn)

    def _build_vpc(self) -> ec2.Vpc:
        return ec2.Vpc(
            self,
            "UserVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private-isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )

    def _build_database(self, vpc: ec2.Vpc) -> tuple[rds.DatabaseCluster, ec2.SecurityGroup]:
        security_group = ec2.SecurityGroup(
            self, "AuroraSecurityGroup", vpc=vpc, description="User service Aurora Postgres"
        )
        cluster = rds.DatabaseCluster(
            self,
            "AuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[security_group],
            default_database_name="users",
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            credentials=rds.Credentials.from_generated_secret("user_service_app"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        return cluster, security_group

    def _build_execution_role(self, db_cluster: rds.DatabaseCluster) -> iam.Role:
        role = iam.Role(
            self,
            "UserExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole"),
            ],
        )

        # sub is filterable, custom attributes are not — see
        # cognito_attribute_adapter.py. Resource: "*" for ListUsers
        # because Cognito doesn't support resource-level conditions for
        # it; AdminUpdateUserAttributes IS scoped to the actual pool ARN.
        pool_arn = f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/{self._cognito_user_pool_id}"
        role.add_to_policy(
            iam.PolicyStatement(actions=["cognito-idp:ListUsers"], resources=[pool_arn])
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:AdminUpdateUserAttributes"], resources=[pool_arn]
            )
        )
        # MA-139 — additive least-privilege grant on the same consumer
        # pool ARN, per spec §1's own framing ("not a new trust
        # boundary"); IAM credentials scoped only to this pool (spec §5
        # Security), no broader Cognito access granted.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:AdminDisableUser", "cognito-idp:AdminEnableUser"],
                resources=[pool_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/default"],
            )
        )
        return role

    def _build_http_api(
        self,
        register_fn: lambda_.Function,
        delivery_slots_fn: lambda_.Function,
        get_me_fn: lambda_.Function,
        address_state_fn: lambda_.Function,
        cognito_client_id: str,
        *,
        admin_customers_list_fn: lambda_.Function,
        admin_customers_detail_fn: lambda_.Function,
        admin_customers_suspend_fn: lambda_.Function,
        admin_customers_deactivate_fn: lambda_.Function,
        admin_customers_reactivate_fn: lambda_.Function,
        admin_customers_bulk_status_fn: lambda_.Function,
        admin_authorizer_fn_arn: str,
    ) -> apigwv2.HttpApi:
        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self._cognito_user_pool_id}"
        authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "UserJwtAuthorizer", issuer, jwt_audience=[cognito_client_id]
        )

        http_api = apigwv2.HttpApi(self, "UserHttpApi", api_name="user")
        http_api.add_routes(
            path="/users/register",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration("RegisterIntegration", register_fn),
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/delivery/slots",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "DeliverySlotsIntegration", delivery_slots_fn
            ),
            authorizer=authorizer,
        )
        http_api.add_routes(
            path="/users/me",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration("GetMeIntegration", get_me_fn),
            authorizer=authorizer,
        )
        # MA-96: internal, service-to-service only — IAM (SigV4), not the
        # Cognito JWT authorizer every public route above uses. See the
        # module docstring's point 7 and internal_address_state_handler.py
        # for why this isn't (and can't be) "network isolation" instead.
        http_api.add_routes(
            path="/v1/internal/users/address-state",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "InternalAddressStateIntegration", address_state_fn
            ),
            authorizer=apigwv2_authorizers.HttpIamAuthorizer(),
        )

        # MA-139 — behind Identity & Auth's own MA-129 admin authorizer
        # Lambda, referenced cross-stack by ARN (module docstring point
        # 8) — never a second copy of that authorizer's logic. Caching
        # disabled (authorizer_result_ttl default is CDK's own 300s
        # otherwise) to match identity-auth's own admin_authorizer's
        # TTL=0 posture (spec §11.2 / identity_auth_stack.py's own
        # precedent) — a role/deactivation change must take effect on
        # the very next request, not after a cache TTL.
        admin_authorizer_fn = lambda_.Function.from_function_arn(
            self, "AdminAuthorizerFunction", admin_authorizer_fn_arn
        )
        admin_authorizer = apigwv2_authorizers.HttpLambdaAuthorizer(
            "AdminCustomersAuthorizer",
            admin_authorizer_fn,
            response_types=[apigwv2_authorizers.HttpLambdaResponseType.SIMPLE],
            results_cache_ttl=Duration.seconds(0),
        )
        http_api.add_routes(
            path="/v1/admin/customers",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AdminCustomersListIntegration", admin_customers_list_fn
            ),
            authorizer=admin_authorizer,
        )
        http_api.add_routes(
            path="/v1/admin/customers/{id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AdminCustomersDetailIntegration", admin_customers_detail_fn
            ),
            authorizer=admin_authorizer,
        )
        http_api.add_routes(
            path="/v1/admin/customers/{id}/suspend",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AdminCustomersSuspendIntegration", admin_customers_suspend_fn
            ),
            authorizer=admin_authorizer,
        )
        http_api.add_routes(
            path="/v1/admin/customers/{id}/deactivate",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AdminCustomersDeactivateIntegration", admin_customers_deactivate_fn
            ),
            authorizer=admin_authorizer,
        )
        http_api.add_routes(
            path="/v1/admin/customers/{id}/reactivate",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AdminCustomersReactivateIntegration", admin_customers_reactivate_fn
            ),
            authorizer=admin_authorizer,
        )
        http_api.add_routes(
            path="/v1/admin/customers/bulk-status",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "AdminCustomersBulkStatusIntegration", admin_customers_bulk_status_fn
            ),
            authorizer=admin_authorizer,
        )
        return http_api

    def _grant_internal_callers(
        self, http_api: apigwv2.HttpApi, internal_caller_role_arns: tuple[str, ...]
    ) -> None:
        # HttpApi has no resource policy of its own (unlike a REST API's
        # `addToResourcePolicy`) — IAM authorization is enforced entirely
        # by whether the *caller's* own identity policy grants
        # execute-api:Invoke on this route's ARN, so the grant has to be
        # added to each caller's role from here, not to this API. `*` for
        # stage matches whatever stage HttpApi's default deployment ends
        # up using, since that's not something this stack pins down.
        route_arn = (
            f"arn:{self.partition}:execute-api:{self.region}:{self.account}:"
            f"{http_api.http_api_id}/*/GET/v1/internal/users/address-state"
        )
        self.internal_address_state_route_arn = route_arn
        CfnOutput(
            self,
            "InternalAddressStateRouteArn",
            value=route_arn,
            description=(
                "execute-api ARN for MA-96's internal address-state route. "
                "A caller (e.g. Cart Service's own Lambda execution role) "
                "needs execute-api:Invoke on this ARN to call it — either "
                "list that role's ARN in this stack's internal_caller_role_arns "
                "at deploy time, or grant it directly from the caller's own "
                "stack once this output is available to import."
            ),
        )

        for i, arn in enumerate(internal_caller_role_arns):
            # mutable=True: this role is defined in a *different* stack
            # (the caller's own — e.g. Cart Service's), not this one; CDK
            # still allows attaching a policy to it from here as long as
            # it's marked mutable, since the underlying IAM role is a
            # plain account-level resource, not something this stack owns
            # exclusively.
            caller_role = iam.Role.from_role_arn(
                self, f"InternalCallerRole{i}", arn, mutable=True
            )
            caller_role.add_to_principal_policy(
                iam.PolicyStatement(actions=["execute-api:Invoke"], resources=[route_arn])
            )

    def _build_outbox_scheduler(self, outbox_publisher_fn: lambda_.Function) -> None:
        rule = events.Rule(
            self, "OutboxPublisherSchedule", schedule=events.Schedule.rate(Duration.minutes(1))
        )
        rule.add_target(events_targets.LambdaFunction(outbox_publisher_fn))

    def _build_suspension_sweep_scheduler(self, suspension_sweep_fn: lambda_.Function) -> None:
        # MA-139 FR-7 — a new, separate schedule (module docstring point
        # 9), not added to _build_outbox_scheduler's own rule above. Cron
        # at 03:00 UTC daily; see this stack's module docstring for why
        # that specific hour is a flagged default, not a fixed decision.
        rule = events.Rule(
            self,
            "SuspensionSweepSchedule",
            schedule=events.Schedule.cron(minute="0", hour="3"),
        )
        rule.add_target(events_targets.LambdaFunction(suspension_sweep_fn))
