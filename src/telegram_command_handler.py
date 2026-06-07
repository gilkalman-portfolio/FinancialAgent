"""
Two-Way Telegram Command Handler — listens for commands via getUpdates polling.

Runs as a background thread inside ibkr_worker (Python 3.13, .venv313).

Supported commands:
    /status     — regime, queue size, positions, daily P&L, last signal
    /positions  — open positions table
    /pause      — pause order submission
    /resume     — resume order submission
    /cancel <T> — cancel open IBKR orders for ticker T

Security: only responds to messages from the configured TELEGRAM_CHAT_ID.
Offset persistence: last_update_id stored in telegram_command_state DB table.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from src.database import get_connection, retry_on_busy

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_HELP_TEXT = (
    "Unknown command.\n"
    "Available: /status /positions /pause /resume /cancel <TICKER>"
)


class TelegramCommandHandler:
    def __init__(
        self,
        telegram_notifier,
        order_manager_module,
        position_tracker,
        ibkr_client,
        poll_interval_seconds: int = 30,
    ):
        self._notifier = telegram_notifier
        self._order_mgr = order_manager_module
        self._tracker = position_tracker
        self._ibkr = ibkr_client
        self._poll_interval = poll_interval_seconds

        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._api_base = _TELEGRAM_API.format(token=self._token)

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_update_id = self._load_offset()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._token or not self._chat_id:
            logger.warning("[cmd_handler] Telegram credentials missing — not starting (token=%s, chat_id=%r)",
                           "set" if self._token else "EMPTY", self._chat_id)
            return
        logger.debug("[cmd_handler] config: chat_id=%r, saved_offset=%s", self._chat_id, self._last_update_id)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-cmd", daemon=True
        )
        self._thread.start()
        logger.info("[cmd_handler] started polling (interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("[cmd_handler] stopped")

    # ── Polling loop ─────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.error("[cmd_handler] poll error: %s", e)
            self._stop_event.wait(self._poll_interval)

    def _poll_once(self) -> None:
        params: dict = {"timeout": 10, "allowed_updates": ["message"]}
        if self._last_update_id is not None:
            params["offset"] = self._last_update_id + 1
        logger.debug("[cmd_handler] getUpdates offset=%s", self._last_update_id)

        try:
            resp = requests.get(
                f"{self._api_base}/getUpdates", params=params, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("[cmd_handler] getUpdates failed: %s", e)
            return

        if not data.get("ok"):
            logger.warning("[cmd_handler] getUpdates ok=false: %s", data)
            return

        results = data.get("result", [])
        logger.debug("[cmd_handler] getUpdates returned %d update(s)", len(results))

        for update in results:
            uid = update["update_id"]
            self._last_update_id = uid
            self._handle_update(update)

        if results:
            self._persist_offset(self._last_update_id)

    # ── Dispatch ─────────────────────────────────────────────────────────

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message")
        if not msg:
            logger.debug("[cmd_handler] update %s has no message key", update.get("update_id"))
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.debug("[cmd_handler] chat_id mismatch: got %r, expected %r", chat_id, self._chat_id)
            return

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            logger.debug("[cmd_handler] ignoring non-command text: %r", text[:50])
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # strip bot username suffix
        args = parts[1:]
        logger.debug("[cmd_handler] dispatching cmd=%s args=%s", cmd, args)

        handler = {
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/cancel": self._cmd_cancel,
        }.get(cmd)

        if handler is None:
            self._reply(_HELP_TEXT)
            return

        try:
            handler(args)
        except Exception as e:
            logger.error("[cmd_handler] %s failed: %s", cmd, e)
            self._reply(f"Error processing {cmd}: {e}")

    # ── Commands ─────────────────────────────────────────────────────────

    def _cmd_status(self, _args: list[str]) -> None:
        from src.market_regime import get_regime, regime_summary

        regime = get_regime()
        regime_line = regime_summary(regime)

        queue_size = 0
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM monitoring_queue_snapshot"
                ).fetchone()
                queue_size = row["cnt"] if row else 0
        except Exception:
            pass

        positions_count = 0
        try:
            with get_connection() as conn:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM ibkr_positions").fetchone()
                positions_count = row["cnt"] if row else 0
        except Exception:
            pass

        daily_pnl = 0.0
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT day_pnl FROM daily_pnl WHERE date = ?", (today,)
                ).fetchone()
                if row:
                    daily_pnl = float(row["day_pnl"])
        except Exception:
            pass

        last_signal = "N/A"
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT ticker, signal_type, signal_ts FROM forward_signals "
                    "ORDER BY signal_ts DESC LIMIT 1"
                ).fetchone()
                if row:
                    last_signal = f"{row['signal_type']} {row['ticker']} @ {row['signal_ts'][:16]}"
        except Exception:
            pass

        paused = "YES" if self._order_mgr.is_paused() else "NO"

        msg = (
            f"{regime_line}\n"
            f"Monitoring queue: {queue_size} tickers\n"
            f"Open positions: {positions_count}\n"
            f"Daily P&L: ${daily_pnl:+,.2f}\n"
            f"Trading paused: {paused}\n"
            f"Last signal: {last_signal}"
        )
        self._reply(msg)

    def _cmd_positions(self, _args: list[str]) -> None:
        try:
            self._tracker.sync_positions()
        except Exception as e:
            logger.warning("[cmd_handler] sync before /positions failed: %s", e)

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, shares, avg_cost, unrealized_pnl FROM ibkr_positions"
            ).fetchall()

        if not rows:
            self._reply("No open positions.")
            return

        lines = ["Ticker | Shares | Avg Cost | Unrealized P&L"]
        lines.append("─" * 42)
        for r in rows:
            lines.append(
                f"{r['ticker']:6s} | {r['shares']:>6.0f} | "
                f"${r['avg_cost']:>8.2f} | ${r['unrealized_pnl']:>+10.2f}"
            )
        self._reply("\n".join(lines))

    def _cmd_pause(self, _args: list[str]) -> None:
        self._order_mgr.set_paused(True)
        self._reply("⏸ Trading PAUSED. Orders will be blocked until /resume.")

    def _cmd_resume(self, _args: list[str]) -> None:
        self._order_mgr.set_paused(False)
        self._reply("▶️ Trading RESUMED. Orders will be processed normally.")

    def _cmd_cancel(self, args: list[str]) -> None:
        if not args:
            self._reply("Usage: /cancel <TICKER>")
            return
        ticker = args[0].upper()

        try:
            open_orders = self._ibkr.get_open_orders()
        except Exception as e:
            self._reply(f"Failed to fetch open orders: {e}")
            return

        matching = [o for o in open_orders if o["ticker"] == ticker]
        if not matching:
            self._reply(f"No open orders found for {ticker}.")
            return

        cancelled = 0
        for order in matching:
            try:
                self._ibkr.cancel_order(order["order_id"])
                cancelled += 1
            except Exception as e:
                logger.warning("[cmd_handler] cancel order %d failed: %s", order["order_id"], e)

        self._reply(f"Cancelled {cancelled}/{len(matching)} orders for {ticker}.")

    # ── Reply helper ─────────────────────────────────────────────────────

    def _reply(self, text: str) -> None:
        try:
            resp = requests.post(
                f"{self._api_base}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning("[cmd_handler] sendMessage rejected: %s", data.get("description", data))
            else:
                logger.debug("[cmd_handler] reply sent (%d chars)", len(text))
        except Exception as e:
            logger.warning("[cmd_handler] reply failed: %s", e)

    # ── Offset persistence ───────────────────────────────────────────────

    @staticmethod
    def _load_offset() -> int | None:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT value FROM telegram_command_state WHERE key = 'last_update_id'"
                ).fetchone()
                return int(row["value"]) if row else None
        except Exception:
            return None

    @staticmethod
    @retry_on_busy()
    def _persist_offset(offset: int) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO telegram_command_state (key, value) VALUES ('last_update_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (offset,),
            )
