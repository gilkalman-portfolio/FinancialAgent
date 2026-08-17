"""
Test: telegram_notifier.py's 2026-08-17 hardening — a message truncated to
4000 chars no longer risks losing the whole send on an unbalanced Markdown
entity, and a 429 rate-limit gets one retry instead of an immediate failure.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_telegram_notifier_resilience.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.telegram_notifier import TelegramNotifier


def _notifier():
    n = TelegramNotifier()
    n.enabled = True
    n.bot_token = "fake-token"
    n.chat_id = "fake-chat"
    return n


def _http_error(status_code, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    err = requests.exceptions.HTTPError(response=resp)
    return err


class TestTruncation:
    def test_long_message_is_truncated(self):
        n = _notifier()
        long_text = "A" * 5000
        with patch("src.telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            n.send_message(long_text)
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert len(sent_text) <= n.MAX_MSG_LEN
        assert sent_text.endswith("[truncated]")

    def test_truncation_prefers_a_newline_boundary(self):
        n = _notifier()
        # A paragraph break sits comfortably before the limit — must cut there,
        # not mid-character at MAX_MSG_LEN-20.
        long_text = ("word " * 700) + "\n\n" + ("Z" * 500)
        with patch("src.telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            n.send_message(long_text)
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert not sent_text.rstrip("\n…[truncated]").endswith("Z" * 10), \
            "expected the cut to land at the newline, not mid Z-run"


class TestMarkdownFallback:
    def test_400_with_markdown_retries_as_plain_text(self):
        n = _notifier()
        calls = []

        def _post_side_effect(url, json, timeout):
            calls.append(json.get("parse_mode"))
            resp = MagicMock()
            if json.get("parse_mode"):
                resp.raise_for_status.side_effect = _http_error(400)
            else:
                resp.raise_for_status.return_value = None
            return resp

        with patch("src.telegram_notifier.requests.post", side_effect=_post_side_effect):
            result = n.send_message("*unbalanced bold", parse_mode="Markdown")

        assert result is True
        assert calls == ["Markdown", None], "expected exactly one retry, second attempt plain text"

    def test_400_without_markdown_does_not_retry_forever(self):
        n = _notifier()
        with patch("src.telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = _http_error(400)
            result = n.send_message("plain text that still 400s", parse_mode=None)
        assert result is False
        assert mock_post.call_count == 1


class TestRateLimitRetry:
    def test_429_retries_once_after_backoff(self):
        n = _notifier()
        responses = [_http_error(429, headers={"Retry-After": "1"}), None]

        def _post_side_effect(url, json, timeout):
            resp = MagicMock()
            err = responses.pop(0)
            resp.raise_for_status.side_effect = err
            return resp

        with patch("src.telegram_notifier.requests.post", side_effect=_post_side_effect), \
             patch("time.sleep") as mock_sleep:
            result = n.send_message("hello")

        assert result is True
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 1

    def test_429_twice_gives_up_after_one_retry(self):
        n = _notifier()
        with patch("src.telegram_notifier.requests.post") as mock_post, \
             patch("time.sleep"):
            mock_post.return_value.raise_for_status.side_effect = _http_error(429)
            result = n.send_message("hello")
        assert result is False
        assert mock_post.call_count == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
