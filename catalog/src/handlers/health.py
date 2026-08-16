"""Process-wide liveness state, shared between `main.py`'s background
StockChanged consumer thread and the `/healthz` endpoint served in the
same process — mirrors inventory's own ConsumerHealth convention exactly."""


class ConsumerHealth:
    def __init__(self) -> None:
        self.alive = True


consumer_health = ConsumerHealth()
