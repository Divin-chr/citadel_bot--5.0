#!/usr/bin/env python3
"""
Database Migration Script for Citadel Quant Bot
Migrates all existing CSV/JSON data to PostgreSQL
"""

import asyncio
import json
import os
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import asyncpg
from typing import Dict, List

from citadel_bot.database.database_manager import db_manager


def get_connection_config() -> Dict:
    """Use the same database settings as the bot and dashboard."""
    allowed = {"dsn", "host", "port", "user", "password", "database"}
    return {key: value for key, value in db_manager.config.items() if key in allowed}

class DatabaseMigrator:
    def __init__(self):
        self.conn = None
        self.data_dir = Path('data')
        self.project_root = Path('.')

    async def connect(self):
        """Establish database connection"""
        try:
            self.conn = await asyncpg.connect(**get_connection_config())
            print("✅ Connected to PostgreSQL")
        except Exception as e:
            print(f"❌ Failed to connect to database: {e}")
            raise

    async def disconnect(self):
        """Close database connection"""
        if self.conn:
            await self.conn.close()
            print("✅ Database connection closed")

    async def create_schema(self):
        """Create database schema"""
        print("🔨 Creating database schema...")
        with open('citadel_bot/database/database_schema.sql', 'r') as f:
            schema_sql = f.read()

        await self.conn.execute(schema_sql)
        print("✅ Schema created successfully")

    async def populate_instruments(self):
        """Populate instruments table from catalog"""
        print("📊 Populating instruments table...")

        # Import the catalog (assuming it's available)
        try:
            from citadel_bot.utils.instrument_catalog import CATALOG
        except ImportError:
            print("⚠️  Could not import instrument_catalog.py, skipping instrument population")
            return

        for symbol, info in CATALOG.items():
            await self.conn.execute("""
                INSERT INTO instruments (
                    symbol, display_name, category, base_currency, quote_currency,
                    multiplier, exchange, session, description, typical_spread, aliases
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (symbol) DO NOTHING
            """,
            symbol, info.display_name, info.category, info.base_currency,
            info.quote_currency, info.multiplier, info.exchange, info.session,
            info.description, info.typical_spread, info.aliases
            )

        print("✅ Instruments populated")

    async def migrate_market_data(self):
        """Migrate all market data CSV files"""
        print("📈 Migrating market data...")

        market_data_dir = self.data_dir / 'market_data'
        if not market_data_dir.exists():
            print("⚠️  Market data directory not found, skipping")
            return

        # Get instrument mappings
        instrument_map = await self.get_instrument_map()

        for csv_file in market_data_dir.glob('*_m1.csv'):
            symbol = csv_file.stem.replace('_m1', '')
            if symbol not in instrument_map:
                print(f"⚠️  Skipping {symbol} - not in instrument catalog")
                continue

            print(f"  Migrating {symbol}...")

            try:
                # Read CSV with proper datetime parsing
                df = pd.read_csv(csv_file, index_col=0, parse_dates=True)
                df.index = pd.to_datetime(df.index, utc=True, errors='coerce')
                df = df.dropna()

                if df.empty:
                    continue

                # Prepare data for bulk insert
                data = []
                for timestamp, row in df.iterrows():
                    data.append((
                        instrument_map[symbol],
                        timestamp.to_pydatetime(),
                        'm1',
                        float(row['open']),
                        float(row['high']),
                        float(row['low']),
                        float(row['close']),
                        int(row['volume'])
                    ))

                # Bulk insert
                await self.conn.executemany("""
                    INSERT INTO market_data (
                        instrument_id, timestamp_utc, timeframe,
                        open_price, high_price, low_price, close_price, volume
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (instrument_id, timestamp_utc, timeframe) DO NOTHING
                """, data)

                print(f"    ✅ {symbol}: {len(data)} bars migrated")

            except Exception as e:
                print(f"    ❌ Failed to migrate {symbol}: {e}")

        print("✅ Market data migration complete")

    async def migrate_signal_logs(self):
        """Migrate signal log CSV"""
        print("📝 Migrating signal logs...")

        signal_log_file = self.data_dir / 'signal_log.csv'
        if not signal_log_file.exists():
            print("⚠️  Signal log file not found, skipping")
            return

        try:
            df = pd.read_csv(signal_log_file)
            instrument_map = await self.get_instrument_map()

            migrated_count = 0
            for _, row in df.iterrows():
                symbol = row['sym']
                if symbol not in instrument_map:
                    continue

                # Parse timestamp
                timestamp = pd.to_datetime(row['timestamp_utc'], utc=True)

                # Convert patterns list if present
                patterns = []
                if pd.notna(row.get('patterns')) and row['patterns']:
                    patterns = [p.strip() for p in str(row['patterns']).split(',') if p.strip()]

                rejection_gate = row.get('rejection_gate')
                if pd.isna(rejection_gate):
                    rejection_gate = None
                else:
                    rejection_gate = str(rejection_gate).strip()

                delta_aligned = row.get('delta_aligned')
                if pd.isna(delta_aligned):
                    delta_aligned = None
                else:
                    delta_aligned = bool(delta_aligned)

                alignment_score = row.get('alignment_score')
                if pd.isna(alignment_score):
                    alignment_score = None
                else:
                    alignment_score = float(alignment_score)

                signal_emitted = row.get('signal_emitted')
                if pd.isna(signal_emitted):
                    signal_emitted = False
                else:
                    signal_emitted = bool(signal_emitted)

                await self.conn.execute("""
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
                timestamp.to_pydatetime(),
                instrument_map[symbol],
                float(row.get('score_trend', 0)),
                float(row.get('score_momentum', 0)),
                float(row.get('score_acceleration', 0)),
                float(row.get('score_volatility', 0)),
                float(row.get('score_structure', 0)),
                row.get('trend_daily', 'NEUTRAL'),
                row.get('trend_weekly', 'NEUTRAL'),
                row.get('trend_monthly', 'NEUTRAL'),
                float(row.get('rsi', 50)),
                float(row.get('macd_hist', 0)),
                row.get('macd_cross', 'NONE'),
                float(row.get('bb_pct', 0.5)),
                bool(row.get('bb_squeeze', False)),
                float(row.get('atr', 0)),
                float(row.get('atr_pct', 0)),
                float(row.get('volume_ratio', 1)),
                float(row.get('nearest_support', 0)),
                float(row.get('nearest_resistance', 0)),
                patterns,
                float(row['composite_score']),
                float(row['confidence']),
                int(row['direction']),
                float(row.get('rt_momentum', 0)),
                delta_aligned,
                alignment_score,
                signal_emitted,
                rejection_gate,
                float(row.get('entry_price', 0)) if pd.notna(row.get('entry_price')) else None,
                float(row.get('stop_loss', 0)) if pd.notna(row.get('stop_loss')) else None,
                float(row.get('tp1', 0)) if pd.notna(row.get('tp1')) else None,
                float(row.get('tp2', 0)) if pd.notna(row.get('tp2')) else None,
                float(row.get('rr_ratio', 0)) if pd.notna(row.get('rr_ratio')) else None,
                row.get('vol_regime', 'NORMAL')
                )

                migrated_count += 1

            print(f"✅ Signal logs migrated: {migrated_count} records")

        except Exception as e:
            print(f"❌ Failed to migrate signal logs: {e}")

    async def migrate_trade_ledger(self):
        """Migrate trade ledger CSV"""
        print("💰 Migrating trade ledger...")

        trade_ledger_file = self.data_dir / 'trade_ledger.csv'
        if not trade_ledger_file.exists():
            print("⚠️  Trade ledger file not found, skipping")
            return

        try:
            df = pd.read_csv(trade_ledger_file)
            instrument_map = await self.get_instrument_map()

            migrated_count = 0
            for _, row in df.iterrows():
                symbol = row['sym']
                if symbol not in instrument_map:
                    continue

                timestamp = pd.to_datetime(row['timestamp_utc'], utc=True)

                await self.conn.execute("""
                    INSERT INTO trade_ledger (
                        timestamp_utc, event_type, mode, instrument_id,
                        parent_order_id, order_id, direction, qty_delta,
                        qty_open, fill_price, pnl_delta_usd, realized_pnl_usd,
                        status, note
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                timestamp.to_pydatetime(),
                row['event_type'],
                row['mode'],
                instrument_map[symbol],
                int(row['parent_order_id']) if pd.notna(row['parent_order_id']) else None,
                int(row['order_id']) if pd.notna(row['order_id']) else None,
                row['direction'],
                float(row['qty_delta']),
                float(row['qty_open']),
                float(row['fill_price']) if pd.notna(row['fill_price']) else None,
                float(row['pnl_delta_usd']) if pd.notna(row['pnl_delta_usd']) else None,
                float(row['realized_pnl_usd']) if pd.notna(row['realized_pnl_usd']) else None,
                row.get('status', ''),
                row.get('note', '')
                )

                migrated_count += 1

            print(f"✅ Trade ledger migrated: {migrated_count} records")

        except Exception as e:
            print(f"❌ Failed to migrate trade ledger: {e}")

    async def migrate_buffer_delays(self):
        """Migrate buffer calibration data"""
        print("🔧 Migrating buffer calibration data...")

        delay_file = Path('buffer_delays.json')
        diag_file = Path('buffer_calibration_diagnostics.json')

        if not delay_file.exists():
            print("⚠️  Buffer delays file not found, skipping")
            return

        try:
            instrument_map = await self.get_instrument_map()

            # Load delays
            with open(delay_file, 'r') as f:
                delays = json.load(f)

            # Load diagnostics if available
            diagnostics = {}
            if diag_file.exists():
                with open(diag_file, 'r') as f:
                    diagnostics = json.load(f)

            for symbol, delay_min in delays.items():
                if symbol not in instrument_map:
                    continue

                diag = diagnostics.get(symbol, {})
                candidates = diag.get('candidates', []) or []
                delay_mean_val_sharpe = diag.get('delay_mean_val_sharpe', [])
                if isinstance(delay_mean_val_sharpe, dict):
                    if candidates:
                        delay_mean_val_sharpe = [
                            delay_mean_val_sharpe.get(str(c), delay_mean_val_sharpe.get(c, 0.0))
                            for c in candidates
                        ]
                    else:
                        delay_mean_val_sharpe = list(delay_mean_val_sharpe.values())

                await self.conn.execute("""
                    INSERT INTO buffer_calibration (
                        instrument_id, run_timestamp, min_delay_min, max_delay_min,
                        step_min, calibration_window_days, optimal_delay_min,
                        best_sharpe, p_value, is_significant, n_bars, n_windows,
                        candidates, delay_mean_val_sharpe, window_winners
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                """,
                instrument_map[symbol],
                datetime.now(timezone.utc),  # Use current time as migration time
                diag.get('buffer_min_delay_min', 4),
                diag.get('buffer_max_delay_min', 40),
                diag.get('calibration_step_min', 2),
                diag.get('calibration_window_days', 90),
                delay_min,
                diag.get('best_sharpe', 0.0),
                diag.get('p_value', 1.0),
                diag.get('p_value', 1.0) < 0.05,
                diag.get('n_bars', 0),
                diag.get('n_windows', 0),
                candidates,
                delay_mean_val_sharpe,
                diag.get('window_winners', [])
                )

            print("✅ Buffer calibration data migrated")

        except Exception as e:
            print(f"❌ Failed to migrate buffer calibration: {e}")

    async def get_instrument_map(self) -> Dict[str, int]:
        """Get mapping of symbol to instrument_id"""
        rows = await self.conn.fetch("SELECT instrument_id, symbol FROM instruments")
        return {row['symbol']: row['instrument_id'] for row in rows}

    async def run_migration(self):
        """Run complete migration"""
        print("🚀 Starting Citadel Quant Bot Database Migration")
        print("=" * 60)

        try:
            await self.connect()
            await self.create_schema()
            await self.populate_instruments()
            await self.migrate_market_data()
            await self.migrate_signal_logs()
            await self.migrate_trade_ledger()
            await self.migrate_buffer_delays()

            print("=" * 60)
            print("✅ Migration completed successfully!")
            print("\n📋 Next steps:")
            print("1. Update requirements.txt with asyncpg")
            print("2. Update Python code to use database connections")
            print("3. Test the integration")

        except Exception as e:
            print(f"❌ Migration failed: {e}")
            raise
        finally:
            await self.disconnect()


async def main():
    migrator = DatabaseMigrator()
    await migrator.run_migration()


if __name__ == "__main__":
    asyncio.run(main())
