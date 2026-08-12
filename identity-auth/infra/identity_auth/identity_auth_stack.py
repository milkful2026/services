"""CDK stack for the Identity & Auth service (MA-92).

Flagged architecture decisions (called out in the PR description, not
silently decided):

1. Self-contained under services/identity-auth/infra/, not a new shared
   services/infrastructure/ — that folder is documented as cross-service
   shared IaC; creating it as a side effect of this one service's ticket
   would be an unapproved architecture decision.
2. OtpRequested is published to the account DEFAULT EventBridge bus, not
   a new named bus — no shared "milkful-domain-events" bus exists yet.
3. A small VPC is created here, dedicated to this service, purely so its
   Lambdas can reach ElastiCache (Redis) — no shared VPC exists yet.
4. Dependency packaging: Lambda code is bundled via plain
   `Code.from_asset` (no Docker). `aws-cdk.aws-lambda-python-alpha`'s
   `PythonFunction` would auto-bundle dependencies via Docker, which may
   not be available wherever this synths/tests — so third-party
   dependencies (boto3 is provided by the runtime; pydantic, bcrypt,
   PyJWT, redis, cachetools, requests, aws-lambda-powertools are NOT) are
   left unpackaged here. A human/CI step must attach a Lambda Layer (or
   switch to Docker-bundled PythonFunction) before this is deployable.
5. Cognito IAM: several `cognito-idp:Admin*` / `InitiateAuth` actions do
   not support resource-level conditions in IAM — AWS requires
   `Resource: "*"` for them. Least-privilege here means action-level
   scoping only, not resource-ARN scoping; this is a real AWS limitation,
   not an oversight.
"""

import os

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")


class IdentityAuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        user_pool = self._build_user_pool()
        app_client = user_pool.add_client(
            "IdentityAuthAppClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(admin_user_password=True, user_password=True),
        )

        otp_table = self._build_otp_table()
        vpc, redis_endpoint, lambda_security_group = self._build_vpc_and_redis()
        event_bus_name = "default"

        env_vars = {
            "IDENTITY_AUTH_COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "IDENTITY_AUTH_COGNITO_CLIENT_ID": app_client.user_pool_client_id,
            "IDENTITY_AUTH_AWS_REGION": self.region,
            "IDENTITY_AUTH_OTP_REQUESTS_TABLE_NAME": otp_table.table_name,
            "IDENTITY_AUTH_REDIS_HOST": redis_endpoint,
            "IDENTITY_AUTH_REDIS_PORT": "6379",
            "IDENTITY_AUTH_EVENT_BUS_NAME": event_bus_name,
        }

        execution_role = self._build_execution_role(otp_table, user_pool, event_bus_name)

        common_lambda_kwargs = dict(
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset(_SRC_DIR),
            environment=env_vars,
            role=execution_role,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[lambda_security_group],
            timeout=Duration.seconds(10),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        otp_send_fn = lambda_.Function(
            self, "OtpSendFunction", handler="handlers.otp_send_handler.handler", **common_lambda_kwargs
        )
        otp_verify_fn = lambda_.Function(
            self, "OtpVerifyFunction", handler="handlers.otp_verify_handler.handler", **common_lambda_kwargs
        )
        social_auth_fn = lambda_.Function(
            self, "SocialAuthFunction", handler="handlers.social_auth_handler.handler", **common_lambda_kwargs
        )
        token_refresh_fn = lambda_.Function(
            self,
            "TokenRefreshFunction",
            handler="handlers.token_refresh_handler.handler",
            **common_lambda_kwargs,
        )

        http_api = self._build_http_api(otp_send_fn, otp_verify_fn, social_auth_fn, token_refresh_fn)
        self._build_waf(http_api)
        self._build_otp_requested_rule(event_bus_name)

    def _build_user_pool(self) -> cognito.UserPool:
        return cognito.UserPool(
            self,
            "IdentityAuthUserPool",
            # username=False: phone_number/email are the literal Cognito
            # Username values (not just sign-in aliases on a generated
            # username) — the adapter code depends on this.
            sign_in_aliases=cognito.SignInAliases(username=False, phone=True, email=True),
            auto_verify=cognito.AutoVerifiedAttrs(phone=True, email=True),
            standard_attributes=cognito.StandardAttributes(
                phone_number=cognito.StandardAttribute(required=False, mutable=True),
                email=cognito.StandardAttribute(required=False, mutable=True),
            ),
            custom_attributes={
                "google_sub": cognito.StringAttribute(mutable=True),
                "apple_sub": cognito.StringAttribute(mutable=True),
            },
            self_sign_up_enabled=False,
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _build_otp_table(self) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            "OtpRequestsTable",
            table_name="otp_requests",
            partition_key=dynamodb.Attribute(name="requestId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.RETAIN,
        )
        table.add_global_secondary_index(
            index_name="mobile-index",
            partition_key=dynamodb.Attribute(name="mobile", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        return table

    def _build_vpc_and_redis(self) -> tuple[ec2.Vpc, str, ec2.SecurityGroup]:
        # Dedicated to this service — see module docstring point 3.
        vpc = ec2.Vpc(
            self,
            "IdentityAuthVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="private-isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private-with-egress", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
            ],
        )

        redis_security_group = ec2.SecurityGroup(
            self, "RedisSecurityGroup", vpc=vpc, description="Identity Auth Redis", allow_all_outbound=False
        )
        lambda_security_group = ec2.SecurityGroup(
            self, "LambdaSecurityGroup", vpc=vpc, description="Identity Auth Lambdas"
        )
        redis_security_group.add_ingress_rule(
            lambda_security_group, ec2.Port.tcp(6379), "Lambda -> Redis"
        )

        subnet_group = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnetGroup",
            description="Identity Auth Redis subnet group",
            subnet_ids=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids,
        )
        redis_cluster = elasticache.CfnCacheCluster(
            self,
            "RedisCluster",
            engine="redis",
            cache_node_type="cache.t3.micro",
            num_cache_nodes=1,
            vpc_security_group_ids=[redis_security_group.security_group_id],
            cache_subnet_group_name=subnet_group.ref,
        )

        redis_endpoint = redis_cluster.attr_redis_endpoint_address
        return vpc, redis_endpoint, lambda_security_group

    def _build_execution_role(
        self, otp_table: dynamodb.Table, user_pool: cognito.UserPool, event_bus_name: str
    ) -> iam.Role:
        role = iam.Role(
            self,
            "IdentityAuthExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole"),
            ],
        )

        otp_table.grant_read_write_data(role)

        # Several Admin*/InitiateAuth actions require Resource: "*" — see
        # module docstring point 5. Scoped to only the actions this
        # service actually calls, not a wildcard cognito-idp:*.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminUpdateUserAttributes",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:ListUsers",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:InitiateAuth"],
                resources=["*"],  # InitiateAuth (non-admin) does not support resource scoping
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/{event_bus_name}"],
            )
        )
        return role

    def _build_http_api(
        self,
        otp_send_fn: lambda_.Function,
        otp_verify_fn: lambda_.Function,
        social_auth_fn: lambda_.Function,
        token_refresh_fn: lambda_.Function,
    ) -> apigwv2.HttpApi:
        # No default authorizer — these 4 routes are pre-auth by
        # definition (spec §6: user isn't authenticated yet).
        http_api = apigwv2.HttpApi(self, "IdentityAuthHttpApi", api_name="identity-auth")

        routes = [
            ("/v1/auth/otp/send", otp_send_fn),
            ("/v1/auth/otp/verify", otp_verify_fn),
            ("/v1/auth/social", social_auth_fn),
            ("/v1/auth/token/refresh", token_refresh_fn),
        ]
        for path, fn in routes:
            http_api.add_routes(
                path=path,
                methods=[apigwv2.HttpMethod.POST],
                integration=apigwv2_integrations.HttpLambdaIntegration(f"{fn.node.id}Integration", fn),
            )
        return http_api

    def _build_waf(self, http_api: apigwv2.HttpApi) -> None:
        web_acl = wafv2.CfnWebACL(
            self,
            "IdentityAuthWebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="IdentityAuthWebAcl",
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitOtpEndpoints",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=100, aggregate_key_type="IP"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        sampled_requests_enabled=True,
                        cloud_watch_metrics_enabled=True,
                        metric_name="RateLimitOtpEndpoints",
                    ),
                )
            ],
        )
        wafv2.CfnWebACLAssociation(
            self,
            "IdentityAuthWebAclAssociation",
            resource_arn=(
                f"arn:aws:apigateway:{self.region}::/apis/{http_api.http_api_id}"
                f"/stages/{apigwv2.HttpStage.DEFAULT_STAGE_NAME if hasattr(apigwv2.HttpStage, 'DEFAULT_STAGE_NAME') else '$default'}"
            ),
            web_acl_arn=web_acl.attr_arn,
        )

    def _build_otp_requested_rule(self, event_bus_name: str) -> None:
        # Target left as a log group so the rule is provably wired
        # without depending on the Notification service's stack, which
        # doesn't exist yet — see module docstring point 2.
        log_group = logs.LogGroup(
            self, "OtpRequestedLogGroup", retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY
        )
        events.Rule(
            self,
            "OtpRequestedRule",
            event_bus=events.EventBus.from_event_bus_name(self, "DefaultBus", event_bus_name)
            if event_bus_name != "default"
            else None,
            event_pattern=events.EventPattern(
                source=["identity-auth"], detail_type=["identity.otp.requested"]
            ),
            targets=[events_targets.CloudWatchLogGroup(log_group)],
        )
