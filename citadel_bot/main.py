"""
Citadel Quant Bot — Main Orchestrator
Forex & Indices | MetaTrader 5 | Buffer-Delayed Prediction Engine
"""

import asyncio
import hmac
import os
import platform
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Support execution as a script from inside the package folder (py main.py)
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Workaround for Windows environments where platform.system() hits broken
# WMI queries during package import (aiohttp / metaapi_cloud_sdk import path).
if os.name == "nt":
    platform.system = lambda: "Windows"

from flask import Flask, abort, jsonify, request
from metaapi_cloud_sdk import MetaApi

from citadel_bot.config import BotConfig
from citadel_bot.auth_store import AuthError, get_auth_store
from citadel_bot.utils.logger import setup_logger

log = setup_logger("main")

app = Flask('')
_supervisor = None
_supervisor_loop = None


def _require_control_api_key():
    expected = os.getenv("CITADEL_CONTROL_API_KEY", "")
    if not expected:
        return
    supplied = request.headers.get("X-Citadel-Api-Key", "")
    if not hmac.compare_digest(supplied, expected):
        abort(401)


def _request_user_id() -> int:
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or request.args.get("user_id")
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        abort(400, "user_id is required")
    if user_id <= 0:
        abort(400, "user_id is required")
    return user_id


def _mask(value: str, visible: int = 4) -> str:
    value = str(value or "")
    if len(value) <= visible:
        return "<configured>" if value else "<not configured>"
    return f"...{value[-visible:]}"


def _run_coro(coro, timeout=30):
    if _supervisor_loop is None:
        raise RuntimeError("Supervisor loop is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, _supervisor_loop)
    return future.result(timeout=timeout)


@app.route('/')
def home():
    return jsonify({"service": "citadel-bot", "multi_tenant": True})


@app.route('/api/status')
def api_status():
    _require_control_api_key()
    user_id = _request_user_id()
    return jsonify(_supervisor.status(user_id) if _supervisor else {"running": False})


@app.route('/api/account')
def api_account():
    _require_control_api_key()
    user_id = _request_user_id()
    return jsonify(_supervisor.account_info(user_id) if _supervisor else {"error": "Supervisor unavailable"})


@app.route('/api/positions')
def api_positions():
    _require_control_api_key()
    user_id = _request_user_id()
    return jsonify(_supervisor.open_positions(user_id) if _supervisor else [])


@app.post('/api/start')
def api_start():
    _require_control_api_key()
    user_id = _request_user_id()
    result = _run_coro(_supervisor.start(user_id))
    return jsonify(result)


@app.post('/api/stop')
def api_stop():
    _require_control_api_key()
    user_id = _request_user_id()
    result = _run_coro(_supervisor.stop(user_id))
    return jsonify(result)


@app.post('/api/reload-config')
def api_reload_config():
    _require_control_api_key()
    user_id = _request_user_id()
    return jsonify(_supervisor.reload_config(user_id) if _supervisor else {"success": False, "message": "Supervisor unavailable"})


@app.post('/api/credentials')
def api_credentials():
    _require_control_api_key()
    if not _supervisor:
        return jsonify({"success": False, "message": "Supervisor unavailable"}), 503
    user_id = _request_user_id()
    data = request.get_json(silent=True) or {}
    token = str(data.get("metaapi_token") or "").strip()
    account_id = str(data.get("metaapi_account_id") or "").strip()
    if not token or not account_id:
        return jsonify({"success": False, "message": "MetaApi token and account ID are required"}), 400
    try:
        get_auth_store().save_credentials(user_id, token, account_id)
        _supervisor.reload_config(user_id)
        return jsonify({"success": True, "message": "MetaApi credentials applied"})
    except AuthError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400


@app.post('/api/config')
def api_save_config():
    _require_control_api_key()
    user_id = _request_user_id()
    data = request.get_json(silent=True) or {}
    config_data = data.get("config") or {}
    config = BotConfig.from_dict(config_data, apply_environment=True)
    get_auth_store().save_config(user_id, config)
    return jsonify(_supervisor.reload_config(user_id) if _supervisor else {"success": True})


def _get_control_api_setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def run_control_api(host='127.0.0.1', port=8765):
    ssl_cert = _get_control_api_setting("CITADEL_CONTROL_API_SSL_CERT")
    ssl_key = _get_control_api_setting("CITADEL_CONTROL_API_SSL_KEY")
    ssl_context = (ssl_cert, ssl_key) if ssl_cert and ssl_key else None
    app.run(host=host, port=port, ssl_context=ssl_context)


def _control_api_scheme() -> str:
    scheme = _get_control_api_setting("CITADEL_CONTROL_API_SCHEME")
    if scheme:
        return scheme.lower()
    return "https" if _get_control_api_setting("CITADEL_CONTROL_API_SSL_CERT") and _get_control_api_setting("CITADEL_CONTROL_API_SSL_KEY") else "http"


def _control_api_host() -> str:
    return _get_control_api_setting("CITADEL_CONTROL_API_HOST", "127.0.0.1")


def _control_api_port() -> int:
    return int(_get_control_api_setting("CITADEL_CONTROL_API_PORT", "8765"))


def _control_api_path() -> str:
    path = _get_control_api_setting("CITADEL_CONTROL_API_PATH", "/api")
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _dashboard_base_path() -> str:
    path = _get_control_api_setting("CITADEL_DASHBOARD_BASE_PATH", "/dashboard")
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def keep_alive(port=None, host='0.0.0.0'):
    selected_port = int(port or os.environ.get("PORT", "8080"))
    t = threading.Thread(
        target=run_control_api,
        kwargs={"host": host, "port": selected_port},
        daemon=True,
    )
    t.start()


def start_dashboard(control_port: int) -> subprocess.Popen:
    dashboard_path = Path(__file__).resolve().parent / "dashboard.py"
    port = os.environ.get("PORT", "8501")
    env = os.environ.copy()
    control_scheme = _control_api_scheme()
    control_host = _control_api_host()
    control_port_env = _control_api_port()
    control_path = _control_api_path()
    base_path = _dashboard_base_path()

    env["CITADEL_CONTROL_API_URL"] = f"{control_scheme}://{control_host}:{control_port_env}{control_path}"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_path),
        "--server.address=0.0.0.0",
        f"--server.port={port}",
        "--server.headless=true",
    ]

    if base_path:
        cmd.append(f"--server.baseUrlPath={base_path}")

    if control_scheme == "https":
        cert_file = _get_control_api_setting("CITADEL_CONTROL_API_SSL_CERT")
        key_file = _get_control_api_setting("CITADEL_CONTROL_API_SSL_KEY")
        if cert_file and key_file:
            cmd.extend([
                f"--server.sslCertFile={cert_file}",
                f"--server.sslKeyFile={key_file}",
            ])
    log.info("Starting dashboard on port %s", port)
    return subprocess.Popen(cmd, env=env)


