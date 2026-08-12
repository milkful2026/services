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
4. **`DATABASE_URL` composition not wired** — same gap as MA-95's stack,
   same reason (Aurora's generated secret has separate fields; ECS/Lambda
   `Secret` injection is one-field-per-env-var).
5. **Inventory reachability not wired** — `inventory_client_adapter`
   needs a real URL to Inventory's internal ALB, which lives in MA-95's
   own separate VPC. Passed as a placeholder env var here.
6. **Aurora Postgres Serverless v2**, not provisioned — consistent with
   MA-95, cost-appropriate for a new, low-traffic service.
"""

from aws_cdk import Duration, RemovalPolicy, Stack
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

        common_env = {
            "USER_AWS_REGION": self.region,
            "USER_COGNITO_USER_POOL_ID": cognito_user_pool_id,
            "USER_INVENTORY_INTERNAL_BASE_URL": inventory_internal_base_url,
            "USER_EVENT_BUS_NAME": "default",
            # Placeholder — see module docstring point 4.
            "USER_DATABASE_URL": "postgresql+psycopg2://COMPOSE_FROM_USER_DB_*_SECRETS",
        }
        # DB host/port/username are injected below via add_environment on
        # each function using SecretValue tokens (resolved at deploy
        # time) — never baked into a plain `environment` dict, since
        # that's evaluated at synth time and can't hold a Secrets
        # Manager-resolved value.
        secret = db_cluster.secret
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

        for fn in (register_fn, delivery_slots_fn, outbox_publisher_fn):
            # unsafe_unwrap() is CDK's documented mechanism for exactly
            # this — embedding a Secrets Manager dynamic reference
            # ({{resolve:secretsmanager:...}}) into a Lambda env var,
            # resolved at deploy time, not baked in at synth time.
            fn.add_environment("USER_DB_HOST", secret.secret_value_from_json("host").unsafe_unwrap())
            fn.add_environment("USER_DB_PORT", secret.secret_value_from_json("port").unsafe_unwrap())
            fn.add_environment(
                "USER_DB_USERNAME", secret.secret_value_from_json("username").unsafe_unwrap()
            )
            secret.grant_read(fn)

        http_api = self._build_http_api(register_fn, delivery_slots_fn, cognito_client_id)
        self._build_outbox_scheduler(outbox_publisher_fn)

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
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/default"],
            )
        )
        return role

    def _build_http_api(
        self, register_fn: lambda_.Function, delivery_slots_fn: lambda_.Function, cognito_client_id: str
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
        return http_api

    def _build_outbox_scheduler(self, outbox_publisher_fn: lambda_.Function) -> None:
        rule = events.Rule(
            self, "OutboxPublisherSchedule", schedule=events.Schedule.rate(Duration.minutes(1))
        )
        rule.add_target(events_targets.LambdaFunction(outbox_publisher_fn))
