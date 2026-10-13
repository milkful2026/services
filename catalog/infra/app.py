#!/usr/bin/env python3
import aws_cdk as cdk

from catalog.catalog_stack import CatalogStack

app = cdk.App()
CatalogStack(app, "MilkfulCatalogStack")
app.synth()
