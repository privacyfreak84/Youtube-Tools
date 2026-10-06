"""The questions the guided menu can ask, and the one place that talks to the `questionary` library.

The menu (ytt/ui/menu.py) only knows these four kinds of question, so tests can answer them from a script and the
real keyboard version stays small. A cancelled question (Ctrl-C) raises GoBack: the menu leaves what it was doing
and returns to the previous menu. Nothing here knows what ytt does.

choices are (label, value) pairs; a select returns the value of the chosen one and a checkbox a list of values.
validate(text) returns None when the answer is fine, or a short message saying what is wrong (the question is asked
again).
"""


class GoBack(Exception):
    """The person cancelled a question (Ctrl-C): leave this flow and return to the menu."""


class Prompter:
    def select(self, message, choices, default=None):
        raise NotImplementedError

    def checkbox(self, message, choices, required=False):
        raise NotImplementedError

    def text(self, message, default="", validate=None):
        raise NotImplementedError

    def confirm(self, message, default=True):
        raise NotImplementedError


class QuestionaryPrompter(Prompter):
    """Arrow keys and enter for choices, space to tick in a checkbox, plain typing for text.
    `input`/`output` exist so a test can feed keystrokes through a pipe instead of a terminal."""

    def __init__(self, input=None, output=None):
        self._io = {k: v for k, v in (("input", input), ("output", output)) if v is not None}

    @staticmethod
    def _answer(value):
        if value is None:                       # questionary's way of saying Ctrl-C
            raise GoBack()
        return value

    def select(self, message, choices, default=None):
        import questionary
        items = [questionary.Choice(title=label, value=value) for label, value in choices]
        chosen = next((i for i in items if i.value == default), None) if default is not None else None
        return self._answer(questionary.select(message, choices=items, default=chosen, use_jk_keys=False,
                                               **self._io).ask())

    def checkbox(self, message, choices, required=False):
        import questionary
        items = [questionary.Choice(title=label, value=value) for label, value in choices]

        def check(picked):
            return True if picked or not required else "Pick at least one (space ticks, enter continues)."

        return self._answer(questionary.checkbox(message, choices=items, validate=check, **self._io).ask())

    def text(self, message, default="", validate=None):
        import questionary

        def check(text):
            problem = validate(text) if validate else None
            return True if problem is None else problem

        return self._answer(questionary.text(message, default=default, validate=check, **self._io).ask())

    def confirm(self, message, default=True):
        import questionary
        return self._answer(questionary.confirm(message, default=default, **self._io).ask())
