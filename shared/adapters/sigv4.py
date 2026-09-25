"""SigV4-signs a request using the caller's own AWS execution-role
credentials (default boto3 credential chain — no explicit key material
handled here). Used only by adapters whose target route is protected by
API Gateway's AWS_IAM authorizer (see cart/order's own
user_client_adapter.py docstrings for why those routes need it and
every other inter-service adapter in this codebase doesn't).

Was hand-duplicated byte-for-byte in cart/order's own
adapters/user_client_adapter.py — moved here per services/README.md §2.
`sign_request` (MA-136) generalizes it to a body-carrying POST; the body
bytes signed here must be exactly the bytes the caller then sends.
"""

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def sign_request(
    method: str,
    url: str,
    region_name: str,
    params: dict[str, str] | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, str]:
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials available to sign the request")
    request = AWSRequest(method=method, url=url, params=params, data=body, headers=headers)
    SigV4Auth(credentials, "execute-api", region_name).add_auth(request)
    return dict(request.headers)


def sign_get_request(url: str, params: dict[str, str], region_name: str) -> dict[str, str]:
    return sign_request("GET", url, region_name, params=params)
