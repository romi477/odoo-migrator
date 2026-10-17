"""Which test methods a directory defines: what a port must carry across."""

from odoo_migrator.test_names import test_method_names as method_names


def _tree(tmp_path, files):
    tests = tmp_path / "tests"
    tests.mkdir()
    for name, body in files.items():
        (tests / name).write_text(body, encoding="utf-8")

    return tests


def test_the_names_are_the_methods_whose_name_says_test(tmp_path):
    tests = _tree(
        tmp_path,
        {
            "test_a.py": (
                "class TestA:\n    def test_one(self): pass\n    def helper(self): pass\n"
                "    def test_two(self): pass\n"
            ),
            "__init__.py": "from . import test_a\n",
        },
    )

    assert method_names(tests) == {"test_one", "test_two"}


def test_every_file_and_every_class_counts(tmp_path):
    tests = _tree(
        tmp_path,
        {
            "test_a.py": "class A:\n    def test_a(self): pass\n",
            "test_b.py": "class B:\n    def test_b(self): pass\nclass C:\n    def test_c(self): pass\n",
        },
    )

    assert method_names(tests) == {"test_a", "test_b", "test_c"}


def test_a_file_that_does_not_parse_has_no_names_and_does_not_stop_the_rest(tmp_path):
    tests = _tree(
        tmp_path,
        {"test_ok.py": "class T:\n    def test_ok(self): pass\n", "test_bad.py": "class T(:\n"},
    )

    assert method_names(tests) == {"test_ok"}


def test_a_directory_that_is_not_there_has_no_names(tmp_path):
    assert method_names(tmp_path / "missing") == set()


def test_bytecode_is_not_read(tmp_path):
    tests = _tree(tmp_path, {})
    (tests / "__pycache__").mkdir()
    (tests / "__pycache__" / "test_x.cpython-312.pyc").write_bytes(b"\0")

    assert method_names(tests) == set()
