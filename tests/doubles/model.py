"""A model client that replays scripted turns, including one that never stops."""


class ModelDouble:
    """Stands in for the Anthropic client inside ``run_agent``.

    ``always_calls`` asks for the same tool on every turn, which is how a
    budget test proves the loop terminates. ``turns`` is a list of
    ``{"name", "input"}`` actions consumed in order; an empty list, or a
    turn with no tool, is a model that returned nothing useful.
    """

    scripted = True

    def __init__(self, *, always_calls: str | None = None, turns: list | None = None):
        self.always_calls = always_calls
        self._turns = list(turns or [])
        self.calls = 0

    def next_action(self) -> dict | None:
        self.calls += 1
        if self.always_calls:
            return {
                "name": self.always_calls,
                "input": {"side": "new", "code": "1"},
            }
        if not self._turns:
            return None
        turn = self._turns.pop(0)
        if turn is None:
            return None

        return turn
