import pytest

from domain.exceptions import (
    ExternalServiceUnavailableError,
    NotServiceableError,
    UserNotFoundError,
    ValidationError,
)
from domain.models import (
    Address,
    Consent,
    DeliverySlot,
    RegistrationRequest,
    RegistrationResult,
    UserProfile,
)
from domain.registration_service import RegistrationService


class FakeUserRepository:
    def __init__(
        self,
        result: RegistrationResult | None = None,
        slots: list[DeliverySlot] | None = None,
        existing: RegistrationResult | None = None,
        profile: UserProfile | None = None,
    ):
        self.result = result or RegistrationResult(
            user_id="user-1", default_address_id="addr-1", is_new_user=True
        )
        self.slots = slots or []
        self.existing = existing
        self.profile = profile
        self.register_calls: list[dict] = []
        self.get_by_cognito_sub_calls: list[str] = []
        self.get_profile_by_sub_calls: list[str] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def get_by_cognito_sub(self, cognito_sub: str):
        self.get_by_cognito_sub_calls.append(cognito_sub)
        return self.existing

    def register(self, **kwargs):
        self.register_calls.append(kwargs)
        return self.result

    def get_delivery_slots(self, zone_id: str):
        return self.slots

    def get_profile_by_sub(self, cognito_sub: str):
        self.get_profile_by_sub_calls.append(cognito_sub)
        return self.profile


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
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def check_serviceability(self, pincode, lat, lng):
        self.calls.append((pincode, lat, lng))
        if self.raises:
            raise self.raises
        return self.serviceable


class FakeCognitoAttributes:
    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[tuple] = []
        self.correlation_id = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

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


def test_register_success_calls_inventory_repo_and_cognito_in_order(
    service, repo, inventory, cognito
):
    result = service.register(_valid_request())

    assert result.user_id == "user-1"
    assert repo.get_by_cognito_sub_calls == ["sub-123"]
    assert inventory.calls == [("560001", 12.9716, 77.5946)]
    assert len(repo.register_calls) == 1
    assert repo.register_calls[0]["cognito_sub"] == "sub-123"
    assert cognito.calls == [("sub-123", "Priya Sharma", "560001")]


def test_register_existing_cognito_sub_short_circuits_before_inventory_or_cognito(
    repo, inventory, cognito
):
    repo.existing = RegistrationResult(
        user_id="user-1", default_address_id="addr-1", is_new_user=False
    )
    service = RegistrationService(repo, inventory, cognito)

    result = service.register(_valid_request())

    assert result.is_new_user is False
    assert inventory.calls == []
    assert repo.register_calls == []
    assert cognito.calls == []


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
    addr = Address(
        lines=["x"], city="c", state="s", pincode="560001", lat=1.0, lng=1.0, is_default=True
    )
    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[addr, addr]))


def test_register_rejects_no_default_address(service):
    addr = Address(
        lines=["x"], city="c", state="s", pincode="560001", lat=1.0, lng=1.0, is_default=False
    )
    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[addr]))


def test_register_rejects_missing_mandatory_consent(service):
    with pytest.raises(ValidationError):
        service.register(
            _valid_request(
                consents=[
                    Consent(type="TERMS", version="2026-01", accepted_at="2026-07-20T10:00:00Z")
                ]
            )
        )


def test_register_rejects_invalid_pincode_format(service, inventory):
    addr = Address(
        lines=["x"], city="c", state="s", pincode="ABCDEF", lat=1.0, lng=1.0, is_default=True
    )

    with pytest.raises(ValidationError):
        service.register(_valid_request(addresses=[addr]))

    assert inventory.calls == []  # fails fast before the inventory call


def test_register_not_serviceable_propagates_and_skips_repository(
    inventory_not_serviceable_service,
):
    service, repo = inventory_not_serviceable_service

    with pytest.raises(NotServiceableError):
        service.register(_valid_request())

    assert repo.register_calls == []


@pytest.fixture
def inventory_not_serviceable_service(repo, cognito):
    inventory = FakeInventoryClient(serviceable=False)
    return RegistrationService(repo, inventory, cognito), repo


def test_register_cognito_sync_failure_is_swallowed_registration_still_succeeds(repo, inventory):
    cognito = FakeCognitoAttributes(raises=ExternalServiceUnavailableError("cognito down"))
    service = RegistrationService(repo, inventory, cognito)

    result = service.register(_valid_request())  # must not raise

    assert result.user_id == "user-1"
    assert len(repo.register_calls) == 1


def test_register_cognito_sync_bug_is_not_swallowed(repo, inventory):
    # Only genuine external-service failures are non-fatal — an
    # unexpected bug (anything other than ExternalServiceUnavailableError)
    # must propagate loudly instead of being logged as a routine sync
    # failure.
    cognito = FakeCognitoAttributes(raises=KeyError("Username"))
    service = RegistrationService(repo, inventory, cognito)

    with pytest.raises(KeyError):
        service.register(_valid_request())


def test_register_duplicate_race_does_not_sync_cognito(repo, inventory, cognito):
    # user_repository.register() returning is_new_user=False (the
    # IntegrityError/lost-race path) must be treated the same as the
    # up-front duplicate short-circuit: never sync Cognito with data that
    # was never actually persisted.
    repo.result = RegistrationResult(
        user_id="user-1", default_address_id="addr-1", is_new_user=False
    )
    service = RegistrationService(repo, inventory, cognito)

    result = service.register(_valid_request())

    assert result.is_new_user is False
    assert cognito.calls == []


def test_set_correlation_id_cascades_to_all_collaborators(service, repo, inventory, cognito):
    service.set_correlation_id("corr-xyz")

    assert repo.correlation_id == "corr-xyz"
    assert inventory.correlation_id == "corr-xyz"
    assert cognito.correlation_id == "corr-xyz"


def test_get_delivery_slots_delegates_to_repository(inventory, cognito):
    repo = FakeUserRepository(slots=[DeliverySlot(id="morning-6-8", label="Morning 6-8 AM")])
    service = RegistrationService(repo, inventory, cognito)

    slots = service.get_delivery_slots("blr-central")

    assert slots == [DeliverySlot(id="morning-6-8", label="Morning 6-8 AM")]


def test_get_my_profile_returns_profile_from_repository(inventory, cognito):
    profile = UserProfile(
        user_id="user-1", name="Priya", mobile="+919876543210", account_type="B2C", default_address_id="addr-1"
    )
    repo = FakeUserRepository(profile=profile)
    service = RegistrationService(repo, inventory, cognito)

    result = service.get_my_profile("sub-123")

    assert result == profile
    assert repo.get_profile_by_sub_calls == ["sub-123"]


def test_get_my_profile_raises_not_found_when_repository_returns_none(inventory, cognito):
    repo = FakeUserRepository(profile=None)
    service = RegistrationService(repo, inventory, cognito)

    with pytest.raises(UserNotFoundError):
        service.get_my_profile("sub-does-not-exist")
