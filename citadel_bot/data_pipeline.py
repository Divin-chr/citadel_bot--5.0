"""
data_pipeline.py — Real-time and historical data feed via MetaApi
Now with PostgreSQL persistence for zero-downtime operation
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, List

import pandas as pd

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager

log = logging.getLogger("pipeline")


class DataPipeline:
    """
    Pulls 1-minute bars from MT5 for each configured instrument and keeps
    a rolling DataFrame with `config.history_bars` rows.

    Now includes PostgreSQL persistence for:
    - Real-time data storage
    - Historical data retrieval
    - Zero-downtime operation
    """

    def __init__(self, config: BotConfig, account, connection):
        self.config = config
        self.account = account
        self.connection = connection
        self._bars: Dict[str, pd.DataFrame] = {}
        self._persisted_m1: Dict[str, pd.DataFrame] = {}
        self._data_dir = Path(self.config.data_dir) / "market_data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # Feed a deeper context window to TA/calibration when local history exists.
        self._analysis_window_bars = max(self.config.history_bars, 5000)
        self._db_available = False

    async def get_realtime(self, sym: str) -> Optional[pd.DataFrame]:
        """Return latest rolling bars and refresh from MT5 on each call."""
        await self._refresh_symbol(sym)
        df = self._bars.get(sym)
        if df is not None and not df.empty:
            return df
        # Fallback to persisted history if MT5 refresh returns nothing.
        persisted = self._load_persisted_m1(sym)
        if persisted is not None and not persisted.empty:
            self._bars[sym] = persisted.tail(self._analysis_window_bars)
            return self._bars[sym]

        # Try loading from database as final fallback
        if self._db_available:
            return await self._load_from_database(sym)
        return None

    async def start_feeds(self):
        """Warm up data windows for all configured symbols."""
        # Check database availability
        self._db_available = await db_manager.health_check()
        if self._db_available:
            log.info("✅ Database available for data persistence")
        else:
            log.warning("⚠️  Database not available, using CSV fallback only")

        for sym in self.config.instruments:
            persisted = self._load_persisted_m1(sym)
            if persisted is not None and not persisted.empty:
                self._persisted_m1[sym] = persisted
                self._bars[sym] = persisted.tail(self._analysis_window_bars)

            # Try loading additional history from database
            if self._db_available:
                await self._load_from_database(sym)

            # Symbol selection not needed in MetaApi
            await self._refresh_symbol(sym)
            df = self._bars.get(sym, pd.DataFrame())
            log.info("[%s] History loaded: %s bars", sym, len(df))

    async def _load_from_database(self, sym: str) -> Optional[pd.DataFrame]:
        """Load historical data from database"""
        if not self._db_available:
            return None

        try:
            # Get instrument ID
            instrument_id = await db_manager.get_instrument_id(sym)
            if not instrument_id:
                return None

            # Load recent data from database
            market_data = await db_manager.get_market_data(sym, 'm1', self._analysis_window_bars)
            if not market_data:
                return None

            # Convert to DataFrame
            df = pd.DataFrame([
                {
                    'datetime': row['timestamp_utc'],
                    'open': row['open_price'],
                    'high': row['high_price'],
                    'low': row['low_price'],
                    'close': row['close_price'],
                    'volume': row['volume']
                }
                for row in market_data
            ])

            df['datetime'] = pd.to_datetime(df['datetime'], utc=True)
            df = df.set_index('datetime').sort_index()

            # Merge with existing data
            existing = self._bars.get(sym)
            if existing is not None and not existing.empty:
                combined = pd.concat([existing, df])
                combined = combined[~combined.index.duplicated(keep='last')]
                self._bars[sym] = combined.sort_index().tail(self._analysis_window_bars)
            else:
                self._bars[sym] = df

            log.debug("[%s] Loaded %d bars from database", sym, len(df))
            return self._bars[sym]

        except Exception as e:
            log.warning("[%s] Failed to load from database: %s", sym, e)
            return None

    async def _refresh_symbol(self, sym: str):
        try:
            from datetime import datetime, timedelta
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(minutes=self.config.history_bars)
            candles = await self.account.get_historical_candles(sym, '1m', start_time, self.config.history_bars)
            if candles is None or len(candles) == 0:
                return
            df = pd.DataFrame(candles)
            if df.empty:
                return
            df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("datetime").sort_index()
            df = df.rename(columns={"tickVolume": "volume"}) if "tickVolume" in df else df
            for col in ["open", "high", "low", "close", "volume"]:
                if col not in df.columns:
                    df[col] = 0.0
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
            self._persist_symbol_data(sym, df)
            merged = self._persisted_m1.get(sym, df)
            self._bars[sym] = merged.tail(self._analysis_window_bars)
        except Exception as exc:
            log.error("[%s] MetaApi refresh error: %s", sym, exc)

    def _persist_symbol_data(self, sym: str, new_m1: pd.DataFrame):
        """
        Persist all received 1-minute bars to both CSV (fallback) and database (primary):
          - {sym}_m1.csv (fallback)
          - PostgreSQL market_data table (primary)
          - Aggregated CSV files (h1, d1, w1) for compatibility
        """
        m1_path = self._data_dir / f"{sym}_m1.csv"
        if sym not in self._persisted_m1:
            if m1_path.exists():
                try:
                    hist = pd.read_csv(m1_path, index_col=0, parse_dates=True)
                    hist.index = pd.to_datetime(hist.index, utc=True)
                    hist = hist[["open", "high", "low", "close", "volume"]].astype(float)
                except Exception:
                    hist = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            else:
                hist = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            self._persisted_m1[sym] = hist

        merged = pd.concat([self._persisted_m1[sym], new_m1])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._persisted_m1[sym] = merged

        # Save to CSV (fallback)
        merged.to_csv(m1_path)

        # Save to database (primary) - run in background to avoid blocking
        if self._db_available:
            asyncio.create_task(self._persist_to_database(sym, new_m1))

        # Maintain aggregated CSV files for compatibility
        agg_map = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        for rule, suffix in [("1h", "h1"), ("1D", "d1"), ("1W", "w1")]:
            agg = merged.resample(rule).agg(agg_map).dropna()
            agg.to_csv(self._data_dir / f"{sym}_{suffix}.csv")

    async def _persist_to_database(self, sym: str, df: pd.DataFrame):
        """Persist market data to database asynchronously"""
        try:
            instrument_id = await db_manager.get_instrument_id(sym)
            if not instrument_id:
                log.warning("[%s] Instrument not found in database, skipping persistence", sym)
                return

            # Insert each bar
            for timestamp, row in df.iterrows():
                await db_manager.insert_market_data(
                    instrument_id=instrument_id,
                    timeframe='m1',
                    timestamp_utc=timestamp.to_pydatetime(),
                    open_price=float(row['open']),
                    high_price=float(row['high']),
                    low_price=float(row['low']),
                    close_price=float(row['close']),
                    volume=int(row['volume'])
                )

            log.debug("[%s] Persisted %d bars to database", sym, len(df))

        except Exception as e:
            log.error("[%s] Failed to persist to database: %s", sym, e)

    def _load_persisted_m1(self, sym: str) -> Optional[pd.DataFrame]:
        m1_path = self._data_dir / f"{sym}_m1.csv"
        if not m1_path.exists():
            return None
        try:
            df = pd.read_csv(m1_path, index_col=0, parse_dates=True)
            if df.empty:
                return None
            df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
            df = df[~df.index.isna()].sort_index()
            for col in ["open", "high", "low", "close", "volume"]:
                if col not in df.columns:
                    df[col] = 0.0
            return df[["open", "high", "low", "close", "volume"]].astype(float)
        except Exception as exc:
            log.warning("[%s] Could not load persisted m1 CSV: %s", sym, exc)
            return None
