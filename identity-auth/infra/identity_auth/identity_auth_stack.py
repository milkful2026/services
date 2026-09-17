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

MA-129 (Admin Identity, RBAC & Session Security) additions — flagged
separately since they were added after the above, and carry their own
open questions:

6. **Second Cognito User Pool ("Admin Pool"), reusing this stack's
   existing dedicated VPC** for its new Aurora database rather than
   provisioning a third VPC — the VPC itself isn't the thing
   database-per-service governs (the *database* is never shared with
   another service; this is a brand-new Aurora cluster owned solely by
   this service).
7. **The admin API-Gateway authorizer Lambda (admin_authorizer_handler)
   needs BOTH internet egress (to fetch Cognito's public JWKS endpoint,
   same requirement `social_jwks_adapter.py` already has for Google/
   Apple) AND VPC access to reach the new admin Aurora cluster.** This
   stack's VPC has `nat_gateways=0` — no Lambda placed in it has any
   internet route at all. That is a PRE-EXISTING gap for
   `social_auth_fn` (out of scope here — additive-only constraint), but
   the new admin authorizer inherits the same problem. Not fixed here:
   a NAT Gateway + public/egress subnet (cost trade-off) or an
   alternative (e.g. a VPC-reachable JWKS cache) needs a human decision
   before real deployment — see README "what still needs a human".
