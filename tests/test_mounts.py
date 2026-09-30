"""Container path to host path and back, from bind mounts. Pure: no Docker."""

from pathlib import Path

from odoo_migrator.mounts import covering, load, to_container, to_host, too_broad

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
