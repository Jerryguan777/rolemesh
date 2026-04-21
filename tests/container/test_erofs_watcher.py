"""Tests for rolemesh.container.erofs_watcher."""

from __future__ import annotations

from unittest.mock import patch

from rolemesh.container.erofs_watcher import ErofsWatcher


def _make_watcher() -> ErofsWatcher:
    return ErofsWatcher(coworker_name="test-cw", container_name="rolemesh-test-123")


class TestEroFsMatching:
    def test_canonical_python_eros_line_matches(self) -> None:
        w = _make_watcher()
        line = "OSError: [Errno 30] Read-only file system: '/home/agent/.jupyter'"
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe(line)
        mock_logger.warning.assert_called_once()
        kwargs = mock_logger.warning.call_args.kwargs
        assert kwargs["path"] == "/home/agent/.jupyter"
        assert kwargs["coworker"] == "test-cw"
        assert kwargs["container"] == "rolemesh-test-123"

    def test_permission_error_30_also_matches(self) -> None:
        """PermissionError: [Errno 30] variant seen from some Python builds."""
        w = _make_watcher()
        line = "PermissionError: [Errno 30] Read-only file system: '/foo/bar'"
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe(line)
        mock_logger.warning.assert_called_once()

    def test_text_only_read_only_message_matches(self) -> None:
        """Some libraries print just 'Read-only file system' with no errno."""
        w = _make_watcher()
        line = "IOError: Read-only file system: '/opt/app/cache'"
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe(line)
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.kwargs["path"] == "/opt/app/cache"

    def test_case_insensitive(self) -> None:
        w = _make_watcher()
        line = "Oserror: [errno 30] read-only file system: '/x'"
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe(line)
        mock_logger.warning.assert_called_once()


class TestNonMatches:
    def test_regular_log_line_does_not_trigger(self) -> None:
        w = _make_watcher()
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe("[agent-runner] Starting query (session: ...)")
            w.observe("INFO: 2026-04-20 running fine")
        mock_logger.warning.assert_not_called()

    def test_permission_denied_eacces_does_not_trigger(self) -> None:
        """Layer 2 is scoped to EROFS. EACCES has a different remediation
        path (mount-security allowlist, UID mismatch) and must NOT fire
        this warning — otherwise the tmpfs-miss signal gets diluted."""
        w = _make_watcher()
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe("PermissionError: [Errno 13] Permission denied: '/etc/shadow'")
            w.observe("OSError: [Errno 13] Permission denied: '/workspace/sessions/x.jsonl'")
        mock_logger.warning.assert_not_called()

    def test_errno_30_without_readonly_keyword_does_not_trigger(self) -> None:
        """Avoid false positives on errno numbers that happen to land on 30
        in other contexts — require the 'Read-only' keyword too."""
        w = _make_watcher()
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe("CustomError: [Errno 30] something completely different: '/x'")
        mock_logger.warning.assert_not_called()

    def test_empty_line(self) -> None:
        w = _make_watcher()
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe("")
        mock_logger.warning.assert_not_called()


class TestDeduplication:
    def test_same_path_reported_once_even_if_line_repeats(self) -> None:
        """Retry loops can emit the same error thousands of times. Only the
        first hit per path per watcher instance should warn — subsequent
        lines are suppressed to keep operator alerts actionable."""
        w = _make_watcher()
        line = "OSError: [Errno 30] Read-only file system: '/home/agent/.foo'"
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            for _ in range(100):
                w.observe(line)
        mock_logger.warning.assert_called_once()

    def test_different_paths_each_reported_once(self) -> None:
        w = _make_watcher()
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe("OSError: [Errno 30] Read-only file system: '/home/agent/.a'")
            w.observe("OSError: [Errno 30] Read-only file system: '/home/agent/.b'")
            w.observe("OSError: [Errno 30] Read-only file system: '/home/agent/.a'")  # dup
        assert mock_logger.warning.call_count == 2
        paths = {c.kwargs["path"] for c in mock_logger.warning.call_args_list}
        assert paths == {"/home/agent/.a", "/home/agent/.b"}

    def test_unknown_path_deduplicates_too(self) -> None:
        """Lines matching EROFS but without a quoted path (rare, but possible
        from non-stdlib code) still get deduplicated — otherwise they'd
        flood the log."""
        w = _make_watcher()
        line = "something Read-only file system something"  # no quotes
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe(line)
            w.observe(line)
            w.observe(line)
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.kwargs["path"] == "<unknown>"


class TestRawLineTrimming:
    def test_long_line_truncated_in_log(self) -> None:
        """Very long stderr lines shouldn't balloon the log record."""
        w = _make_watcher()
        long_path = "/home/agent/" + "a" * 1000
        line = f"OSError: [Errno 30] Read-only file system: '{long_path}'"
        with patch("rolemesh.container.erofs_watcher.logger") as mock_logger:
            w.observe(line)
        raw = mock_logger.warning.call_args.kwargs["raw"]
        assert len(raw) <= 500
