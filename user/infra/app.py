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
)
app.synth()
