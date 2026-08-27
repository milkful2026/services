"""CDK stack for the Inventory service (MA-95).

Flagged architecture decisions (called out in the PR description, not
silently decided):

1. **No local Docker image build.** `ecs.ContainerImage.from_asset()`
   requires a running Docker daemon at `cdk synth`/deploy time — not
   guaranteed available (confirmed unavailable in this build's own
   environment: the CLI exists but the daemon isn't reachable). This
   stack creates an empty ECR repository and references it via
   `from_ecr_repository`; the actual image is built and pushed by CI
   (services/README.md §8's documented pipeline: build → scan → push
   versioned artifact → deploy), not by this CDK stack.
2. **`DATABASE_URL` composition is not wired.** Aurora's
   `Credentials.from_generated_secret()` produces a Secrets Manager
   secret with separate JSON fields (host, port, username, password,
   dbname) — CDK's ECS `Secret` injection maps one field to one env var,
   it cannot compose them into a single SQLAlchemy URL. The task
   definition below injects the discrete fields as `INVENTORY_DB_*` env
   vars; composing `INVENTORY_DATABASE_URL` from them is left as an
   application-startup or entrypoint-script concern for a human to wire,
   not invented here.
3. **Internal endpoint auth is network-level**, not literal mTLS — see
   `src/handlers/internal_serviceability_check_handler.py`'s docstring.
   The internal path is simply never added to the API Gateway route
   table; it's reachable only by hitting the internal ALB directly from
   within the VPC, restricted by security group.
4. **Self-contained under `services/inventory/infra/`**, not a shared
   `services/infrastructure/` — same reasoning as MA-92's stack.
5. **Aurora Postgres Serverless v2** (cost-appropriate for a new,
   low-traffic service) rather than a provisioned cluster.
"""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class InventoryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = self._build_vpc()
        db_cluster, db_security_group = self._build_database(vpc)
        redis_endpoint, redis_security_group = self._build_redis(vpc)
        zone_updated_queue = self._build_queue()

        repository = ecr.Repository(
            self, "InventoryRepository", repository_name="milkful-inventory", removal_policy=RemovalPolicy.RETAIN
        )

        cluster = ecs.Cluster(self, "InventoryCluster", vpc=vpc, container_insights=True)

        task_definition, service_security_group = self._build_fargate_service(
            vpc=vpc,
            cluster=cluster,
            repository=repository,
            db_cluster=db_cluster,
            redis_endpoint=redis_endpoint,
            zone_updated_queue=zone_updated_queue,
        )

        db_security_group.add_ingress_rule(service_security_group, ec2.Port.tcp(5432), "Fargate -> Aurora")
        redis_security_group.add_ingress_rule(service_security_group, ec2.Port.tcp(6379), "Fargate -> Redis")

        fargate_service = ecs.FargateService(
            self,
            "InventoryService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
            security_groups=[service_security_group],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        alb, listener = self._build_internal_alb(vpc, fargate_service, service_security_group)
        self._build_http_api(vpc, alb, listener)
        self._build_zone_updated_rule(zone_updated_queue)

    def _build_vpc(self) -> ec2.Vpc:
        return ec2.Vpc(
            self,
            "InventoryVpc",
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
            self, "AuroraSecurityGroup", vpc=vpc, description="Inventory Aurora Postgres"
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
            default_database_name="inventory",
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            credentials=rds.Credentials.from_generated_secret("inventory_app"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        return cluster, security_group

    def _build_redis(self, vpc: ec2.Vpc) -> tuple[str, ec2.SecurityGroup]:
        security_group = ec2.SecurityGroup(self, "RedisSecurityGroup", vpc=vpc, description="Inventory Redis")
        subnet_group = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnetGroup",
            description="Inventory Redis subnet group",
            subnet_ids=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids,
        )
        redis_cluster = elasticache.CfnCacheCluster(
            self,
            "RedisCluster",
            engine="redis",
            cache_node_type="cache.t3.micro",
            num_cache_nodes=1,
            vpc_security_group_ids=[security_group.security_group_id],
            cache_subnet_group_name=subnet_group.ref,
        )
        return redis_cluster.attr_redis_endpoint_address, security_group

    def _build_queue(self) -> sqs.Queue:
        dlq = sqs.Queue(self, "ZoneUpdatedDLQ", queue_name="zone-updated-dlq", retention_period=Duration.days(14))
        return sqs.Queue(
            self,
            "ZoneUpdatedQueue",
            queue_name="zone-updated",
            visibility_timeout=Duration.seconds(30),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
        )

    def _build_fargate_service(
        self,
        vpc: ec2.Vpc,
        cluster: ecs.Cluster,
        repository: ecr.Repository,
        db_cluster: rds.DatabaseCluster,
        redis_endpoint: str,
        zone_updated_queue: sqs.Queue,
    ) -> tuple[ecs.FargateTaskDefinition, ec2.SecurityGroup]:
        task_definition = ecs.FargateTaskDefinition(
            self, "InventoryTaskDef", cpu=256, memory_limit_mib=512
        )

        # See module docstring point 2 — DATABASE_URL composition from
        # these discrete fields is not wired here.
        secret = db_cluster.secret
        db_secrets = {
            "INVENTORY_DB_HOST": ecs.Secret.from_secrets_manager(secret, field="host"),
            "INVENTORY_DB_PORT": ecs.Secret.from_secrets_manager(secret, field="port"),
            "INVENTORY_DB_USERNAME": ecs.Secret.from_secrets_manager(secret, field="username"),
            "INVENTORY_DB_PASSWORD": ecs.Secret.from_secrets_manager(secret, field="password"),
        }

        container = task_definition.add_container(
            "inventory",
            image=ecs.ContainerImage.from_ecr_repository(repository, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="inventory", log_retention=logs.RetentionDays.ONE_MONTH),
            environment={
                "INVENTORY_AWS_REGION": self.region,
                "INVENTORY_REDIS_HOST": redis_endpoint,
                "INVENTORY_REDIS_PORT": "6379",
                "INVENTORY_ZONE_UPDATED_QUEUE_URL": zone_updated_queue.queue_url,
                # Placeholder — see module docstring point 2. Deploying
                # with this unresolved will fail SQLAlchemy engine
                # creation; a human must wire real composition first.
                "INVENTORY_DATABASE_URL": "postgresql+psycopg2://COMPOSE_FROM_INVENTORY_DB_*_ENV_VARS",
            },
            secrets=db_secrets,
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8000))

        zone_updated_queue.grant_consume_messages(task_definition.task_role)

        service_security_group = ec2.SecurityGroup(
            self, "FargateServiceSecurityGroup", vpc=vpc, description="Inventory Fargate service"
        )
        return task_definition, service_security_group

    def _build_internal_alb(
        self, vpc: ec2.Vpc, fargate_service: ecs.FargateService, service_security_group: ec2.SecurityGroup
    ) -> tuple[elbv2.ApplicationLoadBalancer, elbv2.ApplicationListener]:
        alb_security_group = ec2.SecurityGroup(
            self, "AlbSecurityGroup", vpc=vpc, description="Inventory internal ALB"
        )
        # Only the ALB's own security group (and, once created, other
        # in-VPC services like User Service) may reach the Fargate tasks —
        # see module docstring point 3 for the internal-route auth model.
        service_security_group.add_ingress_rule(alb_security_group, ec2.Port.tcp(8000), "ALB -> Fargate")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "InventoryAlb",
            vpc=vpc,
            internet_facing=False,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        listener = alb.add_listener("InventoryListener", port=80, open=False)
        listener.add_targets(
            "InventoryTargets",
            port=8000,
            targets=[fargate_service],
            # A lightweight liveness path — NOT the real business endpoint,
            # which queries Aurora/Redis and would turn a transient DB blip
            # into ECS cycling the (single) task. See handlers/app.py.
            health_check=elbv2.HealthCheck(path="/healthz"),
        )
        return alb, listener

    def _build_http_api(
        self, vpc: ec2.Vpc, alb: elbv2.ApplicationLoadBalancer, listener: elbv2.ApplicationListener
    ) -> apigwv2.HttpApi:
        # A VpcLink with no explicit security_groups gets an
        # auto-generated one CDK doesn't hand back a reference to, leaving
        # no way to grant it ingress on the ALB — so it's created
        # explicitly here purely to be the source of that ingress rule.
        vpc_link_security_group = ec2.SecurityGroup(
            self,
            "InventoryVpcLinkSecurityGroup",
            vpc=vpc,
            description="Inventory API Gateway VPC Link",
        )
        alb.connections.allow_from(
            vpc_link_security_group, ec2.Port.tcp(80), "API Gateway VPC Link -> ALB"
        )
        vpc_link = apigwv2.VpcLink(
            self, "InventoryVpcLink", vpc=vpc, security_groups=[vpc_link_security_group]
        )
        http_api = apigwv2.HttpApi(self, "InventoryHttpApi", api_name="inventory")

        # Public route only — the internal route is intentionally never
        # registered here (see module docstring point 3).
        http_api.add_routes(
            path="/v1/serviceability/check",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpAlbIntegration(
                "InventoryAlbIntegration", listener, vpc_link=vpc_link
            ),
        )
        return http_api

    def _build_zone_updated_rule(self, zone_updated_queue: sqs.Queue) -> None:
        # Matches events the (not-yet-built) Admin/zone-management service
        # will publish — this stack owns the consumer side of the
        # contract (rule + queue), same pattern as MA-92's OtpRequested
        # rule targeting a not-yet-existing Notification service.
        events.Rule(
            self,
            "ZoneUpdatedRule",
            event_pattern=events.EventPattern(
                source=["inventory-admin"], detail_type=["inventory.zone.updated"]
            ),
            targets=[events_targets.SqsQueue(zone_updated_queue)],
        )
