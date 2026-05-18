"""
Citadel Bot Dashboard - Streamlit Web Interface

A comprehensive web dashboard for monitoring and controlling the Citadel Quant Bot.
Provides real-time status, instrument selection, configuration management, and trading oversight.
"""

import asyncio
import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import threading
import time
from pathlib import Path
import sys
import yaml
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from citadel_bot.config.config import BotConfig
from citadel_bot.auth_store import AuthError, get_auth_store
from citadel_bot.database.database_manager import db_manager
from citadel_bot.utils.logger import get_logger
from citadel_bot.utils.instrument_catalog import CATALOG, list_by_category, all_categories
from citadel_bot.dashboard_service import DashboardService

CONTROL_API_KEY_ENV = "CITADEL_CONTROL_API_KEY"

# Page configuration
st.set_page_config(
    page_title="Citadel Bot Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

def _auth_headers() -> dict:
    api_key = os.getenv(CONTROL_API_KEY_ENV, "")
    return {"X-Citadel-Api-Key": api_key} if api_key else {}


def check_password():
    """Authenticate a tenant user with signup and login flows."""
    store = get_auth_store()
    token = st.session_state.get("session_token", "")
    user = store.get_user_by_session(token)
    if user:
        st.session_state["user"] = user
        return True

    st.title("Citadel Bot")
    st.caption("Sign in or create an account to manage your isolated trading bot.")

    login_tab, signup_tab = st.tabs(["Login", "Sign Up"])

    with login_tab:
        with st.form("tenant_login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Login", type="primary")
        if submitted:
            try:
                user, session_token = store.authenticate(email, password)
                st.session_state["session_token"] = session_token
                st.session_state["user"] = user
                st.rerun()
            except AuthError as exc:
                st.error(str(exc))

    with signup_tab:
        with st.form("tenant_signup"):
            name = st.text_input("Display Name", key="signup_name")
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_password")
            confirm = st.text_input("Confirm Password", type="password", key="signup_confirm")
            submitted = st.form_submit_button("Create Account", type="primary")
        if submitted:
            if password != confirm:
                st.error("Passwords do not match.")
            else:
                try:
                    store.create_user(email, password, name)
                    user, session_token = store.authenticate(email, password)
                    st.session_state["session_token"] = session_token
                    st.session_state["user"] = user
                    st.rerun()
                except AuthError as exc:
                    st.error(str(exc))

    return False


# Bot control class
class BotController:
    def __init__(self):
        self.bot_process = None
        self.is_running = False
        self.user = st.session_state.get("user")
        self.user_id = self.user.user_id if self.user else None
        self.auth_store = get_auth_store()
        self.config = self.auth_store.get_config(self.user_id) if self.user_id else BotConfig.from_file("config.yaml")
        self.logger = get_logger("dashboard")
        self.config_path = Path("config.yaml")
        self.dashboard_service = DashboardService(self.user_id)
        self.bot_instance = None
        self.bot_loop = None
        self.bot_thread = None
        self.bot_task = None
        self.control_api_url = os.getenv("CITADEL_CONTROL_API_URL", "").rstrip("/")
        self.verify_ssl = os.getenv("CITADEL_CONTROL_API_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}

    def _control_request(self, method: str, path: str, default, json=None, params=None):
        if not self.control_api_url:
            return default
        payload = dict(json or {})
        query = dict(params or {})
        if self.user_id:
            if method.upper() == "GET":
                query.setdefault("user_id", self.user_id)
            else:
                payload.setdefault("user_id", self.user_id)
        try:
            response = requests.request(
                method,
                f"{self.control_api_url}{path}",
                headers=_auth_headers(),
                json=payload or None,
                params=query or None,
                timeout=5,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self.logger.warning("Control API unavailable: %s", exc)
            return default

    def set_metaapi_credentials(self, token: str, account_id: str):
        """Persist encrypted MetaApi credentials for the current tenant."""
        if not self.user_id:
            return False, "Login required"
        try:
            self.auth_store.save_credentials(self.user_id, token, account_id)
        except AuthError as exc:
            return False, str(exc)
        self.config.metaapi_token = token.strip()
        self.config.metaapi_account_id = account_id.strip()
        if self.control_api_url:
            result = self._control_request(
                "POST",
                "/api/credentials",
                {"success": False, "message": "Control API unavailable"},
                json={
                    "metaapi_token": self.config.metaapi_token,
                    "metaapi_account_id": self.config.metaapi_account_id,
                },
            )
            return bool(result.get("success")), result.get("message", "Credentials applied")
        return True, "Credentials saved"

    def start_bot(self):
        """Start the bot in a separate thread"""
        if self.control_api_url:
            result = self._control_request("POST", "/api/start", {"success": False, "message": "Control API unavailable"})
            return bool(result.get("success")), result.get("message", "Bot start requested")

        if not self.is_running:
            self.is_running = True
            self.bot_thread = threading.Thread(target=self._run_bot_async, daemon=True)
            self.bot_thread.start()
            self.logger.info("Bot started from dashboard")
            return True, "Bot started successfully"
        else:
            return False, "Bot is already running"

    def stop_bot(self):
        """Stop the bot"""
        if self.control_api_url:
            result = self._control_request("POST", "/api/stop", {"success": False, "message": "Control API unavailable"})
            return bool(result.get("success")), result.get("message", "Bot stop requested")

        if self.is_running:
            self.is_running = False
            # Signal the bot instance to stop
            if self.bot_instance:
                # The CitadelBot will check self.running flag and stop gracefully
                pass
            self.logger.info("Bot stop requested from dashboard")
            return True, "Bot stop requested"
        else:
            return False, "Bot is not running"

    async def _run_bot_async_coro(self):
        """Coroutine to run the bot"""
        from citadel_bot.main import CitadelBot
        from metaapi_cloud_sdk import MetaApi

        try:
            config = self.auth_store.get_config(self.user_id)

            # Initialize MetaApi
            api = MetaApi(token=config.metaapi_token)
            account = await api.metatrader_account_api.get_account(config.metaapi_account_id)
            connection = account.get_streaming_connection()
            await connection.connect()
            print("Waiting for SDK to synchronize...")
            await connection.wait_synchronized()

            self.dashboard_service.attach_connection(connection)
            bot = CitadelBot(config, account, connection)
            self.bot_instance = bot

            # Run the bot
            await bot.start()
        except Exception as e:
            self.logger.error(f"Error running bot: {e}")
            self.is_running = False

    def _run_bot_async(self):
        """Run the bot in asyncio event loop"""
        import asyncio

        try:
            asyncio.run(self._run_bot_async_coro())
        except Exception as e:
            self.logger.error(f"Error running bot: {e}")
            self.is_running = False
        finally:
            self.is_running = False

    def get_status(self):
        """Get current bot status"""
        api_status = self._control_request("GET", "/api/status", None)
        if api_status:
            self.config = self.auth_store.get_config(self.user_id)
            return {
                "running": bool(api_status.get("running")),
                "starting": bool(api_status.get("starting")),
                "instruments": api_status.get("instruments") or self.config.instruments,
                "mode": api_status.get("mode") or self.config.mode,
                "last_update": datetime.fromtimestamp(api_status.get("last_update", time.time())),
                "metaapi_connected": bool(api_status.get("metaapi_connected")),
                "account_balance": float(api_status.get("account_balance") or 0.0),
                "last_error": api_status.get("last_error"),
            }

        # Try to get more detailed status from the bot instance if available
        detailed_status = {
            "running": self.is_running,
            "instruments": self.config.instruments,
            "mode": self.config.mode,
            "last_update": datetime.now(),
            "metaapi_connected": False,
            "account_balance": 0.0
        }
        
        # Try to get MetaApi and account info from dashboard service
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            account_info = loop.run_until_complete(self.dashboard_service.get_account_info())
            loop.close()
            
            if "error" not in account_info:
                detailed_status["metaapi_connected"] = True
                detailed_status["account_balance"] = account_info.get("balance", 0.0)
        except Exception:
            pass  # Keep default values if we can't get the info
            
        return detailed_status

    def get_account_info(self):
        api_account = self._control_request("GET", "/api/account", None)
        if api_account:
            return api_account
        return self._run_async(self.dashboard_service.get_account_info(), {"error": "Unavailable"})

    def get_open_positions(self):
        api_positions = self._control_request("GET", "/api/positions", None)
        if api_positions is not None:
            return api_positions
        return self._run_async(self.dashboard_service.get_open_positions(), [])

    def _run_async(self, coro, default):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(coro)
            loop.close()
            return result
        except Exception:
            return default

    def save_config(self, config: BotConfig):
        """Save configuration to file"""
        try:
            if self.user_id:
                self.auth_store.save_config(self.user_id, config)
            else:
                config.save(str(self.config_path))
            self.config = config
            if self.control_api_url:
                self._control_request("POST", "/api/config", None, json={"config": config.to_dict(include_secrets=False)})
            self.logger.info("Configuration saved successfully")
            return True, "Configuration saved successfully"
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            return False, f"Failed to save: {str(e)}"

    def reload_config(self):
        """Reload configuration from file"""
        try:
            self.config = self.auth_store.get_config(self.user_id)
            if self.control_api_url:
                result = self._control_request("POST", "/api/reload-config", None)
                if result and result.get("instruments"):
                    self.config.instruments = result["instruments"]
            self.logger.info("Configuration reloaded")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reload configuration: {e}")
            return False

bot_controller = None

def main():
    if not check_password():
        st.stop()

    current_user = st.session_state.get("user")
    if (
        "bot_controller" not in st.session_state
        or st.session_state.bot_controller.user_id != current_user.user_id
    ):
        st.session_state.bot_controller = BotController()

    global bot_controller
    bot_controller = st.session_state.bot_controller

    # Sidebar
    st.sidebar.title("📊 Citadel Bot Dashboard")
    st.sidebar.caption(f"Signed in as {current_user.email}")
    st.sidebar.markdown("---")

    # Bot Control
    st.sidebar.subheader("🤖 Bot Control")
    col1, col2 = st.sidebar.columns(2)
    with col1:
        if st.button("▶️ Start", width='stretch'):
            bot_controller.start_bot()
            st.toast("✅ Bot started!", icon="🚀")

    with col2:
        if st.button("⏹️ Stop", width='stretch'):
            bot_controller.stop_bot()
            st.toast("⏹️ Bot stopped!", icon="⛔")

    # Status
    status = bot_controller.get_status()
    st.sidebar.subheader("📈 Status")
    st.sidebar.metric("Bot Status", "🟢 Running" if status["running"] else "🔴 Stopped")
    if status.get("metaapi_connected", False):
        st.sidebar.metric("MT5", "🟢 Connected")
        st.sidebar.metric("Balance", f"${status.get('account_balance', 0):,.2f}")
    else:
        st.sidebar.metric("MT5", "🔴 Disconnected")
    st.sidebar.metric("Configured Mode", bot_controller.config.mode.upper())
    if status.get("running") and status.get("mode") != bot_controller.config.mode:
        st.sidebar.caption(f"Running bot is still {status['mode'].upper()}; restart to apply.")
    st.sidebar.metric("Instruments", len(bot_controller.config.instruments))
    if st.sidebar.button("Logout"):
        bot_controller.stop_bot()
        get_auth_store().revoke_session(st.session_state.get("session_token", ""))
        for key in ("session_token", "user", "bot_controller"):
            st.session_state.pop(key, None)
        st.rerun()

    # Navigation
    st.sidebar.markdown("---")
    page = st.sidebar.radio("Navigation", [
        "Overview",
        "Instruments",
        "Settings",
        "Trading Status",
        "Logs",
        "Analytics"
    ])

    # Main content
    if page == "Overview":
        show_overview(status)
    elif page == "Instruments":
        show_instruments()
    elif page == "Settings":
        show_settings()
    elif page == "Trading Status":
        show_trading_status()
    elif page == "Logs":
        show_logs()
    elif page == "Analytics":
        show_analytics()

def show_overview(status):
    st.title("🏠 Overview")

    account_info = bot_controller.get_account_info()
    open_positions = bot_controller.get_open_positions()

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if "error" not in account_info:
            st.metric("Account Balance", f"${account_info.get('balance', 0):,.2f}")
        else:
            st.metric("Bot Status", "Running" if status["running"] else "Stopped")

    with col2:
        if "error" not in account_info:
            st.metric("Account Equity", f"${account_info.get('equity', 0):,.2f}")
        else:
            st.metric("Mode", status["mode"].upper())

    with col3:
        st.metric("Active Instruments", len(bot_controller.config.instruments))
        
    with col4:
        st.metric("Open Positions", len(open_positions))

    st.markdown("---")

    # Account Info Section
    if "error" not in account_info:
        st.subheader("💰 Account Information")
        acc_col1, acc_col2, acc_col3, acc_col4 = st.columns(4)
        with acc_col1:
            st.metric("Balance", f"${account_info.get('balance', 0):,.2f}")
            st.metric("Currency", account_info.get('currency', 'USD'))
        with acc_col2:
            st.metric("Equity", f"${account_info.get('equity', 0):,.2f}")
            st.metric("Margin Level", f"{account_info.get('margin_level', 0):.2f}%")
        with acc_col3:
            st.metric("Profit", f"${account_info.get('profit', 0):,.2f}")
            st.metric("Margin Used", f"${account_info.get('margin_used', 0):,.2f}")
        with acc_col4:
            st.metric("Free Margin", f"${account_info.get('margin_free', 0):,.2f}")
            st.metric("Server", account_info.get('server', 'N/A'))

    # Open Positions Section
    st.subheader("📈 Open Positions")
    if open_positions:
        positions_df = pd.DataFrame(open_positions)
        st.dataframe(positions_df, width='stretch')
    else:
        st.info("No open positions")

    st.markdown("---")

    # Instruments
    st.subheader("📊 Configured Instruments")
    configured_instruments = bot_controller.config.instruments
    instruments_df = pd.DataFrame({
        "Symbol": configured_instruments,
        "Status": ["Active" for _ in configured_instruments],
        "Category": [CATALOG.get(sym).category if sym in CATALOG else "Unknown" for sym in configured_instruments]
    })
    st.dataframe(instruments_df, width='stretch')

    # Quick Actions
    st.subheader("⚡ Quick Actions")
    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("▶️ Start Bot", width='stretch'):
            success, msg = bot_controller.start_bot()
            if success:
                st.success(f"✅ {msg}")
            else:
                st.warning(f"⚠️ {msg}")

    with col2:
        if st.button("⏹️ Stop Bot", width='stretch'):
            success, msg = bot_controller.stop_bot()
            if success:
                st.info(f"⏹️ {msg}")
            else:
                st.warning(f"⚠️ {msg}")

    with col3:
        if st.button("🔃 Reload Config", width='stretch'):
            if bot_controller.reload_config():
                st.success("✅ Configuration reloaded!")
                st.rerun()
            else:
                st.error("❌ Failed to reload configuration")

def show_instruments():
    st.title("📋 Instrument Management")

    categories = all_categories()
    current = set(bot_controller.config.instruments)

    st.subheader("Select Instruments to Trade")

    cols = st.columns(len(categories))
    selected_instruments = set(current)

    for idx, category in enumerate(categories):
        with cols[idx]:
            st.markdown(f"**{category.upper()}**")
            instruments_in_cat = list_by_category(category)

            for inst_info in instruments_in_cat:
                if st.checkbox(
                    f"{inst_info.symbol}",
                    value=inst_info.symbol in current,
                    key=f"inst_{inst_info.symbol}"
                ):
                    selected_instruments.add(inst_info.symbol)
                else:
                    selected_instruments.discard(inst_info.symbol)

    st.markdown("---")

    if st.button("💾 Save Instrument Selection", type="primary", width='stretch'):
        bot_controller.config.instruments = sorted(list(selected_instruments))
        success, msg = bot_controller.save_config(bot_controller.config)
        if success:
            st.success(f"✅ {msg}")
            st.info(f"Selected: {', '.join(bot_controller.config.instruments)}")
        else:
            st.error(f"❌ {msg}")

    st.markdown("---")
    st.subheader("⚙️ Per-Instrument Settings")

    if selected_instruments:
        selected_inst = st.selectbox("Select Instrument", sorted(selected_instruments))

        inst_info = CATALOG.get(selected_inst)
        if inst_info:
            col1, col2 = st.columns(2)
            with col1:
                st.text(f"Category: {inst_info.category}")
                st.text(f"Exchange: {inst_info.exchange}")
            with col2:
                st.text(f"Multiplier: {inst_info.multiplier}")
                st.text(f"Spread: {inst_info.typical_spread}")

        st.markdown("---")

        per_inst = bot_controller.config.per_instrument.get(selected_inst, {})

        col1, col2 = st.columns(2)
        with col1:
            min_confidence = st.number_input(
                "Min Confidence",
                min_value=0.0, max_value=1.0,
                value=per_inst.get("min_confidence", bot_controller.config.min_confidence),
                step=0.01, key=f"min_conf_{selected_inst}"
            )

            max_risk = st.number_input(
                "Max Risk % (override)",
                min_value=0.001, max_value=0.1,
                value=per_inst.get("max_risk_pct", bot_controller.config.max_risk_per_trade_pct),
                step=0.001, key=f"max_risk_{selected_inst}"
            )

        with col2:
            min_rr = st.number_input(
                "Min R:R Ratio",
                min_value=0.5, max_value=5.0,
                value=per_inst.get("min_rr_ratio", bot_controller.config.min_rr_ratio),
                step=0.1, key=f"min_rr_{selected_inst}"
            )

            atr_mult = st.number_input(
                "ATR SL Multiplier",
                min_value=0.5, max_value=5.0,
                value=per_inst.get("atr_sl_multiplier", bot_controller.config.atr_sl_multiplier),
                step=0.1, key=f"atr_mult_{selected_inst}"
            )

        if st.button(f"💾 Save {selected_inst} Settings", width='stretch'):
            if selected_inst not in bot_controller.config.per_instrument:
                bot_controller.config.per_instrument[selected_inst] = {}

            bot_controller.config.per_instrument[selected_inst]["min_confidence"] = min_confidence
            bot_controller.config.per_instrument[selected_inst]["max_risk_pct"] = max_risk
            bot_controller.config.per_instrument[selected_inst]["min_rr_ratio"] = min_rr
            bot_controller.config.per_instrument[selected_inst]["atr_sl_multiplier"] = atr_mult

            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ Saved settings for {selected_inst}")
            else:
                st.error(f"❌ {msg}")
    else:
        st.warning("No instruments selected")

def show_settings():
    st.title("⚙️ Global Settings")

    st.info("Configure global trading parameters")

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "General",
        "Risk",
        "Technical Analysis",
        "Buffer & Data",
        "MetaApi"
    ])

    with tab1:
        st.subheader("General Settings")

        mode = st.selectbox("Mode", ["paper", "live"], index=0 if bot_controller.config.mode == "paper" else 1, key="settings_general_mode")
        loop_interval = st.number_input("Loop Interval (sec)", min_value=5, max_value=300, value=int(bot_controller.config.loop_interval_sec), key="settings_general_loop_interval")
        use_kelly = st.checkbox("Kelly Sizing", value=bot_controller.config.use_kelly_sizing, key="settings_general_use_kelly")
        kelly_fraction = st.slider("Kelly Fraction", 0.1, 1.0, bot_controller.config.kelly_fraction, 0.1, key="settings_general_kelly_fraction")
        trailing_stop = st.checkbox("Trailing Stop", value=bot_controller.config.trailing_stop_after_tp1, key="settings_general_trailing_stop")
        signal_logging = st.checkbox("Signal Logging", value=bot_controller.config.signal_logging, key="settings_general_signal_logging")

        if st.button("💾 Save", width='stretch', key="settings_general_save"):
            bot_controller.config.mode = mode
            bot_controller.config.loop_interval_sec = loop_interval
            bot_controller.config.use_kelly_sizing = use_kelly
            bot_controller.config.kelly_fraction = kelly_fraction
            bot_controller.config.trailing_stop_after_tp1 = trailing_stop
            bot_controller.config.signal_logging = signal_logging
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.toast(msg)
                bot_controller.reload_config()
                st.rerun()
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab2:
        st.subheader("Risk Management")

        col1, col2 = st.columns(2)
        with col1:
            max_risk = st.number_input("Max Risk (%)", 0.001, 0.1, bot_controller.config.max_risk_per_trade_pct, 0.001, key="settings_risk_max_risk")
            max_drawdown = st.number_input("Max Drawdown (%)", 0.01, 0.5, bot_controller.config.max_daily_drawdown_pct, 0.01, key="settings_risk_max_drawdown")
            portfolio_heat = st.number_input("Heat Cap (%)", 0.01, 0.5, bot_controller.config.portfolio_heat_cap_pct, 0.01, key="settings_risk_portfolio_heat")

        with col2:
            max_concurrent = st.number_input("Max Positions", 1, 10, bot_controller.config.max_concurrent_positions, key="settings_risk_max_concurrent")
            atr_sl_mult = st.number_input("ATR Mult", 0.5, 5.0, bot_controller.config.atr_sl_multiplier, 0.1, key="settings_risk_atr_sl_mult")
            kelly_cap = st.number_input("Kelly Cap (%)", 0.001, 0.1, bot_controller.config.kelly_cap_pct, 0.001, key="settings_risk_kelly_cap")

        if st.button("💾 Save", width='stretch', key="settings_risk_save"):
            bot_controller.config.max_risk_per_trade_pct = max_risk
            bot_controller.config.max_daily_drawdown_pct = max_drawdown
            bot_controller.config.portfolio_heat_cap_pct = portfolio_heat
            bot_controller.config.max_concurrent_positions = max_concurrent
            bot_controller.config.atr_sl_multiplier = atr_sl_mult
            bot_controller.config.kelly_cap_pct = kelly_cap
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab3:
        st.subheader("Technical Analysis")

        col1, col2 = st.columns(2)
        with col1:
            rsi = st.number_input("RSI", 5, 50, bot_controller.config.rsi_period, key="settings_ta_rsi")
            macd_fast = st.number_input("MACD Fast", 5, 30, bot_controller.config.macd_fast, key="settings_ta_macd_fast")
            macd_slow = st.number_input("MACD Slow", 20, 100, bot_controller.config.macd_slow, key="settings_ta_macd_slow")
            macd_sig = st.number_input("MACD Sig", 5, 30, bot_controller.config.macd_signal, key="settings_ta_macd_sig")

        with col2:
            bb = st.number_input("BB Period", 10, 50, bot_controller.config.bb_period, key="settings_ta_bb")
            bb_std = st.number_input("BB Std", 1.0, 5.0, bot_controller.config.bb_std, 0.1, key="settings_ta_bb_std")
            atr = st.number_input("ATR", 5, 50, bot_controller.config.atr_period, key="settings_ta_atr")
            vol_ma = st.number_input("Vol MA", 5, 50, bot_controller.config.volume_ma_period, key="settings_ta_vol_ma")

        if st.button("💾 Save", width='stretch', key="settings_ta_save"):
            bot_controller.config.rsi_period = rsi
            bot_controller.config.macd_fast = macd_fast
            bot_controller.config.macd_slow = macd_slow
            bot_controller.config.macd_signal = macd_sig
            bot_controller.config.bb_period = bb
            bot_controller.config.bb_std = bb_std
            bot_controller.config.atr_period = atr
            bot_controller.config.volume_ma_period = vol_ma
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab4:
        st.subheader("Buffer & Data")

        col1, col2 = st.columns(2)
        with col1:
            history = st.number_input("History Bars", 100, 2000, bot_controller.config.history_bars, key="settings_data_history")
            cal_days = st.number_input("Calibration Days", 30, 365, bot_controller.config.calibration_window_days, key="settings_data_cal_days")
            buf_min = st.number_input("Buffer Min", 1, 30, bot_controller.config.buffer_min_delay_min, key="settings_data_buf_min")

        with col2:
            buf_max = st.number_input("Buffer Max", 10, 60, bot_controller.config.buffer_max_delay_min, key="settings_data_buf_max")
            cal_step = st.number_input("Cal Step", 1, 10, bot_controller.config.calibration_step_min, key="settings_data_cal_step")
            auto_cal = st.checkbox("Auto Calibrate", bot_controller.config.auto_calibrate, key="settings_data_auto_cal")

        if st.button("💾 Save", width='stretch', key="settings_data_save"):
            bot_controller.config.history_bars = history
            bot_controller.config.calibration_window_days = cal_days
            bot_controller.config.buffer_min_delay_min = buf_min
            bot_controller.config.buffer_max_delay_min = buf_max
            bot_controller.config.calibration_step_min = cal_step
            bot_controller.config.auto_calibrate = auto_cal
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab5:
        st.subheader("MetaApi Connection")

        has_credentials = bot_controller.auth_store.has_credentials(bot_controller.user_id)
        st.info("MetaApi tokens are encrypted at rest and are never written to config.yaml.")
        metaapi_account_id = st.text_input("Account ID", value=bot_controller.config.metaapi_account_id, key="settings_metaapi_account_id")
        metaapi_token = st.text_input("Token", type="password", value="", key="settings_metaapi_token")
        if has_credentials:
            st.caption("A token is already saved. Enter a new token only when rotating credentials.")

        st.warning("Use environment variables or your deployment secret store in production.")

        if st.button("Apply Credentials", width='stretch', key="settings_metaapi_save"):
            if not metaapi_account_id.strip() or (not has_credentials and not metaapi_token.strip()):
                st.error("MetaApi token and account ID are required.")
            else:
                token_to_save = metaapi_token or bot_controller.config.metaapi_token
                success, msg = bot_controller.set_metaapi_credentials(token_to_save, metaapi_account_id)
                if success:
                    st.success(msg)
                else:
                    st.error(msg)

def show_trading_status():
    st.title("💰 Trading Status")

    # Helper function to run async calls
    def run_async(coro, default):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(coro)
            loop.close()
            return result
        except Exception as e:
            return default

    # Get real-time stats from dashboard service
    signal_stats = run_async(bot_controller.dashboard_service.get_signal_stats(), {})
    trade_stats = run_async(bot_controller.dashboard_service.get_trade_stats(), {})
    recent_signals = run_async(bot_controller.dashboard_service.get_recent_signals(20), pd.DataFrame())
    trade_history = run_async(bot_controller.dashboard_service.get_trade_history(20), pd.DataFrame())

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Signals (24h)", signal_stats.get("total_signals", "—"))
    with col2:
        win_rate = trade_stats.get("win_rate", 0)
        st.metric("Win Rate (24h)", f"{win_rate}%" if win_rate != 0 else "—%")
    with col3:
        st.metric("Active Instruments", len(bot_controller.config.instruments))

    st.subheader("📊 Recent Signals (24h)")
    if not recent_signals.empty:
        st.dataframe(recent_signals, width='stretch')
    else:
        st.info("No signals yet")

    st.subheader("📈 Trade History (24h)")
    if not trade_history.empty:
        st.dataframe(trade_history, width='stretch')
    else:
        st.info("No trades yet")

    # Display trade stats if available
    if trade_stats and trade_stats.get("total_trades", 0) > 0:
        st.subheader("💹 Trading Statistics (24h)")
        stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
        with stat_col1:
            st.metric("Total Trades", trade_stats.get("total_trades", 0))
        with stat_col2:
            st.metric("Winning Trades", trade_stats.get("winning_trades", 0))
        with stat_col3:
            st.metric("Win Rate", f"{trade_stats.get('win_rate', 0):.2f}%")
        with stat_col4:
            st.metric("Total P&L", f"${trade_stats.get('total_pnl', 0):,.2f}")

def show_logs():
    st.title("📋 Logs")

    log_dir = Path("logs")
    if log_dir.exists():
        log_files = list(log_dir.glob("*.log"))
        if log_files:
            latest = max(log_files, key=lambda x: x.stat().st_mtime)
            st.subheader(f"Logs ({latest.name})")

            try:
                with open(latest, 'r', encoding='utf-8') as f:
                    lines = f.readlines()[-100:]
                st.code("".join(lines), language="text")

                if st.button("🔄 Refresh"):
                    st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
        else:
            st.info("No logs")
    else:
        st.info("Logs directory not found")

def show_analytics():
    st.title("📊 Analytics")

    # Get analytics data from dashboard service
    signal_stats = {}
    trade_stats = {}
    instrument_performance = pd.DataFrame()
    
    # Use asyncio to run the async functions from dashboard service
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        signal_stats = loop.run_until_complete(bot_controller.dashboard_service.get_signal_stats())
        trade_stats = loop.run_until_complete(bot_controller.dashboard_service.get_trade_stats())
        instrument_performance = loop.run_until_complete(bot_controller.dashboard_service.get_instrument_performance())
        loop.close()
    except Exception as e:
        st.warning(f"Could not fetch analytics data: {e}")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Signals (24h)", signal_stats.get("total_signals", "—"))
    with col2:
        st.metric("Emitted Signals (24h)", signal_stats.get("emitted_signals", "—"))
    with col3:
        st.metric("Active Instruments", len(bot_controller.config.instruments))

    st.subheader("📈 Signal Statistics (24h)")
    if signal_stats:
        sig_col1, sig_col2 = st.columns(2)
        with sig_col1:
            st.metric("Total Signals", signal_stats.get("total_signals", 0))
            st.metric("Emitted Signals", signal_stats.get("emitted_signals", 0))
        with sig_col2:
            emitted = signal_stats.get("emitted_signals", 0)
            total = signal_stats.get("total_signals", 0)
            if total > 0:
                emission_rate = (emitted / total) * 100
                st.metric("Emission Rate", f"{emission_rate:.2f}%")
            else:
                st.metric("Emission Rate", "0%")
            avg_conf = signal_stats.get("avg_confidence", 0)
            st.metric("Avg Confidence", f"{avg_conf:.4f}")

    st.subheader("💹 Trading Statistics (24h)")
    if trade_stats:
        trade_col1, trade_col2, trade_col3, trade_col4 = st.columns(4)
        with trade_col1:
            st.metric("Total Trades", trade_stats.get("total_trades", 0))
        with trade_col2:
            st.metric("Winning Trades", trade_stats.get("winning_trades", 0))
        with trade_col3:
            st.metric("Win Rate", f"{trade_stats.get('win_rate', 0):.2f}%")
        with trade_col4:
            st.metric("Total P&L", f"${trade_stats.get('total_pnl', 0):,.2f}")

    st.subheader("📊 Instrument Performance (24h)")
    if not instrument_performance.empty:
        st.dataframe(instrument_performance, width='stretch')
    else:
        st.info("No instrument performance data available")

    # Keep the original performance chart as a fallback
    st.subheader("Performance (Sample)")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[datetime.now() - timedelta(days=i) for i in range(30, 0, -1)],
        y=[100 + i*0.1 for i in range(30)],
        mode='lines+markers'
    ))
    fig.update_layout(title="Portfolio Value (Sample)")
    st.plotly_chart(fig, width='stretch')

if __name__ == "__main__":
    main()
