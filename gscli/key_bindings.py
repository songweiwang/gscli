import logging
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import (
    completion_is_selected,
    is_searching,
    has_completions,
    has_selection,
    vi_mode,
)

from .gsbuffer import buffer_should_be_handled

_logger = logging.getLogger(__name__)


def gscli_bindings(gscli):
    """Custom key bindings for gscli."""
    kb = KeyBindings()

    tab_insert_text = " " * 4

    @kb.add("f2")
    def _(event):
        """Enable/Disable SmartCompletion Mode."""
        _logger.debug("Detected F2 key.")
        gscli.completer.smart_completion = not gscli.completer.smart_completion

    @kb.add("f3")
    def _(event):
        """Enable/Disable Multiline Mode."""
        _logger.debug("Detected F3 key.")
        gscli.multi_line = not gscli.multi_line

    @kb.add("f4")
    def _(event):
        """Toggle between Vi and Emacs mode."""
        _logger.debug("Detected F4 key.")
        gscli.vi_mode = not gscli.vi_mode
        event.app.editing_mode = EditingMode.VI if gscli.vi_mode else EditingMode.EMACS

    @kb.add("tab")
    def _(event):
        """Force autocompletion at cursor on non-empty lines."""

        _logger.debug("Detected <Tab> key.")

        buff = event.app.current_buffer
        doc = buff.document

        if doc.on_first_line or doc.current_line.strip():
            if buff.complete_state:
                buff.complete_next()
            else:
                buff.start_completion(select_first=True)
        else:
            buff.insert_text(tab_insert_text, fire_event=False)

    @kb.add("escape", filter=has_completions)
    def _(event):
        """Force closing of autocompletion."""
        _logger.debug("Detected <Esc> key.")

        event.current_buffer.complete_state = None
        event.app.current_buffer.complete_state = None

    @kb.add("c-space")
    def _(event):
        """
        Initialize autocompletion at cursor.

        If the autocompletion menu is not showing, display it with the
        appropriate completions for the context.

        If the menu is showing, select the next completion.
        """
        _logger.debug("Detected <C-Space> key.")

        b = event.app.current_buffer
        if b.complete_state:
            b.complete_next()
        else:
            b.start_completion(select_first=False)

    @kb.add("enter", filter=completion_is_selected)
    def _(event):
        """Makes the enter key work as the tab key only when showing the menu.

        In other words, don't execute query when enter is pressed in
        the completion dropdown menu, instead close the dropdown menu
        (accept current selection).

        """
        _logger.debug("Detected enter key during completion selection.")

        event.current_buffer.complete_state = None
        event.app.current_buffer.complete_state = None

    # When using multi_line input mode the buffer is not handled on Enter (a new line is
    # inserted instead), so we force the handling if we're not in a completion or
    # history search, and one of several conditions are True
    @kb.add(
        "enter",
        filter=~(completion_is_selected | is_searching)
        & buffer_should_be_handled(gscli),
    )
    def _(event):
        _logger.debug("Detected enter key.")
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter", filter=~vi_mode)
    def _(event):
        """Introduces a line break regardless of multi-line mode or not."""
        _logger.debug("Detected alt-enter key.")
        event.app.current_buffer.insert_text("\n")

    @kb.add("c-p", filter=~has_selection)
    def _(event):
        """Move up in history."""
        event.current_buffer.history_backward(count=event.arg)

    @kb.add("c-n", filter=~has_selection)
    def _(event):
        """Move down in history."""
        event.current_buffer.history_forward(count=event.arg)

    return kb
