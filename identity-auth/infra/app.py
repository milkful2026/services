#!/usr/bin/env python3
import aws_cdk as cdk

from identity_auth.identity_auth_stack import IdentityAuthStack

app = cdk.App()
IdentityAuthStack(app, "MilkfulIdentityAuthStack")
app.synth()
