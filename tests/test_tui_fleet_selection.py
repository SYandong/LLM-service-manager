# Generated-By: Codex / gpt-6.1-sol
"""Real mouse-event coverage of snapshot-preserving fleet text selection."""

import asyncio

import pytest
from rich.text import Text

pytest.importorskip("textual")
from textual import events
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from tui.fleet_selection import SelectableStatic


class AllocationText(SelectableStatic):
    def __init__(self, content):
        super().__init__(content, id="selection", markup=False)
        self.clicks = 0

    def on_click(self, event):
        if self.consume_selection_click(event):
            return
        self.clicks += 1


class SelectionApp(App):
    CSS = """
    #scroll { width: 14; height: 10; }
    #selection { width: 10; height: auto; }
    """

    def __init__(self, content):
        super().__init__()
        self.panel = AllocationText(content)

    def compose(self):
        with VerticalScroll(id="scroll"):
            yield self.panel
        yield Static("Outside", id="outside")


async def drag(pilot, start, end):
    await pilot.mouse_down("#selection", offset=start)
    await pilot.hover("#selection", offset=end)
    await pilot.mouse_up("#selection", offset=end)


@pytest.mark.parametrize("start,end,expected", [
    ((0, 0), (6, 1), "alpha beta gamma"),
    ((6, 1), (0, 0), "alpha beta gamma"),
    ((6, 0), (6, 1), "beta gamma"),
])
def test_wrapped_selection_copies_source_without_inserted_newlines(start, end, expected):
    async def scenario():
        app = SelectionApp("alpha beta gamma")
        async with app.run_test(size=(40, 18)) as pilot:
            await drag(pilot, start, end)
            assert app.panel.selected_text == expected
            assert app.panel.has_selection and not app.panel.dragging
            assert app.mouse_captured is None
            assert app.panel.clicks == 0
            assert isinstance(app.panel.render(), Text)
            assert app.panel.render().plain == "alpha beta gamma"
            assert any(span.style == "reverse" for span in app.panel.render().spans)
            assert any(segment.style and segment.style.reverse
                       for segment in app.panel.render_line(0))
    asyncio.run(scenario())


@pytest.mark.parametrize("start,end,expected", [
    ((0, 0), (8, 1), "GPU 猫犬\n次行 end"),
    ((8, 1), (0, 0), "GPU 猫犬\n次行 end"),
    ((5, 0), (8, 0), "猫犬"),
    ((8, 0), (5, 0), "猫犬"),
])
def test_multiline_and_wide_characters_use_terminal_cell_offsets(start, end, expected):
    async def scenario():
        app = SelectionApp("GPU 猫犬\n次行 end")
        async with app.run_test(size=(40, 18)) as pilot:
            await drag(pilot, start, end)
            assert app.panel.selected_text == expected
    asyncio.run(scenario())


def test_combining_characters_remain_with_their_base_character():
    async def scenario():
        app = SelectionApp("e\u0301猫 z")
        async with app.run_test(size=(40, 18)) as pilot:
            await drag(pilot, (0, 0), (3, 0))
            assert app.panel.selected_text == "e\u0301猫"
    asyncio.run(scenario())


def test_wide_characters_wrap_and_copy_without_inserting_newlines():
    async def scenario():
        app = SelectionApp("猫犬鳥魚犬猫終")
        async with app.run_test(size=(40, 18)) as pilot:
            assert app.panel.render_line(0).text == "猫犬鳥魚犬"
            assert app.panel.render_line(1).text.rstrip() == "猫終"
            await drag(pilot, (1, 0), (4, 1))
            assert app.panel.selected_text == "猫犬鳥魚犬猫終"
    asyncio.run(scenario())


def test_tabs_blank_lines_and_final_newline_preserve_source_characters():
    async def scenario():
        app = SelectionApp("a\t猫b\n\nlast\n")
        async with app.run_test(size=(40, 18)) as pilot:
            await drag(pilot, (0, 0), (0, 4))
            assert app.panel.selected_text == "a\t猫b\n\nlast\n"
    asyncio.run(scenario())


def test_mouse_offsets_exclude_panel_border_and_padding():
    async def scenario():
        app = SelectionApp("alpha beta gamma")
        app.panel.styles.width = 16
        app.panel.styles.border = ("solid", "white")
        app.panel.styles.padding = (1, 2)
        async with app.run_test(size=(40, 18)) as pilot:
            await drag(pilot, (3, 2), (9, 3))
            assert app.panel.selected_text == "alpha beta gamma"
            await pilot.click("#selection", offset=(0, 0))
            assert not app.panel.dragging
    asyncio.run(scenario())


