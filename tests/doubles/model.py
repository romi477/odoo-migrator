"""A model client that replays scripted turns, including one that never stops."""


class ModelDouble:
    """The turns a `ScriptedProvider` replays.

    ``always_calls`` asks for the same tool on every turn, which is how a
    budget test proves the loop terminates. ``turns`` is a list of
    ``{"name", "input"}`` actions consumed in order; an empty list, or a
    turn with no tool, is a model that returned nothing useful.
    """

    def __init__(
        self,
        *,
        always_calls: str | None = None,
        always_reports: bool = False,
        turns: list | None = None,
    ):
        self.always_calls = always_calls
        self.always_reports = always_reports
        self._turns = list(turns or [])
        self.calls = 0

    def next_action(self) -> dict | None:
        self.calls += 1
        if self.always_calls:
            return {
                "name": self.always_calls,
                "input": {"side": "new", "code": "1"},
            }
        if self.always_reports:
            return {"name": "report", "input": {"answer": "noted"}}
        if not self._turns:
            return None
        turn = self._turns.pop(0)
        if turn is None:
            return None

        return turn
