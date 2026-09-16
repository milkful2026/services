"""Generic exceptions for `shared/` adapters that have no single owning
service's domain-exception hierarchy to raise instead — e.g.
`shared.adapters.outbox_event_publisher`, used by both payment and
wallet's independent outbox-drain loops. Never surfaced to an HTTP
response directly (unlike each service's own `domain.exceptions`), since
nothing in `shared/` sits behind a request handler.
"""


class ServiceUnavailableError(Exception):
    pass
