"""SigV4-signs a GET request using the caller's own AWS execution-role
credentials (default boto3 credential chain — no explicit key material
handled here). Used only by adapters whose target route is protected by
API Gateway's AWS_IAM authorizer (see cart/order's own
user_client_adapter.py docstrings for why this one route needs it and
every other inter-service adapter in this codebase doesn't).

Was hand-duplicated byte-for-byte in cart/order's own
adapters/user_client_adapter.py — moved here per services/README.md §2.
"""

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def sign_get_request(url: str, params: dict[str, str], region_name: str) -> dict[str, str]:
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials available to sign the request")
    request = AWSRequest(method="GET", url=url, params=params)
    SigV4Auth(credentials, "execute-api", region_name).add_auth(request)
    return dict(request.headers)
