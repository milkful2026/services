#!/usr/bin/env python3
import aws_cdk as cdk
from cart.cart_stack import CartStack

app = cdk.App()
CartStack(
    app,
    "MilkfulCartStack",
    # Placeholders — see the stack module's docstring on the cross-stack
    # Cognito reference gap. A human must supply MA-92's real pool ID,
    # and real catalog/user/pricing internal URLs, before this is
    # deployable.
    cognito_user_pool_id="ap-south-1_PLACEHOLDER",
    cognito_client_id="PLACEHOLDER_CLIENT_ID",
)
app.synth()
