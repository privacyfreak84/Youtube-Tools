"""The real keyboard prompts (ytt/ui/prompts.py), driven with the keys a person would press, through a pipe
instead of a terminal: arrows, space, enter, typing and Ctrl-C."""
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ytt.ui.prompts import GoBack, QuestionaryPrompter

DOWN, ENTER, SPACE, CTRL_C = "\x1b[B", "\r", " ", "\x03"
CHOICES = [("First", "a"), ("Second", "b"), ("Third", "c")]


class KeyboardTest(unittest.TestCase):
    def ask(self, keys, method, *args, **kw):
        """Press `keys`, then run the prompt; returns its answer (or raises what the prompt raises)."""
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return getattr(QuestionaryPrompter(input=pipe, output=DummyOutput()), method)(*args, **kw)


class SelectTests(KeyboardTest):
    def test_enter_takes_the_first_choice(self):
        self.assertEqual(self.ask(ENTER, "select", "Pick", CHOICES), "a")

    def test_arrow_down_moves_and_enter_takes_it(self):
        self.assertEqual(self.ask(DOWN + ENTER, "select", "Pick", CHOICES), "b")
        self.assertEqual(self.ask(DOWN + DOWN + ENTER, "select", "Pick", CHOICES), "c")

    def test_the_default_is_where_the_pointer_starts(self):
        self.assertEqual(self.ask(ENTER, "select", "Pick", CHOICES, default="c"), "c")

    def test_ctrl_c_goes_back(self):
        with self.assertRaises(GoBack):
            self.ask(CTRL_C, "select", "Pick", CHOICES)


class CheckboxTests(KeyboardTest):
    def test_space_ticks_and_enter_finishes(self):
        self.assertEqual(self.ask(SPACE + DOWN + DOWN + SPACE + ENTER, "checkbox", "Pick", CHOICES), ["a", "c"])

    def test_nothing_ticked_is_an_empty_list_when_that_is_allowed(self):
        self.assertEqual(self.ask(ENTER, "checkbox", "Pick", CHOICES), [])

    def test_when_one_is_required_enter_alone_asks_again_until_something_is_ticked(self):
        self.assertEqual(self.ask(ENTER + DOWN + SPACE + ENTER, "checkbox", "Pick", CHOICES, required=True), ["b"])


class TextTests(KeyboardTest):
    def test_typing_then_enter(self):
        self.assertEqual(self.ask("@Chan" + ENTER, "text", "Channel?"), "@Chan")

    def test_enter_alone_gives_the_default(self):
        self.assertEqual(self.ask(ENTER, "text", "How many?", default="15"), "15")

    def test_a_bad_answer_is_refused_with_the_message_and_asked_again(self):
        seen = []

        def validate(text):
            seen.append(text)
            return None if text.isdigit() else "a whole number please"

        self.assertEqual(self.ask("abc" + ENTER + "\x7f\x7f\x7f" + "12" + ENTER, "text", "How many?", validate=validate), "12")
        self.assertIn("abc", seen)

    def test_ctrl_c_goes_back(self):
        with self.assertRaises(GoBack):
            self.ask(CTRL_C, "text", "Channel?")


class ConfirmTests(KeyboardTest):
    def test_enter_takes_the_default(self):
        self.assertTrue(self.ask(ENTER, "confirm", "Sure?", default=True))
        self.assertFalse(self.ask(ENTER, "confirm", "Sure?", default=False))

    def test_y_and_n(self):
        self.assertTrue(self.ask("y", "confirm", "Sure?", default=False))
        self.assertFalse(self.ask("n", "confirm", "Sure?", default=True))

    def test_ctrl_c_goes_back(self):
        with self.assertRaises(GoBack):
            self.ask(CTRL_C, "confirm", "Sure?")


if __name__ == "__main__":
    unittest.main()
