"""Process-wide liveness, shared between main.py's background SQS
consumer thread and the /healthz endpoint served in the same process —
mirrors catalog/inventory's ConsumerHealth convention."""


class ConsumerHealth:
    def __init__(self) -> None:
        self.alive = True


consumer_health = ConsumerHealth()
