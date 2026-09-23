"""Tool catalog. A cartridge picks from this and cannot extend it.

``run_test`` is allowed on the old session as well as the new one: the stock
flow lists it while reading the module on the source container.
"""

TOOL_SIDES = {
    "exec": ("old", "new"),
    "source": ("old", "new"),
    "journal": ("old", "new"),
    "history": ("old", "new"),
    "run_test": ("old", "new"),
    "list_tests": ("old", "new"),
    "test_result": ("new",),
    "interrupt": ("old", "new"),
    "rollback": ("old", "new"),
    "reopen": ("new",),
    "read_file": ("old", "new"),
    "write_file": ("new",),
}
