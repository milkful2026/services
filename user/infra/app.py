#!/usr/bin/env python3
import aws_cdk as cdk

from user.user_stack import UserStack

app = cdk.App()
UserStack(
    app,
    "MilkfulUserStack",
    # Placeholder — see the stack module's docstring on the cross-stack
    # Cognito reference gap. A human must supply MA-92's real pool ID
    # before this is deployable.
    cognito_user_pool_id="ap-south-1_PLACEHOLDER",
    cognito_client_id="PLACEHOLDER_CLIENT_ID",
    # internal_caller_role_arns defaults to empty — MA-96's internal
    # address-state route (see user_stack.py docstring point 7) is
    # unreachable by anyone until Cart Service's own CDK stack exists and
    # its real Lambda execution role ARN is added here.
)
app.synth()
