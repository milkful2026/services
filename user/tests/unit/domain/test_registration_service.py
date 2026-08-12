import pytest

from domain.exceptions import NotServiceableError, ValidationError
from domain.models import Address, Consent, DeliverySlot, RegistrationRequest, RegistrationResult
from domain.registration_service import RegistrationService


class FakeUserRepository:
    def __init__(self, result: RegistrationResult | None = None, slots: list[DeliverySlot] | None = None):
        self.result = result or RegistrationResult(
            user_id="user-1", default_address_id="addr-1", is_new_user=True
        )
        self.slots = slots or []
        self.register_calls: list[dict] = []

    def register(self, **kwargs):
        self.register_calls.append(kwargs)
        return self.result

    def get_delivery_slots(self, zone_id: str):
        return self.slots


class FakeInventoryClient:
    """Mirrors the real HttpInventoryClient's contract: returns a bool,
    never raises NotServiceableError itself — the domain service is
    responsible for turning `False` into that exception. An earlier
    version of this fake raised on `serviceable=False`, which masked a
    real bug where registration_service.register() called
    check_serviceability() but never checked its return value."""

    def __init__(self, serviceable: bool = True, raises: Exception | None = None):
        self.serviceable = serviceable
        self.raises = raises
        self.calls: list[tuple] = []

    def check_serviceability(self, pincode, lat, lng):
        self.calls.append((pincode, lat, lng))
        if self.raises:
            raise self.raises
        return self.serviceable


class FakeCognitoAttributes:
    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[tuple] = []

    def sync_profile_attributes(self, cognito_sub, name, default_pincode):
        self.calls.append((cognito_sub, name, default_pincode))
        if self.raises:
            raise self.raises


def _valid_request(**overrides) -> RegistrationRequest:
    defaults = dict(
        cognito_sub="sub-123",
        mobile="+919876543210",
        name="Priya Sharma",
        addresses=[
            Address(
                lines=["12 MG Road"],
                city="Bangalore",
                state="Karnataka",
                pincode="560001",
                lat=12.9716,
                lng=77.5946,
                is_default=True,
            )
        ],
        consents=[
            Consent(type="TERMS", version="2026-01", accepted_at="2026-07-20T10:00:00Z"),
            Consent(type="PRIVACY", version="2026-01", accepted_at="2026-07-20T10:00:00Z"),
        ],
    )
    defaults.update(overrides)
    return RegistrationRequest(**defaults)


@pytest.fixture
def repo():
    return FakeUserRepository()


@pytest.fixture
def inventory():
    return FakeInventoryClient()


@pytest.fixture
def cognito():
    return FakeCognitoAttributes()


@pytest.fixture
def service(repo, inventory, cognito):
    return RegistrationService(repo, inventory, cognito)


def test_register_success_calls_inventory_repo_and_cognito_in_order(service, repo, inventory, cognito):
    result = service.register(_valid_request())

    assert result.user_id == "user-1"
    assert inventory.calls == [("560001", 12.9716, 77.5946)]
    assert len(repo.register_calls) == 1
    assert repo.register_calls[0]["cognito_sub"] == "sub-123"
    assert cognito.calls == [("sub-123", "Priya Sharma", "560001")]


def test_register_rejects_name_too_short(service):
    with pytest.raises(ValidationError):
        service.register(_valid_request(name="A"))


def test_register_rejects_name_too_long(service):
    with pytest.raises(ValidationError):
        service.register(_valid_request(name="A" * 101))


def test_register_rejects_zero_addresses(service):
    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[]))


def test_register_rejects_multiple_addresses(service):
    addr = Address(lines=["x"], city="c", state="s", pincode="560001", lat=1.0, lng=1.0, is_default=True)
    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[addr, addr]))


def test_register_rejects_no_default_address(service):
    addr = Address(lines=["x"], city="c", state="s", pincode="560001", lat=1.0, lng=1.0, is_default=False)
    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[addr]))


def test_register_rejects_missing_mandatory_consent(service):
    with pytest.raises(ValidationError):
        service.register(
            _valid_request(
                consents=[Consent(type="TERMS", version="2026-01", accepted_at="2026-07-20T10:00:00Z")]
            )
        )


def test_register_rejects_invalid_pincode_format(service, inventory):
    addr = Address(lines=["x"], city="c", state="s", pincode="ABCDEF", lat=1.0, lng=1.0, is_default=True)

    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[addr]))

    assert inventory.calls == []  # fails fast before the inventory call


def test_register_not_serviceable_propagates_and_skips_repository(inventory_not_serviceable_service):
    service, repo = inventory_not_serviceable_service

    with pytest.raises(NotServiceableError):
        service.register(_valid_request())

    assert repo.register_calls == []


@pytest.fixture
def inventory_not_serviceable_service(repo, cognito):
    inventory = FakeInventoryClient(serviceable=False)
    return RegistrationService(repo, inventory, cognito), repo


def test_register_cognito_sync_failure_is_swallowed_registration_still_succeeds(repo, inventory):
    cognito = FakeCognitoAttributes(raises=Exception("cognito down"))
    service = RegistrationService(repo, inventory, cognito)

    result = service.register(_valid_request())  # must not raise

    assert result.user_id == "user-1"
    assert len(repo.register_calls) == 1


def test_get_delivery_slots_delegates_to_repository(inventory, cognito):
    repo = FakeUserRepository(slots=[DeliverySlot(id="morning-6-8", label="Morning 6-8 AM")])
    service = RegistrationService(repo, inventory, cognito)

    slots = service.get_delivery_slots("blr-central")

    assert slots == [DeliverySlot(id="morning-6-8", label="Morning 6-8 AM")]
