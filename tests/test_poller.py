"""轮询器测试：游标去重、事件类型修正、单 PR 失败隔离、仓库范围。"""

from core.poller import Poller
from core.state_store import CursorStore
from github.client import GitHubError
from github.models import PullRequestEventType
from tests.conftest import make_event


class FakePollerGitHub:
    """提供 list_open_prs / fix_event_type 的轮询侧假客户端。"""

    def __init__(self, prs_by_repo, fail_repos=()):
        self._prs = prs_by_repo
        self._fail = set(fail_repos)

    async def list_open_prs(self, repo):
        if repo in self._fail:
            raise GitHubError("api down")
        return self._prs.get(repo, [])

    def fix_event_type(self, event, seen_sha):
        if seen_sha is None:
            return event
        return make_event(
            repo=event.repo,
            number=event.number,
            author=event.author_login,
            head_sha=event.head_sha,
        ).__class__(
            repo=event.repo,
            number=event.number,
            title=event.title,
            body=event.body,
            author_login=event.author_login,
            head_sha=event.head_sha,
            base_branch=event.base_branch,
            event_type=PullRequestEventType.SYNCHRONIZE,
            html_url=event.html_url,
        )


async def _make_poller(tmp_path, config, gh, handled):
    async def handler(event):
        handled.append(event)

    cursor = CursorStore(tmp_path / "cursor.json")
    poller = Poller(gh, config.github, cursor, handler)
    return poller, cursor


class TestPoller:
    async def test_new_pr_dispatched_and_deduped(self, config, tmp_path):
        event = make_event(head_sha="sha1")
        gh = FakePollerGitHub({"octo/demo": [event]})
        handled: list = []
        poller, cursor = await _make_poller(tmp_path, config, gh, handled)

        assert await poller.scan_once() == 1
        assert handled == [event]

        # 同 head_sha 再扫：去重不触发
        await cursor.mark(event.cursor_key, event.head_sha)
        assert await poller.scan_once() == 0

    async def test_new_head_triggers_synchronize(self, config, tmp_path):
        gh = FakePollerGitHub({"octo/demo": [make_event(head_sha="sha2")]})
        handled: list = []
        poller, cursor = await _make_poller(tmp_path, config, gh, handled)
        await cursor.mark("octo/demo#42", "sha1")

        assert await poller.scan_once() == 1
        assert handled[0].event_type == PullRequestEventType.SYNCHRONIZE

    async def test_handler_failure_isolated(self, config, tmp_path):
        prs = [make_event(number=1, head_sha="a"), make_event(number=2, head_sha="b")]
        gh = FakePollerGitHub({"octo/demo": prs})
        handled: list = []

        async def flaky(event):
            if event.number == 1:
                raise RuntimeError("boom")
            handled.append(event)

        cursor = CursorStore(tmp_path / "cursor.json")
        poller = Poller(gh, config.github, cursor, flaky)

        assert await poller.scan_once() == 1  # 仅 #2 成功
        assert [e.number for e in handled] == [2]

    async def test_repo_api_failure_skipped(self, config, tmp_path):
        gh = FakePollerGitHub({}, fail_repos={"octo/demo"})
        handled: list = []
        poller, _ = await _make_poller(tmp_path, config, gh, handled)

        assert await poller.scan_once() == 0
        assert not handled

    async def test_scan_once_rejects_unconfigured_repo(self, config, tmp_path):
        gh = FakePollerGitHub({"other/repo": [make_event(repo="other/repo")]})
        handled: list = []
        poller, _ = await _make_poller(tmp_path, config, gh, handled)

        assert await poller.scan_once("other/repo") == 0
        assert not handled

    async def test_start_and_stop(self, config, tmp_path):
        gh = FakePollerGitHub({"octo/demo": []})
        handled: list = []
        poller, _ = await _make_poller(tmp_path, config, gh, handled)

        poller.start()
        assert poller._task is not None and not poller._task.done()
        await poller.stop()
        assert poller._task.done()
