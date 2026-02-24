import pytest

from super_worker.models import AppState, Session, Worktree


class TestSession:
    def test_defaults_generate_unique_ids(self):
        s1 = Session(tmux_session_name="sw-a-0", label="first")
        s2 = Session(tmux_session_name="sw-a-1", label="second")
        assert s1.id != s2.id
        assert len(s1.id) == 8


class TestAppState:
    @pytest.mark.parametrize("name,expected_found", [
        ("feat", True),
        ("nonexistent", False),
        ("other", False),
    ])
    def test_get_worktree(self, name, expected_found):
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
        result = state.get_worktree(name)
        assert (result is wt) == expected_found