class CitadelBot:
    """
    Master controller. Wires together:
      DataPipeline → AdaptiveBuffer → TechnicalAnalyzer →
      PredictionEngine → SignalGenerator → RiskManager → ExecutionEngine
    """

    def __init__(self, config: BotConfig, account, connection):
        self.config = config
        self.account = account
        self.connection = connection
        self.running = False

        # Core modules
        from citadel_bot.data_pipeline import DataPipeline
        from citadel_bot.buffer_engine import AdaptiveBuffer
        from citadel_bot.technical_analysis import TechnicalAnalyzer
        from citadel_bot.prediction_engine import PredictionEngine
        from citadel_bot.signal_generator import SignalGenerator
        from citadel_bot.execution_engine import ExecutionEngine
        from citadel_bot.risk_manager import RiskManager
        from citadel_bot.signal_logger import SignalLogger

        self.pipeline   = DataPipeline(config, account, connection)
        self.buffer     = AdaptiveBuffer(config)
        self.analyzer   = TechnicalAnalyzer(config)
        self.predictor  = PredictionEngine(config)
        self.signals    = SignalGenerator(config)
        self.risk       = RiskManager(config)
        self.executor   = ExecutionEngine(config, account, connection)
        self.executor.attach_risk_manager(self.risk)

        # v2.2: signal quality logger
        self.signal_logger = SignalLogger(config)

        # Database initialization flag
        self._db_initialized = False

        # Locks for thread-safe access to shared resources
        # Use threading.Lock() since MT5 calls run in executor threads
        self.risk_lock = threading.Lock()
        self.exec_lock = threading.Lock()

    # ------------------------------------------------------------------
    async def start(self):
        log.info("=" * 60)
        log.info("  CITADEL QUANT BOT  |  v2.2  |  %s MODE", self.config.mode.upper())
        log.info("  Instruments : %s", ", ".join(self.config.instruments))
        log.info("  MetaApi account: %s", _mask(self.config.metaapi_account_id))
        log.info("  Features    : Kelly=%s | TrailingStop=%s | SignalLog=%s | Database=%s",
                  self.config.use_kelly_sizing, self.config.trailing_stop_after_tp1,
                  self.config.signal_logging, "Enabled" if self._db_initialized else "Disabled")
        log.info("=" * 60)

        self.running = True

        # Initialize database connections for components that need it
        if not self._db_initialized:
            await self._initialize_database_components()

        # MetaApi connection already established
        log.info("MetaApi connection established.")
        await self.executor.connect()

        # Start real-time data feed
        await self.pipeline.start_feeds()

        # Calibrate buffer delay after history is loaded.
        if self.config.auto_calibrate:
            log.info("Running buffer auto-calibration (this takes ~60 s)...")
            await self.buffer.calibrate(self.pipeline)
            log.info("Buffer optimal delay: %s min per instrument", self.buffer.optimal_delays)

        # Main loop
        await self._main_loop()

    async def _initialize_database_components(self):
        """Initialize database connections for all components"""
        try:
            # Initialize global database manager
            from citadel_bot.database.database_manager import init_database

            await init_database({
                "host": self.config.database_host,
                "port": self.config.database_port,
                "database": self.config.database_name,
                "user": self.config.database_user,
                "password": self.config.database_password,
            })
            log.info("[SUCCESS] Global database manager initialized")

            # Initialize component-specific database connections
            await self.buffer.initialize_db()
            await self.signal_logger.initialize_db()

            self._db_initialized = True
            log.info("[SUCCESS] All database components initialized")

        except Exception as e:
            log.warning("⚠️  Database initialization failed, continuing with CSV fallbacks: %s", e)

    # ------------------------------------------------------------------
    async def _main_loop(self):
        log.info("Bot running. Press Ctrl+C to stop.")
        tick = 0
        while self.running:
            try:
                tick += 1

                # Ensure MT5 account / position state is synced each loop.
                # This writes closed-trade ledger rows even when no new signal is generated.
                # Use run_in_executor with lock acquisition in the executor thread
                await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_account_with_lock
                )

                # Process all instruments in parallel
                tasks = [self._process_instrument(sym, tick) for sym in self.config.instruments]
                await asyncio.gather(*tasks)

                await asyncio.sleep(self.config.loop_interval_sec)

            except Exception as e:
                log.error("Main loop error: %s", e, exc_info=True)
                await asyncio.sleep(5)

    def _sync_account_with_lock(self):
        """Sync account value with thread-safe lock protection."""
        with self.exec_lock:
            self.executor.get_account_value()

    # ------------------------------------------------------------------
    async def _process_instrument(self, sym: str, tick: int):
        # v2.2: tick the cooldown counter
        self.signals.tick(sym)

        # 1. Pull real-time snapshot
        rt_data = await self.pipeline.get_realtime(sym)
        if rt_data is None or rt_data.empty:
            return

        # 2. Push to buffer; get delayed snapshot
        self.buffer.push(sym, rt_data)
        delayed_data = self.buffer.get_delayed(sym)
        if delayed_data is None or len(delayed_data) < 200:
            log.debug("[%s] Buffer warming up (%s bars)...", sym, 0 if delayed_data is None else len(delayed_data))
            return

        # 3. Technical analysis on DELAYED data → prediction
        ta_result = self.analyzer.analyze(sym, delayed_data)
        prediction = self.predictor.predict(sym, ta_result, delayed_data)

        # 4. Compare prediction to REAL-TIME situation → delta
        delta = self.signals.compute_delta(sym, prediction, rt_data)

        # 5. Generate trade signal if delta confirms
        signal_out = self.signals.generate(sym, prediction, delta, rt_data)

        # v2.2: determine rejection gate for signal logging
        rejection_gate = ""
        if signal_out is None:
            if ta_result.vol_regime == "EXTREME":
                rejection_gate = "VOL_REGIME_EXTREME"
            elif prediction.confidence < self.config.min_confidence:
                rejection_gate = "CONFIDENCE"
            elif prediction.direction == 0:
                rejection_gate = "FLAT_DIRECTION"
            elif not delta.aligned:
                rejection_gate = "DELTA_NOT_ALIGNED"
            elif delta.alignment_score < self.config.delta_threshold:
                rejection_gate = "DELTA_SCORE_LOW"
            else:
                rejection_gate = "RR_OR_COOLDOWN"

        # v2.2: log every signal attempt
        self.signal_logger.log_signal(
            sym=sym,
            ta_result=ta_result,
            prediction=prediction,
            delta=delta,
            signal=signal_out,
            rejection_gate=rejection_gate,
        )

        if signal_out is None:
            return

        log.info("[%s] SIGNAL → %s | conf=%.1f%% | entry=%s SL=%s TP1=%s TP2=%s",
                 sym, signal_out.direction, signal_out.confidence * 100,
                 signal_out.entry, signal_out.stop_loss, signal_out.tp1, signal_out.tp2)

        # 6. Risk check (thread-safe)
        with self.risk_lock:
            approved = self.risk.approve(signal_out, self.executor.get_account_value())
        if not approved:
            log.warning("[%s] Signal rejected by risk manager.", sym)
            return

        # 7. Execute (thread-safe)
        if self.config.mode == "live" or self.config.mode == "paper":
            with self.exec_lock:
                await self.executor.place_bracket_order(signal_out)

    # ------------------------------------------------------------------
    async def stop(self):
        log.info("Shutting down bot...")
        self.running = False
        await self.executor.cancel_all_orders()
        await self.executor.disconnect()

        # Database pool is process-wide and may be shared by other tenants.
        # It is closed by process shutdown, not by an individual bot stop.

        log.info("Bot stopped cleanly.")

