#!/usr/bin/env python3
import aws_cdk as cdk

from inventory.inventory_stack import InventoryStack

app = cdk.App()
InventoryStack(app, "MilkfulInventoryStack")
app.synth()
