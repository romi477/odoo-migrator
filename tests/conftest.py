import pytest
from fastapi.testclient import TestClient

from odoo_migrator.api import create_app
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.sheller import FakeShellerClient


@pytest.fixture
def compose():

    return FakeComposeEngine()


@pytest.fixture
def fake_sheller():

    return FakeShellerClient()


@pytest.fixture
def app(tmp_path, compose, fake_sheller):

    return create_app(
        tmp_path / "test.sqlite",
        compose=compose,
        sheller=fake_sheller,
    )


@pytest.fixture
def client(app):

    return TestClient(app)
