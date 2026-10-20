"""Compose provisioning: up / restart / rebuild / stop, without a Docker Engine."""

from pathlib import Path

import pytest

from odoo_migrator.provision import (
    ComposeMissing,
    FakeComposeEngine,
    NothingRunning,
    PortStolen,
    count_running,
    describe_side,
    find_compose_file,
    published_host_ports,
    rebuild,
    restart,
    stop,
    up,
)
from odoo_migrator.provision.engine import WORKING_DIR_LABEL


def _compose(project: Path, *, port: int = 8069, name: str = "compose.yaml") -> Path:
    project.mkdir(parents=True, exist_ok=True)
    path = project / name
    path.write_text(
        "services:\n"
        "  odoo:\n"
        "    image: odoo:17.0\n"
        "    ports:\n"
        f'      - "{port}:8069"\n',
        encoding="utf-8",
    )

    return path


def _foreign(engine: FakeComposeEngine, *, port: int = 8069, name: str = "someone-elses-odoo"):
    engine.add_container(
        name=name,
        image="odoo:17.0",
        status="running",
        labels={},
        host_ports=[port],
    )


def test_compose_yaml_is_preferred_over_docker_compose_yml(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    preferred = tmp_path / "compose.yaml"
    preferred.write_text("services: {}\n", encoding="utf-8")

    assert find_compose_file(tmp_path) == preferred


def test_published_ports_from_short_and_long_syntax(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  odoo:\n"
        "    image: odoo:17.0\n"
        "    ports:\n"
        '      - "8069:8069"\n'
        "      - target: 5432\n"
        "        published: 5433\n"
        '      - "127.0.0.1:8071:8069"\n',
        encoding="utf-8",
    )

    assert published_host_ports(tmp_path / "compose.yaml") == [8069, 5433, 8071]


def test_up_without_a_compose_file_is_refused(tmp_path):
    engine = FakeComposeEngine()

    with pytest.raises(ComposeMissing, match="compose"):
        up(engine, tmp_path)

    assert engine.ups == []


def test_up_starts_ours_and_records_the_odoo_container(tmp_path):
    _compose(tmp_path)
    engine = FakeComposeEngine()
    result = up(engine, tmp_path)

    assert result.state == "running"
    assert "odoo" in result.container.lower()
    assert engine.ups == [tmp_path.resolve()]
    assert count_running(engine, [tmp_path]).ours == 1


def test_up_when_already_running_does_not_compose_again(tmp_path):
    _compose(tmp_path)
    engine = FakeComposeEngine()
    first = up(engine, tmp_path)
    second = up(engine, tmp_path)

    assert first.container == second.container
    assert engine.ups == [tmp_path.resolve()]


def test_up_does_not_steal_someone_elses_port(tmp_path):
    _compose(tmp_path, port=8069)
    engine = FakeComposeEngine()
    _foreign(engine, port=8069)

    with pytest.raises(PortStolen, match="not part of this project"):
        up(engine, tmp_path)

    assert engine.ups == []
    assert [c.name for c in engine.containers] == ["someone-elses-odoo"]


def test_stop_stops_ours_and_the_side_reads_stopped(tmp_path):
    _compose(tmp_path)
    engine = FakeComposeEngine()
    up(engine, tmp_path)

    result = stop(engine, tmp_path)

    assert engine.stops == [tmp_path.resolve()]
    assert result.state == "stopped"
    assert count_running(engine, [tmp_path]).ours == 0


def test_stop_with_nothing_running_is_a_no_op_not_an_error(tmp_path):
    """Stop on an idle side should not raise the way restart does; there is
    simply nothing to stop, and the side already reads stopped."""
    _compose(tmp_path)
    engine = FakeComposeEngine()

    result = stop(engine, tmp_path)

    assert engine.stops == []
    assert result.state == "stopped"


def test_stop_without_a_compose_file_is_refused(tmp_path):
    engine = FakeComposeEngine()

    with pytest.raises(ComposeMissing, match="compose"):
        stop(engine, tmp_path)

    assert engine.stops == []


def test_restart_and_rebuild_need_ours_and_do_not_touch_a_foreign_container(tmp_path):
    _compose(tmp_path)
    empty = FakeComposeEngine()

    with pytest.raises(NothingRunning):
        restart(empty, tmp_path)
    with pytest.raises(NothingRunning):
        rebuild(empty, tmp_path)

    engine = FakeComposeEngine()
    up(engine, tmp_path)
    restart(engine, tmp_path)
    rebuild(engine, tmp_path)

    assert engine.restarts == [tmp_path.resolve()]
    assert engine.rebuilds == [tmp_path.resolve()]

    stolen = FakeComposeEngine()
    _foreign(stolen, port=8069)
    with pytest.raises(PortStolen):
        restart(stolen, tmp_path)
    assert [c.name for c in stolen.containers] == ["someone-elses-odoo"]


def test_describe_side_missing_compose_is_a_state_not_a_crash(tmp_path):
    status = describe_side(FakeComposeEngine(), tmp_path)

    assert status.state == "missing_compose"
    assert status.container == ""


def test_count_attributes_containers_instead_of_guessing_from_names(tmp_path):
    """The old count matched "odoo" in the name, and the previous version of
    this test used a container called "postgres" so that it passed. A real
    host runs one called ``odoo-postgres``, which that count reported as an
    Odoo server while missing the Odoo container itself.

    Attribution is by the compose working directory, which is a fact rather
    than a guess.
    """
    ours = tmp_path / "acme-old"
    ours.mkdir()
    engine = FakeComposeEngine()
    engine.add_container(
        name="odoo-postgres",
        image="pgvector/pgvector:pg16",
        status="running",
        labels={},
        host_ports=[5432],
    )
    engine.add_container(
        name="acme-old-odoo-1",
        image="odoo:17.0",
        status="running",
        labels={WORKING_DIR_LABEL: str(ours)},
        host_ports=[8069],
    )
    engine.add_container(
        name="acme-old-db-1",
        image="postgres:16",
        status="running",
        labels={WORKING_DIR_LABEL: str(ours)},
        host_ports=[],
    )
    engine.add_container(
        name="stopped-odoo",
        image="odoo:18.0",
        status="exited",
        labels={WORKING_DIR_LABEL: str(ours)},
        host_ports=[],
    )
    counted = count_running(engine, [ours])

    assert counted.ours == 2
    assert counted.total == 3


def test_a_port_range_does_not_crash_the_page(tmp_path):
    """"8000-8010:8000-8010" is legal compose. int() on it raised ValueError,
    uncaught, and the settings page answered 500."""
    project = tmp_path / "ranged"
    project.mkdir()
    (project / "compose.yaml").write_text(
        'services:\n  odoo:\n    image: odoo:17\n    ports: ["8000-8002:8000-8002"]\n',
        encoding="utf-8",
    )

    assert published_host_ports(project / "compose.yaml") == [8000, 8001, 8002]

    status = describe_side(FakeComposeEngine(), project)

    assert status.state == "stopped"


def test_short_syntax_publishes_no_known_host_port(tmp_path):
    """``ports: ["8069"]`` lets compose pick an ephemeral host port. Reading it
    as host port 8069 invented a clash with whatever really held 8069."""
    project = tmp_path / "ephemeral"
    project.mkdir()
    (project / "compose.yaml").write_text(
        'services:\n  odoo:\n    image: odoo:17\n    ports: ["8069"]\n',
        encoding="utf-8",
    )

    assert published_host_ports(project / "compose.yaml") == []

    engine = FakeComposeEngine()
    engine.add_container(
        name="somebody-else",
        image="nginx",
        status="running",
        labels={},
        host_ports=[8069],
    )

    assert describe_side(engine, project).state == "stopped"


def test_the_other_side_of_the_same_migration_is_named_as_the_clash(tmp_path):
    """Two Odoo compose files copied from one project both publish 8069, so
    this is the likeliest clash of all. Calling it "someone else's" sends the
    reader looking for a stranger."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old)
    _compose(new)
    engine = FakeComposeEngine()
    up(engine, old, sibling_path=new)

    status = describe_side(engine, new, sibling_path=old)

    assert status.state == "port_stolen"
    assert "other side of this migration" in status.detail
    assert "different host ports" in status.detail

    with pytest.raises(PortStolen, match="other side of this migration"):
        up(engine, new, sibling_path=old)
