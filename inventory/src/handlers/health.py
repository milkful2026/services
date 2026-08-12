"""Process-wide liveness state, shared between `main.py`'s background
ZoneUpdated consumer thread and the `/healthz` endpoint served in the same
process — so the ALB health check (and thus ECS) can tell the difference
between "this task is broken" and "Aurora/Redis had a blip", instead of
either exercising the real business endpoint (which conflates the two) or
staying green forever if the consumer thread silently dies.
"""


class ConsumerHealth:
    def __init__(self) -> None:
        self.alive = True


consumer_health = ConsumerHealth()