8. **Admin Pool access/ID token validity: 15 minutes; refresh token: 1
   day.** Spec §12 Q5 asks for this number without providing one
   ("shorter than the consumer pool's ... recommended but not yet a
   number") — picked here as a concrete, admin-appropriate value given
   the stale-role-claim trade-off in spec §11.2, not silently left
   unset. Flagged for the architect to confirm or override.
9. **Admin JWT authorizer result caching is disabled (`results_cache_ttl
   = Duration.seconds(0)`)** — the explicit recommendation in spec
   §11.2, chosen over a short (e.g. 30s) compromise TTL since the spec
   itself says this "should not be treated as fully approved" without
   architect sign-off; disabling entirely is the conservative default
   until that sign-off happens.
10. **No endpoint here implements the "JWT refresh/logout" capability
    spec §3 lists as in-scope.** §4's FR-1..FR-6 never actually define a
    concrete `/v1/admin/auth/refresh` or `/v1/admin/auth/logout` route,
    and the task's own explicit endpoint list omits them too — not
    implemented, flagged as a real spec gap rather than invented. The
    Admin Pool app client still allows Cognito's standard
    `REFRESH_TOKEN_AUTH` flow (CDK includes it on every app client by
    default), so a token refresh is technically possible directly
    against Cognito without a backend-proxied route, but this is an
    assumption, not a confirmed contract.
"""

import os

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
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
from aws_cdk import aws_rds as rds
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

_ADMIN_ROLES = ("Ops", "Finance", "Support", "Marketing", "SuperAdmin")

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

        # --- MA-129: Admin Identity, RBAC & Session Security (additive) ---
        admin_user_pool = self._build_admin_user_pool()
        admin_app_client = admin_user_pool.add_client(
            "AdminAppClient",
            generate_secret=False,
            # Admin login is always server-mediated via AdminInitiateAuth
            # (spec FR-1's enumeration-safety + live Aurora-status-check
            # requirements) — never direct client-side USER_PASSWORD_AUTH.
            auth_flows=cognito.AuthFlow(admin_user_password=True),
            # See module docstring point 8 — a concrete number for spec
            # §12 Q5, not yet architect-confirmed.
            access_token_validity=Duration.minutes(15),
            id_token_validity=Duration.minutes(15),
            refresh_token_validity=Duration.days(1),
        )
        admin_db_cluster, admin_db_security_group = self._build_admin_database(vpc)
        admin_db_security_group.add_ingress_rule(
            lambda_security_group, ec2.Port.tcp(5432), "Lambda -> Admin Aurora"
        )
        admin_secret = admin_db_cluster.secret
        admin_db_username = admin_secret.secret_value_from_json("username").unsafe_unwrap()
        admin_db_password = admin_secret.secret_value_from_json("password").unsafe_unwrap()
        admin_db_host = admin_secret.secret_value_from_json("host").unsafe_unwrap()
        admin_db_port = admin_secret.secret_value_from_json("port").unsafe_unwrap()
        admin_db_name = admin_secret.secret_value_from_json("dbname").unsafe_unwrap()
        # --- end MA-129 provisioning that other env_vars/roles below need ---

        env_vars = {
            "IDENTITY_AUTH_COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "IDENTITY_AUTH_COGNITO_CLIENT_ID": app_client.user_pool_client_id,
            "IDENTITY_AUTH_AWS_REGION": self.region,
            "IDENTITY_AUTH_OTP_REQUESTS_TABLE_NAME": otp_table.table_name,
            "IDENTITY_AUTH_REDIS_HOST": redis_endpoint,
            "IDENTITY_AUTH_REDIS_PORT": "6379",
            "IDENTITY_AUTH_EVENT_BUS_NAME": event_bus_name,
            "IDENTITY_AUTH_ADMIN_COGNITO_USER_POOL_ID": admin_user_pool.user_pool_id,
            "IDENTITY_AUTH_ADMIN_COGNITO_CLIENT_ID": admin_app_client.user_pool_client_id,
            "IDENTITY_AUTH_ADMIN_DATABASE_URL": (
                f"postgresql+psycopg2://{admin_db_username}:{admin_db_password}"
                f"@{admin_db_host}:{admin_db_port}/{admin_db_name}"
            ),
        }

        execution_role = self._build_execution_role(otp_table, user_pool, event_bus_name, admin_user_pool)

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
        login_otp_send_fn = lambda_.Function(
            self,
            "LoginOtpSendFunction",
            handler="handlers.login_otp_send_handler.handler",
            **common_lambda_kwargs,
        )
        login_otp_verify_fn = lambda_.Function(
            self,
            "LoginOtpVerifyFunction",
            handler="handlers.login_otp_verify_handler.handler",
            **common_lambda_kwargs,
        )
        logout_fn = lambda_.Function(
            self, "LogoutFunction", handler="handlers.logout_handler.handler", **common_lambda_kwargs
        )

        # --- MA-129 Lambdas (share common_lambda_kwargs — see module
        # docstring point 7 for the authorizer's own internet-egress gap) ---
        admin_login_fn = lambda_.Function(
            self, "AdminLoginFunction", handler="handlers.admin_auth.login_handler.handler", **common_lambda_kwargs
        )
        admin_2fa_verify_fn = lambda_.Function(
            self,
            "Admin2faVerifyFunction",
            handler="handlers.admin_auth.verify_2fa_handler.handler",
            **common_lambda_kwargs,
        )
        admin_create_user_fn = lambda_.Function(
            self,
            "AdminCreateUserFunction",
            handler="handlers.admin_users.create_handler.handler",
            **common_lambda_kwargs,
        )
        admin_list_users_fn = lambda_.Function(
            self, "AdminListUsersFunction", handler="handlers.admin_users.list_handler.handler", **common_lambda_kwargs
        )
        admin_update_user_fn = lambda_.Function(
            self,
            "AdminUpdateUserFunction",
            handler="handlers.admin_users.update_handler.handler",
            **common_lambda_kwargs,
        )
        admin_deactivate_user_fn = lambda_.Function(
            self,
            "AdminDeactivateUserFunction",
            handler="handlers.admin_users.deactivate_handler.handler",
            **common_lambda_kwargs,
        )
        admin_reactivate_user_fn = lambda_.Function(
            self,
            "AdminReactivateUserFunction",
            handler="handlers.admin_users.reactivate_handler.handler",
            **common_lambda_kwargs,
        )
        admin_authorizer_fn = lambda_.Function(
            self,
            "AdminAuthorizerFunction",
            handler="handlers.admin_authorizer_handler.handler",
            **common_lambda_kwargs,
        )

        admin_fns = (
            admin_login_fn,
            admin_2fa_verify_fn,
            admin_create_user_fn,
            admin_list_users_fn,
            admin_update_user_fn,
            admin_deactivate_user_fn,
            admin_reactivate_user_fn,
            admin_authorizer_fn,
        )
        for fn in admin_fns:
            admin_secret.grant_read(fn)

        http_api = self._build_http_api(
            otp_send_fn,
            otp_verify_fn,
            social_auth_fn,
            token_refresh_fn,
            login_otp_send_fn,
            login_otp_verify_fn,
            logout_fn,
            user_pool,
            app_client,
        )
        self._build_admin_routes(
            http_api,
            admin_login_fn,
            admin_2fa_verify_fn,
            admin_create_user_fn,
            admin_list_users_fn,
            admin_update_user_fn,
            admin_deactivate_user_fn,
            admin_reactivate_user_fn,
            admin_authorizer_fn,
        )
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

    def _build_admin_user_pool(self) -> cognito.UserPool:
        pool = cognito.UserPool(
            self,
            "AdminUserPool",
            # Email-only username, unlike the consumer pool (spec §6.1:
            # "no phone, unlike the consumer pool") — a second, separate
            # pool per the explicit human decision recorded in spec §11.1.
            sign_in_aliases=cognito.SignInAliases(username=False, email=True, phone=False),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                fullname=cognito.StandardAttribute(required=False, mutable=True),
            ),
            self_sign_up_enabled=False,
            # TOTP MFA REQUIRED at the pool level (spec §6.1) — not
            # optional, unlike a typical consumer pool.
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.RETAIN,
        )
        for role in _ADMIN_ROLES:
            cognito.CfnUserPoolGroup(
                self, f"AdminGroup{role}", user_pool_id=pool.user_pool_id, group_name=role
            )
        return pool

    def _build_admin_database(self, vpc: ec2.Vpc) -> tuple[rds.DatabaseCluster, ec2.SecurityGroup]:
        """A brand-new Aurora Postgres Serverless v2 cluster owned solely
        by this service for `admin_user` (MA-129 §7) — reuses this
        stack's existing VPC (module docstring point 6) but is a wholly
        separate database from anything the consumer flow uses, and
        never shared with `user` service's own Aurora (database-per-
        service, services/README.md §1)."""
        security_group = ec2.SecurityGroup(
            self, "AdminAuroraSecurityGroup", vpc=vpc, description="Identity Auth Admin Pool Aurora Postgres"
        )
        cluster = rds.DatabaseCluster(
            self,
            "AdminAuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_4
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[security_group],
            default_database_name="admin",
            # Sized for "low hundreds of accounts" (spec §5 Scalability) —
            # same minimal Serverless v2 sizing as services/user's own
            # Aurora cluster.
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            credentials=rds.Credentials.from_generated_secret("identity_auth_admin_service"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        return cluster, security_group

    def _build_admin_routes(
        self,
        http_api: apigwv2.HttpApi,
        admin_login_fn: lambda_.Function,
        admin_2fa_verify_fn: lambda_.Function,
        admin_create_user_fn: lambda_.Function,
        admin_list_users_fn: lambda_.Function,
        admin_update_user_fn: lambda_.Function,
        admin_deactivate_user_fn: lambda_.Function,
        admin_reactivate_user_fn: lambda_.Function,
        admin_authorizer_fn: lambda_.Function,
    ) -> None:
        # Simple-response REQUEST authorizer (FR-5) — checks live
        # Aurora status/role/ip_allowlist on every call. Caching
        # disabled (module docstring point 9) per spec §11.2.
        admin_authorizer = apigwv2_authorizers.HttpLambdaAuthorizer(
            "AdminJwtAuthorizer",
            admin_authorizer_fn,
            response_types=[apigwv2_authorizers.HttpLambdaResponseType.SIMPLE],
            results_cache_ttl=Duration.seconds(0),
            identity_source=["$request.header.Authorization"],
        )

        # (path, method, function, authorizer) — login/2fa-verify are
        # pre-auth (spec §6: unauthenticated for /admin/auth/*); every
        # other admin route sits behind the authorizer above.
        AdminRouteEntry = tuple[str, apigwv2.HttpMethod, lambda_.Function, apigwv2_authorizers.HttpLambdaAuthorizer | None]
        routes: list[AdminRouteEntry] = [
            ("/v1/admin/auth/login", apigwv2.HttpMethod.POST, admin_login_fn, None),
            ("/v1/admin/auth/2fa/verify", apigwv2.HttpMethod.POST, admin_2fa_verify_fn, None),
            ("/v1/admin/users", apigwv2.HttpMethod.POST, admin_create_user_fn, admin_authorizer),
            ("/v1/admin/users", apigwv2.HttpMethod.GET, admin_list_users_fn, admin_authorizer),
            ("/v1/admin/users/{id}", apigwv2.HttpMethod.PATCH, admin_update_user_fn, admin_authorizer),
            (
                "/v1/admin/users/{id}/deactivate",
                apigwv2.HttpMethod.POST,
                admin_deactivate_user_fn,
                admin_authorizer,
            ),
            (
                "/v1/admin/users/{id}/reactivate",
                apigwv2.HttpMethod.POST,
                admin_reactivate_user_fn,
                admin_authorizer,
            ),
        ]
        for path, method, fn, authorizer in routes:
            http_api.add_routes(
                path=path,
                methods=[method],
                integration=apigwv2_integrations.HttpLambdaIntegration(f"{fn.node.id}Integration", fn),
                authorizer=authorizer,
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
        # Dedicated to this service — see module docstring point 3. Only
        # PRIVATE_ISOLATED subnets are used below (Lambdas and the Redis
        # subnet group); no public or egress-routed tier or NAT Gateway is
        # provisioned, since nothing in this stack needs internet access.
        vpc = ec2.Vpc(
            self,
            "IdentityAuthVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private-isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
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
        self,
        otp_table: dynamodb.Table,
        user_pool: cognito.UserPool,
        event_bus_name: str,
        admin_user_pool: cognito.UserPool,
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
                # Neither non-admin action supports resource-level scoping
                # (same limitation as the Admin* actions above, but these
                # two aren't Admin* so they don't even take a UserPoolId
                # in their IAM resource context).
                actions=["cognito-idp:InitiateAuth", "cognito-idp:RevokeToken"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/{event_bus_name}"],
            )
        )

        # MA-129 — Admin Pool operations, scoped to that pool's own ARN
        # only (never the consumer pool above). Same real AWS limitation
        # as point 5 in the module docstring: AdminUserGlobalSignOut and
        # the group-management actions DO support resource scoping, but
        # are listed together here for readability since they're all
        # this-pool-only actions.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminRespondToAuthChallenge",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminAddUserToGroup",
                    "cognito-idp:AdminRemoveUserFromGroup",
                    "cognito-idp:AdminUserGlobalSignOut",
                ],
                resources=[admin_user_pool.user_pool_arn],
            )
        )
        return role

    def _build_http_api(
        self,
        otp_send_fn: lambda_.Function,
        otp_verify_fn: lambda_.Function,
        social_auth_fn: lambda_.Function,
        token_refresh_fn: lambda_.Function,
        login_otp_send_fn: lambda_.Function,
        login_otp_verify_fn: lambda_.Function,
        logout_fn: lambda_.Function,
        user_pool: cognito.UserPool,
        app_client: cognito.UserPoolClient,
    ) -> apigwv2.HttpApi:
        http_api = apigwv2.HttpApi(self, "IdentityAuthHttpApi", api_name="identity-auth")

        # A Cognito JWT authorizer, used only by /auth/logout below — the
        # one authenticated route in this service (MA-21 §6: must be an
        # authenticated request to log out of). Built once here so adding
        # the next authenticated route is a one-line table entry instead
        # of a copy-pasted authorizer-plus-add_routes block.
        jwt_authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
            "IdentityAuthJwtAuthorizer", user_pool, user_pool_clients=[app_client]
        )

        # Every route this service exposes, with its authorizer (None ==
        # pre-auth, per spec §6 / MA-21 §6: user isn't authenticated yet
        # at send/verify time). One table, one loop — the next route
        # can't ship without an authorizer just by landing in the wrong
        # list.
        RouteEntry = tuple[str, lambda_.Function, apigwv2_authorizers.HttpUserPoolAuthorizer | None]
        routes: list[RouteEntry] = [
            ("/v1/auth/otp/send", otp_send_fn, None),
            ("/v1/auth/otp/verify", otp_verify_fn, None),
            ("/v1/auth/social", social_auth_fn, None),
            ("/v1/auth/token/refresh", token_refresh_fn, None),
            ("/v1/auth/login/otp/send", login_otp_send_fn, None),
            ("/v1/auth/login/otp/verify", login_otp_verify_fn, None),
            ("/v1/auth/logout", logout_fn, jwt_authorizer),
        ]
        for path, fn, authorizer in routes:
            http_api.add_routes(
                path=path,
                methods=[apigwv2.HttpMethod.POST],
                integration=apigwv2_integrations.HttpLambdaIntegration(f"{fn.node.id}Integration", fn),
                authorizer=authorizer,
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
            # $default is the literal (non-configurable) name HttpApi gives
            # its auto-created default stage; aws-cdk-lib's HttpStage has no
            # constant for it.
            resource_arn=f"arn:aws:apigateway:{self.region}::/apis/{http_api.http_api_id}/stages/$default",
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
