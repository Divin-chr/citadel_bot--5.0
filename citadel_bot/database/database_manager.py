"""
Database connection manager for Citadel Quant Bot
Provides async database operations with connection pooling
"""

import asyncpg
import json
import logging
import os
import yaml
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("database")

class DatabaseManager:
    """
    Centralized database connection manager with connection pooling.
    Handles all database operations for the Citadel Quant Bot.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = self._build_config(config)
        self.pool: Optional[asyncpg.Pool] = None

    def configure(self, config: Optional[Dict[str, Any]] = None):
        """Refresh settings without replacing the global manager object."""
        self.config = self._build_config(config)

    def _build_config(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        file_config = self._load_config_file()
        merged = {}
        if file_config:
            merged.update(file_config)
        if config:
            merged.update(config)

        pool_settings = {
            'min_size': int(os.environ.get('DATABASE_POOL_MIN_SIZE') or merged.get('min_size') or 1),
            'max_size': int(os.environ.get('DATABASE_POOL_MAX_SIZE') or merged.get('max_size') or 5),
            'max_queries': int(os.environ.get('DATABASE_MAX_QUERIES') or merged.get('max_queries') or 50000),
            'max_inactive_connection_lifetime': float(
                os.environ.get('DATABASE_MAX_INACTIVE_CONNECTION_LIFETIME')
                or merged.get('max_inactive_connection_lifetime')
                or 300.0
            ),
        }

        dsn = (
            os.environ.get("DATABASE_URL")
            or os.environ.get("CITADEL_DATABASE_URL")
            or merged.get("database_url")
        )
        if dsn:
            return {'dsn': dsn, **pool_settings}

        return {
            'host': (
                os.environ.get('DATABASE_HOST')
                or os.environ.get('CITADEL_DATABASE_HOST')
                or merged.get('host')
                or merged.get('database_host')
                or 'localhost'
            ),
            'port': int(
                os.environ.get('DATABASE_PORT')
                or os.environ.get('CITADEL_DATABASE_PORT')
                or merged.get('port')
                or merged.get('database_port')
                or 5432
            ),
            'user': (
                os.environ.get('DATABASE_USER')
                or os.environ.get('CITADEL_DATABASE_USER')
                or merged.get('user')
                or merged.get('database_user')
                or 'postgres'
            ),
            'password': (
                os.environ.get('DATABASE_PASSWORD')
                or os.environ.get('CITADEL_DATABASE_PASSWORD')
                or merged.get('password')
                or merged.get('database_password')
                or ''
            ),
            'database': (
                os.environ.get('DATABASE_NAME')
                or os.environ.get('CITADEL_DATABASE_NAME')
                or merged.get('database')
                or merged.get('database_name')
                or 'citadel_bot'
            ),
            **pool_settings,
        }

    def _load_config_file(self) -> Dict[str, Any]:
        for path in (Path("config.yaml"), Path("citadel_bot/config/config.yaml")):
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8-sig") as f:
                    return yaml.safe_load(f) or {}
            except Exception as exc:
                log.debug("Could not load database settings from %s: %s", path, exc)
        return {}

    async def connect(self):
        """Initialize connection pool"""
        if self.pool:
            return
        try:
            self.pool = await asyncpg.create_pool(**self.config)
            log.info("✅ Database connection pool initialized")
        except Exception as e:
            log.error(f"❌ Failed to initialize database pool: {e}")
            raise

    async def disconnect(self):
        """Close connection pool"""
        if self.pool:
            await self.pool.close()
            self.pool = None
            log.info("✅ Database connection pool closed")

    @asynccontextmanager
    async def connection(self):
        """Get a connection from the pool"""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            yield conn

    # =================================================================================
    # INSTRUMENT OPERATIONS
    # =================================================================================

    async def get_instrument_id(self, symbol: str) -> Optional[int]:
        """Get instrument ID by symbol"""
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT instrument_id FROM instruments WHERE symbol = $1",
                symbol
            )
            return row['instrument_id'] if row else None

    async def get_instrument_info(self, symbol: str) -> Optional[Dict]:
        """Get full instrument information"""
        async with self.connection() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM instruments WHERE symbol = $1
            """, symbol)
            return dict(row) if row else None

    async def get_all_instruments(self) -> List[Dict]:
        """Get all instruments"""
        async with self.connection() as conn:
            rows = await conn.fetch("SELECT * FROM instruments ORDER BY symbol")
            return [dict(row) for row in rows]

    # =================================================================================
    # MARKET DATA OPERATIONS
    # =================================================================================

    async def insert_market_data(self, instrument_id: int, timeframe: str,
                                timestamp_utc, open_price: float, high_price: float,
                                low_price: float, close_price: float, volume: int):
        """Insert market data bar"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO market_data (
                    instrument_id, timestamp_utc, timeframe,
                    open_price, high_price, low_price, close_price, volume
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (instrument_id, timestamp_utc, timeframe) DO UPDATE SET
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    volume = EXCLUDED.volume
            """,
            instrument_id, timestamp_utc, timeframe,
            open_price, high_price, low_price, close_price, volume
            )

    async def get_market_data(self, symbol: str, timeframe: str = 'm1',
                             limit: int = 400) -> Optional[List[Dict]]:
        """Get recent market data for symbol"""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return None

        async with self.connection() as conn:
            rows = await conn.fetch("""
                SELECT * FROM market_data
                WHERE instrument_id = $1 AND timeframe = $2
                ORDER BY timestamp_utc DESC
                LIMIT $3
            """, instrument_id, timeframe, limit)

            return [dict(row) for row in rows]

    async def get_latest_market_data(self, symbol: str, timeframe: str = 'm1') -> Optional[Dict]:
        """Get latest market data bar for symbol"""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return None

        async with self.connection() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM market_data
                WHERE instrument_id = $1 AND timeframe = $2
                ORDER BY timestamp_utc DESC
                LIMIT 1
            """, instrument_id, timeframe)

            return dict(row) if row else None

    # =================================================================================
    # SIGNAL LOG OPERATIONS
    # =================================================================================

    async def insert_signal_log(self, signal_data: Dict):
        """Insert signal log entry"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO signal_logs (
                    timestamp_utc, instrument_id, score_trend, score_momentum,
                    score_acceleration, score_volatility, score_structure,
                    trend_daily, trend_weekly, trend_monthly, rsi, macd_hist,
                    macd_cross, bb_pct, bb_squeeze, atr, atr_pct, volume_ratio,
                    nearest_support, nearest_resistance, patterns,
                    composite_score, confidence, direction, rt_momentum,
                    delta_aligned, alignment_score, signal_emitted, rejection_gate,
                    entry_price, stop_loss, tp1, tp2, rr_ratio, vol_regime
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24,
                    $25, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35
                )
            """,
            signal_data['timestamp_utc'],
            signal_data['instrument_id'],
            signal_data.get('score_trend'),
            signal_data.get('score_momentum'),
            signal_data.get('score_acceleration'),
            signal_data.get('score_volatility'),
            signal_data.get('score_structure'),
            signal_data.get('trend_daily'),
            signal_data.get('trend_weekly'),
            signal_data.get('trend_monthly'),
            signal_data.get('rsi'),
            signal_data.get('macd_hist'),
            signal_data.get('macd_cross'),
            signal_data.get('bb_pct'),
            signal_data.get('bb_squeeze'),
            signal_data.get('atr'),
            signal_data.get('atr_pct'),
            signal_data.get('volume_ratio'),
            signal_data.get('nearest_support'),
            signal_data.get('nearest_resistance'),
            signal_data.get('patterns', []),
            signal_data['composite_score'],
            signal_data['confidence'],
            signal_data['direction'],
            signal_data.get('rt_momentum'),
            signal_data.get('delta_aligned'),
            signal_data.get('alignment_score'),
            signal_data['signal_emitted'],
            signal_data.get('rejection_gate'),
            signal_data.get('entry_price'),
            signal_data.get('stop_loss'),
            signal_data.get('tp1'),
            signal_data.get('tp2'),
            signal_data.get('rr_ratio'),
            signal_data.get('vol_regime', 'NORMAL')
            )

    # =================================================================================
    # TRADE LEDGER OPERATIONS
    # =================================================================================

    async def insert_trade_ledger_entry(self, trade_data: Dict):
        """Insert trade ledger entry"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO trade_ledger (
                    timestamp_utc, event_type, mode, instrument_id,
                    parent_order_id, order_id, direction, qty_delta,
                    qty_open, fill_price, pnl_delta_usd, realized_pnl_usd,
                    status, note
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            trade_data['timestamp_utc'],
            trade_data['event_type'],
            trade_data['mode'],
            trade_data['instrument_id'],
            trade_data.get('parent_order_id'),
            trade_data.get('order_id'),
            trade_data['direction'],
            trade_data['qty_delta'],
            trade_data['qty_open'],
            trade_data.get('fill_price'),
            trade_data.get('pnl_delta_usd'),
            trade_data.get('realized_pnl_usd'),
            trade_data.get('status'),
            trade_data.get('note')
            )

    # =================================================================================
    # BUFFER CALIBRATION OPERATIONS
    # =================================================================================

    async def get_optimal_buffer_delay(self, symbol: str) -> int:
        """Get optimal buffer delay for symbol"""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return 12  # Default fallback

        async with self.connection() as conn:
            row = await conn.fetchrow("""
                SELECT optimal_delay_min
                FROM buffer_calibration
                WHERE instrument_id = $1 AND is_significant = true
                ORDER BY run_timestamp DESC
                LIMIT 1
            """, instrument_id)

            return row['optimal_delay_min'] if row else 12

    async def save_buffer_calibration(self, calibration_data: Dict):
        """Save buffer calibration results"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO buffer_calibration (
                    instrument_id, run_timestamp, min_delay_min, max_delay_min,
                    step_min, calibration_window_days, optimal_delay_min,
                    best_sharpe, p_value, is_significant, n_bars, n_windows,
                    candidates, delay_mean_val_sharpe, window_winners
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            calibration_data['instrument_id'],
            calibration_data['run_timestamp'],
            calibration_data['min_delay_min'],
            calibration_data['max_delay_min'],
            calibration_data['step_min'],
            calibration_data['calibration_window_days'],
            calibration_data['optimal_delay_min'],
            calibration_data['best_sharpe'],
            calibration_data['p_value'],
            calibration_data['is_significant'],
            calibration_data['n_bars'],
            calibration_data['n_windows'],
            calibration_data['candidates'],
            calibration_data['delay_mean_val_sharpe'],
            calibration_data['window_winners']
            )

    # =================================================================================
    # UTILITY METHODS
    # =================================================================================

    async def health_check(self) -> bool:
        """Check database connectivity"""
        try:
            async with self.connection() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def get_stats(self) -> Dict:
        """Get database statistics"""
        async with self.connection() as conn:
            stats = {}

            # Table counts
            for table in ['instruments', 'market_data', 'signal_logs', 'trade_ledger', 'buffer_calibration']:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                stats[f'{table}_count'] = count

            return stats

# Global database manager instance
db_manager = DatabaseManager()

async def init_database(config: Optional[Dict] = None) -> DatabaseManager:
    """Initialize global database manager"""
    db_manager.configure(config)
    await db_manager.connect()
    return db_manager

async def close_database():
    """Close global database manager"""
    await db_manager.disconnect()
