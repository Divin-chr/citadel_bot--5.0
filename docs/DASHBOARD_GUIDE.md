# Citadel Bot Dashboard

The Citadel Bot Dashboard is a Streamlit-based web interface for managing the trading bot, monitoring positions, and configuring settings.

## Quick Start

### Option 1: Recommended (Auto-launch with Browser)
From the project root:
```bash
python launch_dashboard.py
```

This will:
- Launch the Streamlit server
- Automatically open the dashboard in your default browser
- Display login credentials and helpful information

### Option 2: Manual Streamlit
From the `citadel_bot/` directory:
```bash
streamlit run dashboard.py
```

Then open your browser to: `http://localhost:8501`

### Option 3: Using the Runner Script
```bash
python citadel_bot/run_dashboard.py
```

### Optional: Local HTTPS Launch
To reduce browser "connection not secure" warnings for local dashboard use, provide TLS cert and key files:

```bash
set CITADEL_DASHBOARD_ENABLE_HTTPS=true
set CITADEL_DASHBOARD_SSL_CERT=path\to\localhost-cert.pem
set CITADEL_DASHBOARD_SSL_KEY=path\to\localhost-key.pem
python launch_dashboard.py
```

Then open your browser to `https://localhost:8501`.

If your certificate is self-signed, you may also set:

```bash
set CITADEL_CONTROL_API_VERIFY_SSL=false
```

For a fully secure local deployment, also configure the local control API if you want dashboard control traffic over HTTPS:

```bash
set CITADEL_CONTROL_API_SCHEME=https
set CITADEL_CONTROL_API_SSL_CERT=path\to\control-api-cert.pem
set CITADEL_CONTROL_API_SSL_KEY=path\to\control-api-key.pem
```

## Login

Create a platform user account, then add your MetaApi token and account ID in
Settings. Each user has isolated encrypted credentials, isolated bot controls,
and an isolated trading configuration.

## Production Setup

For production, set deployment-wide secrets through environment variables or
your deployment secret store:

```bash
# Windows (PowerShell)
$env:CITADEL_SECRET_KEY="your_generated_secret_key"
$env:CITADEL_AUTH_DB_PATH="data/auth.db"
$env:CITADEL_CONTROL_API_KEY="random_internal_api_key"

# Windows (Command Prompt)
set CITADEL_SECRET_KEY=your_generated_secret_key
set CITADEL_AUTH_DB_PATH=data/auth.db
set CITADEL_CONTROL_API_KEY=random_internal_api_key

# Linux/macOS
export CITADEL_SECRET_KEY=your_generated_secret_key
export CITADEL_AUTH_DB_PATH=data/auth.db
export CITADEL_CONTROL_API_KEY=random_internal_api_key
```

Generate a secret key with:

```bash
python -c "from citadel_bot.auth_store import AuthenticationStore; print(AuthenticationStore.generate_secret_key())"
```

Use persistent storage for `CITADEL_AUTH_DB_PATH` in production. On ephemeral
hosts, users and encrypted MetaApi credentials will disappear when the instance
is rebuilt.

## Features

- **🔐 Secure Login** — Signup/login with hashed passwords and encrypted MetaApi tokens
- **📊 Account Metrics** — Real-time equity, balance, and P&L
- **🛠️ MT5 Configuration** — Connect/disconnect with broker credentials
- **📈 Instrument Setup** — Select indices, forex, commodities from catalog
- **💱 Trading Mode** — Toggle between paper and live trading
- **📋 Position Viewer** — Open positions and orders tables
- **💰 Trade History** — Ledger with filters by date and instrument
- **⚠️ Risk Monitor** — Real-time risk utilization and suggestions
- **🔄 Auto-Refresh** — Configurable refresh interval

## Troubleshooting

### "ScriptRunContext" Warnings
These warnings occur if you run `python dashboard.py` directly instead of using `streamlit run`. Simply use one of the recommended launch methods above.

### Dashboard Not Opening
1. Check that Streamlit is installed: `pip install streamlit`
2. If using `launch_dashboard.py`, verify Chrome is installed or manually open the printed localhost URL.
3. If you enabled HTTPS, open `https://localhost:8501` instead of `http://localhost:8501`.
4. Check for firewall rules blocking localhost:8501

### Login Issues
- Verify the email and password.
- Check that `CITADEL_SECRET_KEY` is stable between deployments; changing it makes stored MetaApi tokens undecryptable.

## Architecture

- **Framework:** Streamlit (Python web framework)
- **Backend:** Citadel Bot core (asyncio-based)
- **Database:** PostgreSQL (optional, for analytics)
- **MT5 Connection:** MetaTrader 5 API

## Next Steps

1. Configure your MT5 broker credentials in the dashboard
2. Select your trading instruments (indices/forex/commodities)
3. Choose trading mode (paper or live)
4. Monitor positions and risk metrics in real-time
