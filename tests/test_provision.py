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


def _built(tmp_path, *, dockerfile="FROM python:3.12\nCOPY project/requirements.txt /r.txt\nCOPY project/ /opt/project\n"):
    import os

    (tmp_path / "compose.yaml").write_text(
        "services:\n  odoo:\n    build:\n      context: .\n      additional_contexts:\n"
        f"        src: {tmp_path / 'src'}\n"
        '    ports:\n      - "8069:8069"\n',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        dockerfile + "COPY --from=src requirements.txt /odoo.txt\nCOPY --from=builder /x /y\n",
        encoding="utf-8",
    )
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "requirements.txt").write_text("lxml\n", encoding="utf-8")
    for path in (
        tmp_path / "compose.yaml",
        tmp_path / "Dockerfile",
        tmp_path / "project" / "requirements.txt",
        tmp_path / "src" / "requirements.txt",
    ):
        os.utime(path, (1000, 1000))


def _running_ours(engine, project, *, created, image_built):
    from odoo_migrator.provision.engine import WORKING_DIR_LABEL

    engine.images["sha256:img"] = image_built
    engine.add_container(
        name="proj-odoo-1",
        image="proj-odoo",
        status="running",
        labels={WORKING_DIR_LABEL: str(project.resolve())},
        host_ports=[8069],
        created=created,
        image_id="sha256:img",
    )


def test_build_inputs_are_the_dockerfile_and_the_files_it_copies(tmp_path):
    from odoo_migrator.provision.compose_file import build_inputs

    _built(tmp_path)

    found = build_inputs(tmp_path / "compose.yaml")

    assert found["container"] == [tmp_path / "compose.yaml"]
    # A copied directory is not: every edit of a module would ask for a
    # rebuild. Nor is a stage of the Dockerfile.
    assert found["image"] == [
        (tmp_path / "Dockerfile").resolve(),
        (tmp_path / "project" / "requirements.txt").resolve(),
        (tmp_path / "src" / "requirements.txt").resolve(),
    ]


def test_a_running_side_built_before_its_inputs_changed_names_them(tmp_path):
    import os

    _built(tmp_path)
    engine = FakeComposeEngine()
    _running_ours(engine, tmp_path, created=2000, image_built=2000)

    assert describe_side(engine, tmp_path).stale == ()

    os.utime(tmp_path / "project" / "requirements.txt", (3000, 3000))
    assert describe_side(engine, tmp_path).stale == ("project/requirements.txt",)

    os.utime(tmp_path / "compose.yaml", (3000, 3000))
    assert describe_side(engine, tmp_path).stale == ("compose.yaml", "project/requirements.txt")


def test_a_container_recreated_from_an_old_image_is_not_fresh(tmp_path):
    """Compose recreates a container from the image it has: the container is
    new, and the Dockerfile edit is still not in it."""
    import os

    _built(tmp_path)
    os.utime(tmp_path / "Dockerfile", (3000, 3000))
    engine = FakeComposeEngine()
    _running_ours(engine, tmp_path, created=5000, image_built=2000)

    assert describe_side(engine, tmp_path).stale == ("Dockerfile",)


def test_a_time_it_cannot_read_says_nothing(tmp_path):
    import os

    _built(tmp_path)
    os.utime(tmp_path / "Dockerfile", (3000, 3000))
    engine = FakeComposeEngine()
    _running_ours(engine, tmp_path, created=None, image_built=2000)
    engine.images.clear()

    assert describe_side(engine, tmp_path).stale == ()


def test_docker_times_are_read_past_their_nanoseconds():
    from odoo_migrator.provision.engine import _unix_time

    assert _unix_time("2026-10-17T09:12:33.123456789Z") == _unix_time("2026-10-17T09:12:33Z") + 0.123456
    assert _unix_time("2026-10-17T11:12:33.5+02:00") == _unix_time("2026-10-17T09:12:33Z") + 0.5
    assert _unix_time("") is None and _unix_time("soon") is None


def test_a_compose_file_touched_but_not_changed_asks_for_nothing(tmp_path):
    """Sandbox 18: compose.yaml was saved after the container was made and
    gave the service the same hash. A comment is not a reason to rebuild."""
    import os

    from odoo_migrator.provision.engine import CONFIG_HASH_LABEL, SERVICE_LABEL

    _built(tmp_path)
    os.utime(tmp_path / "compose.yaml", (3000, 3000))
    engine = FakeComposeEngine()
    _running_ours(engine, tmp_path, created=2000, image_built=2000)
    engine.containers[0].labels.update({SERVICE_LABEL: "odoo", CONFIG_HASH_LABEL: "abc"})

    engine.config_hashes["odoo"] = "abc"
    assert describe_side(engine, tmp_path).stale == ()

    engine.config_hashes["odoo"] = "def"
    assert describe_side(engine, tmp_path).stale == ("compose.yaml",)
