"""One reading of a daemon's dotted version."""

from odoo_migrator.sheller import FakeShellerClient, ShellerError
from odoo_migrator.sheller.version import CLICKBOT_SINCE, daemon_version, parse_version


def test_a_dotted_version_is_a_tuple_of_numbers_and_leaves_out_what_is_not_one():
    assert parse_version("1.13.2") == (1, 13, 2)
    assert parse_version("1.13.2-rc1") == (1, 13)
    assert parse_version("") == ()
    assert CLICKBOT_SINCE == (1, 12, 0)


def test_the_version_is_the_one_the_daemon_reports():
    assert daemon_version(FakeShellerClient(version="1.11.0")) == (1, 11, 0)


def test_a_daemon_that_does_not_answer_has_no_version_and_that_is_not_an_error():
    class Silent:
        def health(self):
            raise ShellerError("down")

    assert daemon_version(Silent()) == ()