def test_refreshes_keep_drag_and_selection_snapshot_until_clear():
    async def scenario():
        source = Text("alpha beta gamma", style="green")
        app = SelectionApp(source)
        source.append(" external mutation")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(0, 0))
            assert app.panel.dragging and app.mouse_captured is app.panel
            app.panel.update(Text("first refresh", style="blue"))
            await pilot.hover("#selection", offset=(6, 1))
            app.panel.update(Text("newest refresh", style="bold yellow"))
            await pilot.mouse_up("#selection", offset=(6, 1))
            assert app.panel.selected_text == "alpha beta gamma"
            assert app.panel.render().plain == "alpha beta gamma"
            app.panel.update("latest after release")
            assert app.panel.selected_text == "alpha beta gamma"
            app.panel.clear_selection()
            await pilot.pause()
            assert not app.panel.has_selection
            assert app.panel.render().plain == "latest after release"
            assert app.mouse_captured is None
    asyncio.run(scenario())


def test_click_without_drag_applies_pending_refresh_and_allows_allocation_click():
    async def scenario():
        app = SelectionApp("alpha beta")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(2, 0))
            app.panel.update("refreshed")
            await pilot.mouse_up("#selection", offset=(2, 0))
            assert app.panel.render().plain == "refreshed"
            assert not app.panel.has_selection
            await pilot.click("#selection", offset=(2, 0))
            assert app.panel.clicks == 1
    asyncio.run(scenario())


def test_synthetic_click_after_drag_cannot_activate_an_allocation():
    async def scenario():
        app = SelectionApp("alpha beta")
        async with app.run_test(size=(40, 18)) as pilot:
            await drag(pilot, (0, 0), (5, 0))
            await pilot._post_mouse_events([events.Click], "#selection", (5, 0), button=1)
            assert app.panel.clicks == 0
            await pilot.click("#selection", offset=(2, 0))
            assert app.panel.clicks == 1
    asyncio.run(scenario())


def test_cancelling_a_press_before_mouse_movement_suppresses_its_click():
    async def scenario():
        app = SelectionApp("alpha beta")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(2, 0))
            app.panel.clear_selection()
            await pilot.mouse_up("#selection", offset=(2, 0))
            await pilot._post_mouse_events([events.Click], "#selection", (2, 0), button=1)
            assert app.panel.clicks == 0
            assert app.mouse_captured is None
            await pilot.click("#selection", offset=(2, 0))
            assert app.panel.clicks == 1
    asyncio.run(scenario())


def test_captured_drag_beyond_the_panel_clamps_to_source_bounds():
    async def scenario():
        app = SelectionApp("alpha beta gamma\nlast")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(0, 0))
            await pilot.hover(offset=(30, 15))
            assert app.panel.selected_text == "alpha beta gamma\nlast"
            await pilot.mouse_up(offset=(30, 15))
            assert app.mouse_captured is None
            assert not app.panel.dragging
    asyncio.run(scenario())


def test_losing_capture_cancels_without_releasing_another_widgets_capture():
    async def scenario():
        app = SelectionApp("alpha beta")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(0, 0))
            await pilot.hover("#selection", offset=(5, 0))
            app.panel.update("pending")
            other = app.query_one("#outside")
            other.capture_mouse()
            await pilot.pause()
            assert app.mouse_captured is other
            assert not app.panel.dragging and not app.panel.has_selection
            assert app.panel.render().plain == "pending"
            other.release_mouse()
    asyncio.run(scenario())


def test_stale_release_after_recapturing_cannot_cancel_the_current_drag():
    async def scenario():
        app = SelectionApp("alpha beta")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(0, 0))
            app.panel.release_mouse()
            app.panel.capture_mouse()
            await pilot.pause()
            assert app.panel.dragging and app.mouse_captured is app.panel
            await pilot.mouse_up("#selection", offset=(5, 0))
            assert app.panel.selected_text == "alpha"
            assert app.mouse_captured is None
    asyncio.run(scenario())


@pytest.mark.parametrize("end_state", ["clear", "hide", "remove"])
def test_cancellation_and_teardown_release_capture_without_late_updates(end_state):
    async def scenario():
        app = SelectionApp("alpha beta")
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.mouse_down("#selection", offset=(0, 0))
            await pilot.hover("#selection", offset=(5, 0))
            app.panel.update("pending")
            if end_state == "remove":
                await app.panel.remove()
            elif end_state == "hide":
                app.panel.display = False
            else:
                app.panel.clear_selection()
            await pilot.pause()
            assert app.mouse_captured is None
            assert not app.panel.dragging and not app.panel.has_selection
            if end_state == "remove":
                app.panel.update("late refresh")
                assert app.panel.render().plain == "alpha beta"
            else:
                assert app.panel.render().plain == "pending"
    asyncio.run(scenario())
