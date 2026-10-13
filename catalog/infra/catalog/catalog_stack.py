"""CDK stack for Catalog Service (MA-156/MA-42) — this service's
**first-ever** CDK/infra stack (confirmed: no `infra/` directory existed
before this story, unlike `inventory/infra/`), mirroring
`inventory/infra/inventory/inventory_stack.py`'s structure closely.

Flagged architecture decisions (same categories inventory_stack.py
already flags, called out here rather than silently inherited):

1. **No local Docker image build** — same reasoning as
   `inventory_stack.py` point 1: an empty ECR repo is created and
   referenced via `from_ecr_repository`; CI builds and pushes the real
   image (services/README.md §8).
2. **`CATALOG_DATABASE_URL` composition is not wired** — same gap as
   `inventory_stack.py` point 2. The discrete `CATALOG_DB_*` secret
   fields are injected; composing the SQLAlchemy URL from them is left
   as a human/entrypoint-script concern, not invented here.
3. **No Redis** — unlike Inventory, Catalog has no Redis-backed
   dependency anywhere in `config/env.py`; this stack provisions none.
4. **Both public AND admin routes are registered here** — a deliberate
   scope *widening* beyond MA-156 §6 point 7's literal "API Gateway
   route integrations for the five [six] admin endpoints" framing: since
   this is Catalog's first-ever stack, leaving its existing, already-
   shipped public reads (`GET /products`, `GET /products/{id}`,
   `GET /categories`, `GET /search` — MA-94/MA-116/MA-117, live since
   before this story) unregistered would mean this stack deploys a
   Catalog that the mobile app can never actually reach in a real
   environment. The public routes carry no authorizer (same posture as
   `inventory_stack.py`'s own public `/v1/serviceability/check` route —
   confirmed from `services/local-dev/README.md`'s "no auth needed"
   note on every one of these endpoints).
5. **This stack also provisions the `StockChanged` consumer's queue +
   EventBridge rule** — Catalog's `stock_changed_consumer.py` has
   existed since MA-116/117, but (same root cause as point above) never
   had a real queue/rule to consume from outside local-dev's
   `bootstrap.py`. Owned here, by the consumer's own stack — the same
   "consumer owns the inbound queue+rule" convention
   `inventory_stack.py` itself already follows for its own
   `CatalogUpdatedQueue`/`CatalogUpdatedRule` (Catalog is the producer
   there, Inventory the consumer-and-owner; the roles are reversed here).
6. **MA-156 admin routes**: gated by Identity & Auth's existing MA-129
   admin authorizer Lambda, referenced cross-stack by ARN — same
   placeholder-ARN gap `inventory_stack.py`/`user_stack.py` already
   carry (inherited, not introduced). Catalog's compute is Fargate
   behind an ALB, like Inventory's — the authorizer's `{adminId, email,
   role}` context is forwarded via `HttpAlbIntegration`'s own
   `parameter_mapping`, read back by `src/handlers/admin_context.py`,
   identical mechanism to Inventory's MA-48/MA-150 admin routes.
7. **`events:PutEvents` on the default event bus**, granted to the
   Fargate task role — `adapters/catalog_event_publisher.py`'s
   `CatalogUpdated` producer (this service's first-ever event publish)
   needs this; nothing in this (nonexistent-until-now) stack granted it
   before.
"""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class CatalogStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        # Placeholder until Identity & Auth's stack exports its real
        # MA-129 admin authorizer function ARN (module docstring point 6,
        # same gap inventory_stack.py/user_stack.py already carry). An
        # obviously-fake ARN rather than None so a forgotten wire-up
        # fails loudly at deploy time, not silently with an
        # unauthenticated admin API.
        admin_authorizer_fn_arn: str = (
            "arn:aws:lambda:ap-south-1:000000000000:function:PLACEHOLDER-admin-authorizer"
        ),
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = self._build_vpc()
        db_cluster, db_security_group = self._build_database(vpc)
        stock_changed_queue = self._build_stock_changed_queue()

        repository = ecr.Repository(
            self, "CatalogRepository", repository_name="milkful-catalog", removal_policy=RemovalPolicy.RETAIN
        )

        cluster = ecs.Cluster(self, "CatalogCluster", vpc=vpc, container_insights=True)

        task_definition, service_security_group = self._build_fargate_service(
            vpc=vpc,
            cluster=cluster,
            repository=repository,
            db_cluster=db_cluster,
            stock_changed_queue=stock_changed_queue,
        )

        db_security_group.add_ingress_rule(service_security_group, ec2.Port.tcp(5432), "Fargate -> Aurora")

        fargate_service = ecs.FargateService(
            self,
            "CatalogService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
            security_groups=[service_security_group],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        alb, listener = self._build_internal_alb(vpc, fargate_service, service_security_group)
        self._build_http_api(vpc, alb, listener, admin_authorizer_fn_arn)
        self._build_stock_changed_rule(stock_changed_queue)

    def _build_vpc(self) -> ec2.Vpc:
        return ec2.Vpc(
            self,
            "CatalogVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="private-with-egress", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private-isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )

    def _build_database(self, vpc: ec2.Vpc) -> tuple[rds.DatabaseCluster, ec2.SecurityGroup]:
        security_group = ec2.SecurityGroup(
            self, "AuroraSecurityGroup", vpc=vpc, description="Catalog Aurora Postgres"
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
            default_database_name="catalog",
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            credentials=rds.Credentials.from_generated_secret("catalog_app"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        return cluster, security_group

    def _build_stock_changed_queue(self) -> sqs.Queue:
        # MA-116 FR-5 — `stock_changed_consumer.py`'s inbound queue. Same
        # queue+DLQ shape every consumer queue in this codebase uses
        # (e.g. inventory_stack.py's ZoneUpdatedQueue).
        dlq = sqs.Queue(
            self, "StockChangedDLQ", queue_name="stock-changed-dlq", retention_period=Duration.days(14)
        )
        return sqs.Queue(
            self,
            "StockChangedQueue",
            queue_name="stock-changed",
            visibility_timeout=Duration.seconds(30),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
        )

    def _build_fargate_service(
        self,
        vpc: ec2.Vpc,
        cluster: ecs.Cluster,
        repository: ecr.Repository,
        db_cluster: rds.DatabaseCluster,
        stock_changed_queue: sqs.Queue,
    ) -> tuple[ecs.FargateTaskDefinition, ec2.SecurityGroup]:
        task_definition = ecs.FargateTaskDefinition(
            self, "CatalogTaskDef", cpu=256, memory_limit_mib=512
        )

        # See module docstring point 2 — DATABASE_URL composition from
        # these discrete fields is not wired here.
        secret = db_cluster.secret
        db_secrets = {
            "CATALOG_DB_HOST": ecs.Secret.from_secrets_manager(secret, field="host"),
            "CATALOG_DB_PORT": ecs.Secret.from_secrets_manager(secret, field="port"),
            "CATALOG_DB_USERNAME": ecs.Secret.from_secrets_manager(secret, field="username"),
            "CATALOG_DB_PASSWORD": ecs.Secret.from_secrets_manager(secret, field="password"),
        }

        container = task_definition.add_container(
            "catalog",
            image=ecs.ContainerImage.from_ecr_repository(repository, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="catalog", log_retention=logs.RetentionDays.ONE_MONTH),
            environment={
                "CATALOG_AWS_REGION": self.region,
                "CATALOG_STOCK_CHANGED_QUEUE_URL": stock_changed_queue.queue_url,
                # MA-156 (MA-42) ---------------------------------------
                "CATALOG_EVENT_BUS_NAME": "default",
                "CATALOG_EVENT_SOURCE": "catalog",
                # Placeholder — see module docstring point 2. Deploying
                # with this unresolved will fail SQLAlchemy engine
                # creation; a human must wire real composition first.
                "CATALOG_DATABASE_URL": "postgresql+psycopg2://COMPOSE_FROM_CATALOG_DB_*_ENV_VARS",
            },
            secrets=db_secrets,
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8003))

        stock_changed_queue.grant_consume_messages(task_definition.task_role)
        # MA-156 FR-1 — catalog_event_publisher.py's CatalogUpdated
        # producer; nothing in this (nonexistent-until-now) stack granted
        # PutEvents before now (module docstring point 7).
        task_definition.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/default"],
            )
        )

        service_security_group = ec2.SecurityGroup(
            self, "FargateServiceSecurityGroup", vpc=vpc, description="Catalog Fargate service"
        )
        return task_definition, service_security_group

    def _build_internal_alb(
        self, vpc: ec2.Vpc, fargate_service: ecs.FargateService, service_security_group: ec2.SecurityGroup
    ) -> tuple[elbv2.ApplicationLoadBalancer, elbv2.ApplicationListener]:
        alb_security_group = ec2.SecurityGroup(
            self, "AlbSecurityGroup", vpc=vpc, description="Catalog internal ALB"
        )
        service_security_group.add_ingress_rule(alb_security_group, ec2.Port.tcp(8003), "ALB -> Fargate")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "CatalogAlb",
            vpc=vpc,
            internet_facing=False,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        listener = alb.add_listener("CatalogListener", port=80, open=False)
        listener.add_targets(
            "CatalogTargets",
            port=8003,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[fargate_service],
            # A lightweight liveness path — NOT the real business
            # endpoint. See handlers/app.py's /healthz.
            health_check=elbv2.HealthCheck(path="/healthz"),
        )
        return alb, listener

    def _build_http_api(
        self,
        vpc: ec2.Vpc,
        alb: elbv2.ApplicationLoadBalancer,
        listener: elbv2.ApplicationListener,
        admin_authorizer_fn_arn: str,
    ) -> apigwv2.HttpApi:
        vpc_link_security_group = ec2.SecurityGroup(
            self,
            "CatalogVpcLinkSecurityGroup",
            vpc=vpc,
            description="Catalog API Gateway VPC Link",
        )
        alb.connections.allow_from(
            vpc_link_security_group, ec2.Port.tcp(80), "API Gateway VPC Link -> ALB"
        )
        vpc_link = apigwv2.VpcLink(
            self, "CatalogVpcLink", vpc=vpc, security_groups=[vpc_link_security_group]
        )
        http_api = apigwv2.HttpApi(self, "CatalogHttpApi", api_name="catalog")

        self._build_public_routes(http_api, listener, vpc_link)
        self._build_admin_routes(http_api, listener, vpc_link, admin_authorizer_fn_arn)
        return http_api

    def _build_public_routes(
        self,
        http_api: apigwv2.HttpApi,
        listener: elbv2.ApplicationListener,
        vpc_link: apigwv2.VpcLink,
    ) -> None:
        # MA-94/MA-116/MA-117 — no authorizer (module docstring point 4;
        # confirmed "no auth needed" against every one of these in
        # services/local-dev/README.md's own "Exercising the catalog"
        # section).
        for path, method, integration_id in (
            ("/products", apigwv2.HttpMethod.GET, "ListProductsIntegration"),
            ("/products/{productId}", apigwv2.HttpMethod.GET, "GetProductIntegration"),
            ("/categories", apigwv2.HttpMethod.GET, "ListCategoriesIntegration"),
            ("/search", apigwv2.HttpMethod.GET, "SearchIntegration"),
        ):
            http_api.add_routes(
                path=path,
                methods=[method],
                integration=apigwv2_integrations.HttpAlbIntegration(
                    integration_id, listener, vpc_link=vpc_link
                ),
            )

    def _build_admin_routes(
        self,
        http_api: apigwv2.HttpApi,
        listener: elbv2.ApplicationListener,
        vpc_link: apigwv2.VpcLink,
        admin_authorizer_fn_arn: str,
    ) -> None:
        # MA-156 FR-1 through FR-5 — behind Identity & Auth's own MA-129
        # admin authorizer Lambda, referenced cross-stack by ARN exactly
        # like inventory_stack.py's MA-48/MA-150 routes (module docstring
        # point 6). Caching disabled (results_cache_ttl=0) to match that
        # same precedent — a role/deactivation change must take effect on
        # the very next request.
        admin_authorizer_fn = lambda_.Function.from_function_arn(
            self, "AdminAuthorizerFunction", admin_authorizer_fn_arn
        )
        admin_authorizer = apigwv2_authorizers.HttpLambdaAuthorizer(
            "CatalogAdminAuthorizer",
            admin_authorizer_fn,
            response_types=[apigwv2_authorizers.HttpLambdaResponseType.SIMPLE],
            results_cache_ttl=Duration.seconds(0),
        )

        # Catalog's compute is Fargate behind an ALB (HttpAlbIntegration),
        # not Lambda — same header-forwarding mechanism as
        # inventory_stack.py's admin routes (module docstring point 6),
        # read back by src/handlers/admin_context.py.
        admin_parameter_mapping = apigwv2.ParameterMapping()
        admin_parameter_mapping.append_header(
            "x-admin-id", apigwv2.MappingValue.context_variable("authorizer.lambda.adminId")
        )
        admin_parameter_mapping.append_header(
            "x-admin-email", apigwv2.MappingValue.context_variable("authorizer.lambda.email")
        )
        admin_parameter_mapping.append_header(
            "x-admin-role", apigwv2.MappingValue.context_variable("authorizer.lambda.role")
        )

        def _admin_integration(integration_id: str) -> apigwv2_integrations.HttpAlbIntegration:
            return apigwv2_integrations.HttpAlbIntegration(
                integration_id, listener, vpc_link=vpc_link, parameter_mapping=admin_parameter_mapping
            )

        for path, method, integration_id in (
            ("/products", apigwv2.HttpMethod.POST, "CreateProductIntegration"),
            ("/products/{productId}", apigwv2.HttpMethod.PATCH, "UpdateProductIntegration"),
            ("/categories", apigwv2.HttpMethod.POST, "CreateCategoryIntegration"),
            ("/categories/{categoryId}", apigwv2.HttpMethod.PATCH, "UpdateCategoryIntegration"),
            ("/admin/products", apigwv2.HttpMethod.GET, "ListAdminProductsIntegration"),
            ("/products/{productId}/audit-log", apigwv2.HttpMethod.GET, "AuditLogIntegration"),
        ):
            http_api.add_routes(
                path=path,
                methods=[method],
                integration=_admin_integration(integration_id),
                authorizer=admin_authorizer,
            )

    def _build_stock_changed_rule(self, stock_changed_queue: sqs.Queue) -> None:
        # MA-116 FR-5 — matches Inventory's stock_event_publisher.py
        # (STOCK_CHANGED_DETAIL_TYPE = "inventory.stock.changed"). Same
        # RuleTargetInput unwrap every consumer-owned rule in this
        # codebase needs (inventory_stack.py's own ZoneUpdatedRule/
        # OrderCancelledRule/CatalogUpdatedRule docstrings) — a plain
        # SqsQueue target delivers EventBridge's own `{"detail": {...},
        # ...}` envelope, not the flat `{"payload": {...}}` shape
        # stock_changed_consumer.py parses.
        events.Rule(
            self,
            "StockChangedRule",
            event_pattern=events.EventPattern(
                source=["inventory"], detail_type=["inventory.stock.changed"]
            ),
            targets=[
                events_targets.SqsQueue(
                    stock_changed_queue,
                    message=events.RuleTargetInput.from_event_path("$.detail"),
                )
            ],
        )