class UserBotSupervisor:
    """Owns the long-running bot task for the dashboard/control API."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.bot = None
        self.account = None
        self.connection = None
        self.config = get_auth_store().get_config(user_id)
        self.task = None
        self.starting = False
        self.last_error = None

    async def start(self):
        if self.task and not self.task.done():
            return {"success": False, "message": "Bot is already running"}
        if self.starting:
            return {"success": False, "message": "Bot is already starting"}

        self.starting = True
        self.last_error = None
        self.task = asyncio.create_task(self._run())
        return {"success": True, "message": "Bot start requested"}

    async def _run(self):
        try:
            self.config = get_auth_store().get_config(self.user_id)
            self.account, self.connection = await create_metaapi_connection(self.config)
            self.bot = CitadelBot(self.config, self.account, self.connection)
            await self.bot.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Supervised bot stopped with error: %s", exc, exc_info=True)
        finally:
            self.starting = False
            if self.bot and self.bot.running:
                try:
                    await self.bot.stop()
                except Exception as exc:
                    log.warning("Error while stopping supervised bot: %s", exc)
            self.bot = None
            self.account = None
            self.connection = None

    async def stop(self):
        if not self.task or self.task.done():
            return {"success": False, "message": "Bot is not running"}

        if self.bot:
            await self.bot.stop()

        try:
            await asyncio.wait_for(self.task, timeout=20)
        except asyncio.TimeoutError:
            self.task.cancel()
            return {"success": True, "message": "Bot stop requested; task cancellation forced"}

        return {"success": True, "message": "Bot stopped"}

    def reload_config(self):
        self.config = get_auth_store().get_config(self.user_id)
        if self.task and not self.task.done():
            return {
                "success": True,
                "message": "Configuration reloaded for dashboard/status. Restart the bot for running trading logic to use it.",
                "instruments": self.config.instruments,
            }
        return {
            "success": True,
            "message": "Configuration reloaded",
            "instruments": self.config.instruments,
        }

    def status(self):
        running = bool(self.task and not self.task.done())
        return {
            "running": running,
            "starting": self.starting,
            "user_id": self.user_id,
            "instruments": self.config.instruments,
            "mode": self.config.mode,
            "metaapi_connected": self.connection is not None,
            "account_balance": self.account_info().get("balance", 0.0),
            "last_error": self.last_error,
            "last_update": time.time(),
        }

    def account_info(self):
        if self.connection is None:
            return {"error": "MetaApi connection not attached"}
        try:
            info = getattr(self.connection.terminal_state, "account_information", None)
            if not info:
                return {"error": "Account information not synchronized"}
            return {
                "login": info.get("login"),
                "server": info.get("server"),
                "balance": round(float(info.get("balance") or 0), 2),
                "equity": round(float(info.get("equity") or info.get("balance") or 0), 2),
                "profit": round(float(info.get("profit") or 0), 2),
                "margin": round(float(info.get("margin") or 0), 2),
                "margin_free": round(float(info.get("freeMargin") or info.get("marginFree") or 0), 2),
                "margin_level": round(float(info.get("marginLevel") or 0), 2),
                "currency": info.get("currency") or "",
                "company": info.get("broker") or info.get("company") or "",
            }
        except Exception as exc:
            return {"error": str(exc)}

    def open_positions(self):
        if self.connection is None:
            return []
        try:
            positions = getattr(self.connection.terminal_state, "positions", []) or []
            return [{
                "ticket": pos.get("id") or pos.get("positionId"),
                "symbol": pos.get("symbol"),
                "type": "BUY" if pos.get("type") == "POSITION_TYPE_BUY" else "SELL",
                "volume": pos.get("volume"),
                "open_price": round(float(pos.get("openPrice") or 0), 5),
                "current_price": round(float(pos.get("currentPrice") or 0), 5),
                "profit": round(float(pos.get("profit") or 0), 2),
                "open_time": pos.get("time"),
            } for pos in positions]
        except Exception:
            return []


class BotSupervisorManager:
    """Multiplexes independent bot supervisors per tenant/user."""

    def __init__(self):
        self._supervisors = {}

    def _get(self, user_id: int) -> UserBotSupervisor:
        if user_id not in self._supervisors:
            self._supervisors[user_id] = UserBotSupervisor(user_id)
        return self._supervisors[user_id]

    async def start(self, user_id: int):
        return await self._get(user_id).start()

    async def stop(self, user_id: int):
        return await self._get(user_id).stop()

    async def stop_all(self):
        results = []
        for supervisor in list(self._supervisors.values()):
            results.append(await supervisor.stop())
        return results

    def reload_config(self, user_id: int):
        return self._get(user_id).reload_config()

    def status(self, user_id: int):
        return self._get(user_id).status()

    def account_info(self, user_id: int):
        return self._get(user_id).account_info()

    def open_positions(self, user_id: int):
        return self._get(user_id).open_positions()


async def main():
    global _supervisor, _supervisor_loop
    _supervisor_loop = asyncio.get_running_loop()
    _supervisor = BotSupervisorManager()
    os.environ.setdefault("CITADEL_CONTROL_API_KEY", secrets.token_urlsafe(32))

    run_dashboard = os.getenv("CITADEL_RUN_DASHBOARD", "true").lower() not in {"0", "false", "no"}
    control_port = int(os.getenv("CITADEL_CONTROL_PORT", "8765"))
    dashboard_process = None

    if run_dashboard:
        keep_alive(port=control_port, host="127.0.0.1")
        dashboard_process = start_dashboard(control_port)
    else:
        keep_alive()

    loop = asyncio.get_event_loop()

    def _shutdown(sig, frame):
        log.info("Signal %s received — stopping.", sig)
        loop.create_task(_supervisor.stop_all())
        if dashboard_process:
            dashboard_process.terminate()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if os.getenv("CITADEL_AUTOSTART_BOT", "false").lower() not in {"0", "false", "no"}:
        autostart_user = int(os.getenv("CITADEL_AUTOSTART_USER_ID", "0") or "0")
        if autostart_user > 0:
            await _supervisor.start(autostart_user)

    try:
        while True:
            if dashboard_process and dashboard_process.poll() is not None:
                raise RuntimeError(f"Dashboard exited with code {dashboard_process.returncode}")
            await asyncio.sleep(2)
    finally:
        await _supervisor.stop_all()
        if dashboard_process and dashboard_process.poll() is None:
            dashboard_process.terminate()


async def create_metaapi_connection(config: BotConfig):
    """Create and synchronize a MetaApi streaming connection."""
    config.validate_metaapi()
    # Increase MetaApi SDK request timeout to reduce subscription timeouts
    os.environ['METAAPI_REQUEST_TIMEOUT'] = '120'  # 2 minutes
    try:
        api = MetaApi(token=config.metaapi_token, request_timeout=120)
    except TypeError:
        # Fallback if SDK does not accept request_timeout parameter
        api = MetaApi(token=config.metaapi_token)
    account = await api.metatrader_account_api.get_account(config.metaapi_account_id)
    connection = account.get_streaming_connection()
    await connection.connect()
    print("Waiting for SDK to synchronize...")
    # Retry synchronization with exponential backoff to handle intermittent timeouts
    max_retries = 5
    base_delay = 2  # seconds
    for attempt in range(max_retries):
        try:
            await connection.wait_synchronized()
            break
        except Exception as sync_error:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning(
                "MetaApi synchronization attempt %s/%s failed: %s. Retrying in %s seconds...",
                attempt + 1, max_retries, sync_error, delay
            )
            await asyncio.sleep(delay)
    return account, connection


if __name__ == "__main__":
    asyncio.run(main())
