"""Container path to host path and back, from bind mounts. No Docker."""

from pathlib import Path
from types import SimpleNamespace

from odoo_migrator.mounts import (
    container_mounts,
    covering,
    load,
    to_container,
    to_host,
    too_broad,
)
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.provision.engine import _bind_mounts
from odoo_migrator.provision.errors import DockerUnavailable

MOUNTS = [
    {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False},
    {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": True},
    {"host": "/opt/sources/queue", "container": "/opt/external-addons/integration-queue", "read_only": True},
]


def test_a_container_path_maps_to_its_host_file():
    assert to_host(MOUNTS, "/opt/project/sale_x/models/sale.py") == Path(
        "/opt/sources/project/sale_x/models/sale.py"
    )


def test_the_longest_mount_wins():
    assert to_host(MOUNTS, "/opt/external-addons/integration-queue/q/__init__.py") == Path(
        "/opt/sources/queue/q/__init__.py"
    )


def test_a_common_string_prefix_is_not_a_parent():
    assert covering(MOUNTS, "/opt/project2/sale_x") is None
    assert to_host(MOUNTS, "/opt/project2/sale_x") is None


def test_the_mount_point_itself_is_covered():
    assert to_host(MOUNTS, "/opt/project") == Path("/opt/sources/project")


def test_a_path_under_no_mount_is_none():
    assert to_host(MOUNTS, "/usr/lib/python3/dist-packages/odoo/models.py") is None


def test_a_host_path_maps_back_to_the_container():
    assert to_container(MOUNTS, "/opt/sources/external/stock_x") == "/opt/external-addons/stock_x"
    assert to_container(MOUNTS, "/elsewhere/stock_x") is None


def test_load_ignores_garbage():
    assert load(None) == []
    assert load("not json") == []
    assert load('{"host": "/a"}') == []
    assert load('[{"host": "/a"}]') == []
    assert load('[{"host": "/a", "container": "/b", "read_only": false}]') == [
        {"host": "/a", "container": "/b", "read_only": False}
    ]


def test_too_broad_refuses_the_disk_home_and_data_dir_parents():
    home = Path("/Users/someone")
    data = home / ".odoo-migrator"

    assert too_broad("/", home=home, data_dir=data)
    assert too_broad("/Users", home=home, data_dir=data)
    assert too_broad("/Users/someone", home=home, data_dir=data)
    assert too_broad("/Users/someone/.odoo-migrator", home=home, data_dir=data)
    assert too_broad("/Users/someone/work/addons", home=home, data_dir=data) is None
    assert too_broad("/opt/sources/project", home=home, data_dir=data) is None


def test_only_bind_mounts_are_reported():
    container = SimpleNamespace(
        attrs={
            "Mounts": [
                {"Type": "bind", "Source": "/opt/sources/project", "Destination": "/opt/project", "RW": True},
                {"Type": "volume", "Source": "/var/lib/docker/volumes/x", "Destination": "/var/lib/odoo", "RW": True},
                {"Type": "bind", "Source": "/opt/conf", "Destination": "/etc/odoo", "RW": False},
            ]
        }
    )

    assert _bind_mounts(container) == [
        {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False},
        {"host": "/opt/conf", "container": "/etc/odoo", "read_only": True},
    ]


def test_container_mounts_reads_the_named_container():
    engine = FakeComposeEngine()
    engine.add_container(
        name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
        mounts=[{"host": "/opt/sources/project", "container": "/opt/project", "read_only": False}],
    )

    assert container_mounts(engine, "odoo-old") == [
        {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False}
    ]
    assert container_mounts(engine, "missing") is None


def test_an_unreachable_engine_is_not_known_rather_than_empty():
    class Down:
        def list_containers(self):
            raise DockerUnavailable("Docker Engine is not reachable: no socket")

    assert container_mounts(Down(), "odoo-old") is None
