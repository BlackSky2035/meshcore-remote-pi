import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import hostguard
from app.automation import SendBudget
from app.config import Settings
from app.main import create_app
from app.node.sim import SimNode
from app.runtime import build

ALICE = "a1" * 32
BOB = "b2" * 32
REPEATER_OK = "c3" * 32     # 4010 mV
REPEATER_LOW = "d4" * 32    # 3620 mV


REAL_HOST_ALLOWED = hostguard.host_allowed
TEST_HOSTS = ("test", "t", "testserver")      # Namen der Test-Clients (httpx/Starlette)


@pytest.fixture(autouse=True)
def _test_hosts(monkeypatch):
    """Die Test-Clients schicken Host „test“/„testserver“; die echte Prüfung testet tests/test_hostguard.py."""
    monkeypatch.setattr(hostguard, "host_allowed",
                        lambda header, extra=(): hostguard.host_name(header) in TEST_HOSTS or REAL_HOST_ALLOWED(header, extra))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


@pytest_asyncio.fixture
async def rt():
    """Laufzeit mit Simulator (ohne Hintergrundverkehr), RAM-Datenbank, ohne Sendeabstand."""
    settings = Settings(mode="sim", db_path=":memory:")
    runtime = build(
        settings,
        node_factory=lambda emit: SimNode(emit, background=False),
        budget=SendBudget(min_gap_s=0, max_per_hour=1000),
    )
    await runtime.node.connect()
    yield runtime
    await runtime.hub.drain()
    runtime.raw_store.close()


@pytest_asyncio.fixture
async def client(rt):
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
