"""A stand-in for the keyboard: answers the menu's questions from a script and remembers what was asked.

Answers go by value (what the menu gets back), so a select is answered with a choice's value, a checkbox with a list
of values, a text with a string and a confirm with True/False. An answer that is not on offer fails the test: the
script cannot "choose" something the person could not. A text answer that fails the menu's own validation is
recorded in `refused` and the next answer is used instead, as when a person is told to try again.
"""
from ytt.ui.prompts import GoBack, Prompter

CANCEL = object()           # Ctrl-C


class ScriptedPrompter(Prompter):
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []             # (kind, message, labels, default)
        self.refused = []           # (message, answer, why) for text answers the menu's validation turned down

    def _next(self, kind, message, labels, default=None):
        self.asked.append((kind, message, list(labels), default))
        if not self.answers:
            raise AssertionError(f"the menu asked {message!r} but the script has no answers left")
        answer = self.answers.pop(0)
        if answer is CANCEL:
            raise GoBack()
        return answer

    def select(self, message, choices, default=None):
        answer = self._next("select", message, [label for label, _ in choices], default)
        values = [value for _, value in choices]
        if answer not in values:
            raise AssertionError(f"{message!r}: the script chose {answer!r}, which is not on offer ({values})")
        return answer

    def checkbox(self, message, choices, required=False):
        answer = self._next("checkbox", message, [label for label, _ in choices])
        values = [value for _, value in choices]
        bad = [a for a in answer if a not in values]
        if bad:
            raise AssertionError(f"{message!r}: the script ticked {bad}, which are not on offer ({values})")
        if required and not answer:
            raise AssertionError(f"{message!r}: needs at least one tick")
        return list(answer)

    def text(self, message, default="", validate=None):
        while True:
            answer = self._next("text", message, [], default)
            problem = validate(answer) if validate else None
            if problem is None:
                return answer
            self.refused.append((message, answer, problem))

    def confirm(self, message, default=True):
        return bool(self._next("confirm", message, [], default))

    def everything_shown(self):
        """Every question and choice label the person was shown, as one string."""
        return "\n".join(m + "\n" + "\n".join(labels) for _, m, labels, _ in self.asked)
