"""Tests for SessionSidebar.show_worktree() using Textual's run_test pattern."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import ListView

from super_worker.models import Session, Worktree
from super_worker.services.tmux import SessionState
from super_worker.widgets.sidebar import SessionSidebar


class SidebarTestApp(App):
    def compose(self) -> ComposeResult:
        yield SessionSidebar()


def _make_worktree(session_labels: list[str]) -> Worktree:
    sessions = [
        Session(tmux_session_name=f"sw-test-{i}", label=label)
        for i, label in enumerate(session_labels)
    ]
    return Worktree(name="test-wt", path="/tmp/test", branch="main", sessions=sessions)


def _states(wt: Worktree, state: SessionState = SessionState.RUNNING) -> dict[str, SessionState]:
    return {s.tmux_session_name: state for s in wt.sessions}


@pytest.mark.asyncio
async def test_show_worktree_populates_list():
    """show_worktree with 2 sessions produces 2 items in the ListView."""
    wt = _make_worktree(["alpha", "beta"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.show_worktree(wt, states=_states(wt), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        items = sidebar.query_one("#session-list", ListView).children
        assert len(items) == 2


@pytest.mark.asyncio
async def test_show_worktree_updates_in_place():
    """Calling show_worktree twice with the same sessions but different states updates labels without adding items."""
    wt = _make_worktree(["alpha", "beta"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)

        sidebar.show_worktree(wt, states=_states(wt, SessionState.RUNNING), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        sidebar.show_worktree(wt, states=_states(wt, SessionState.WAITING_INPUT), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        items = sidebar.query_one("#session-list", ListView).children
        assert len(items) == 2


@pytest.mark.asyncio
async def test_show_worktree_removes_excess_items():
    """After showing 3 sessions then 1 session, only 1 item remains."""
    wt3 = _make_worktree(["a", "b", "c"])
    wt1 = _make_worktree(["a"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)

        sidebar.show_worktree(wt3, states=_states(wt3), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        sidebar.show_worktree(wt1, states=_states(wt1), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        items = sidebar.query_one("#session-list", ListView).children
        assert len(items) == 1


@pytest.mark.asyncio
async def test_show_worktree_adds_new_items():
    """After showing 1 session then 3 sessions, 3 items are present."""
    wt1 = _make_worktree(["a"])
    wt3 = _make_worktree(["a", "b", "c"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)

        sidebar.show_worktree(wt1, states=_states(wt1), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        sidebar.show_worktree(wt3, states=_states(wt3), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        items = sidebar.query_one("#session-list", ListView).children
        assert len(items) == 3


@pytest.mark.asyncio
async def test_show_worktree_skips_rebuild_when_unchanged():
    """Calling show_worktree twice with identical data skips the list rebuild on the second call."""
    wt = _make_worktree(["alpha", "beta"])
    states = _states(wt)
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)

        sidebar.show_worktree(wt, states=states, git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        snapshot_after_first = sidebar._prev_session_snapshot

        # Capture item references before second call
        list_view = sidebar.query_one("#session-list", ListView)
        items_before = list(list_view.children)

        sidebar.show_worktree(wt, states=states, git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        # Snapshot must not change and item references must be identical (no rebuild)
        assert sidebar._prev_session_snapshot == snapshot_after_first
        assert list(list_view.children) == items_before


@pytest.mark.asyncio
async def test_session_map_tracks_indices():
    """_session_map maps each index to the corresponding Session object."""
    wt = _make_worktree(["alpha", "beta", "gamma"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.show_worktree(wt, states=_states(wt), git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        await pilot.pause()

        assert len(sidebar._session_map) == 3
        for i, session in enumerate(wt.sessions):
            assert sidebar._session_map[i] is session
