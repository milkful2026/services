"""Local dev only: prints the plaintext OTP from recent
`identity.otp.requested` events, so you can complete the otp/verify step
without a real SMS provider. Reads the otp-requested-debug SQS queue
that bootstrap.py wires up for exactly this — a 1-second visibility
timeout means messages reappear almost immediately, so this is safe to
run repeatedly without ever losing a message.

    python peek_otp.py                 # show recent OTPs
    python peek_otp.py +919876543210   # filter to one mobile number
"""

import json
import sys

import boto3

ENDPOINT_URL = "http://localhost:5000"
REGION = "us-east-1"


def main() -> None:
    mobile_filter = sys.argv[1] if len(sys.argv) > 1 else None
    sqs = boto3.client(
        "sqs",
        aws_access_key_id="local",
        aws_secret_access_key="local",
        region_name=REGION,
        endpoint_url=ENDPOINT_URL,
    )
    queue_url = sqs.get_queue_url(QueueName="otp-requested-debug")["QueueUrl"]
    response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, VisibilityTimeout=1)
    messages = response.get("Messages", [])
    if not messages:
        print("No OTP events yet — call POST /v1/auth/otp/send or /v1/auth/login/otp/send first.")
        return

    for message in messages:
        event = json.loads(message["Body"])
        payload = event.get("detail", {}).get("payload", {})
        mobile = payload.get("mobile")
        if mobile_filter and mobile != mobile_filter:
            continue
        print(f"mobile={mobile} otp={payload.get('otp')} template={payload.get('template')}")


if __name__ == "__main__":
    main()
