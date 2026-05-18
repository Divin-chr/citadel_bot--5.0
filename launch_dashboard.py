#!/usr/bin/env python3
"""
Citadel Bot Dashboard Launcher

Run this script to start the dashboard with proper Streamlit configuration.
The dashboard will open in your default browser with the trading interface.

Usage:
    python launch_dashboard.py
"""

import os
import subprocess
import sys
from pathlib import Path

def _build_streamlit_command(dashboard_path: Path):
    port = os.environ.get('PORT', '8501')
    cert_file = os.environ.get('CITADEL_DASHBOARD_SSL_CERT', '').strip()
    key_file = os.environ.get('CITADEL_DASHBOARD_SSL_KEY', '').strip()
    enable_https = os.environ.get('CITADEL_DASHBOARD_ENABLE_HTTPS', '').lower() in {'1', 'true', 'yes'}
    use_https = enable_https or (cert_file and key_file)

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_path),
        "--server.port",
        port,
        "--server.headless",
        "true",
        "--server.address",
        "0.0.0.0",
    ]

    if use_https:
        if not cert_file or not key_file:
            raise ValueError(
                "To enable HTTPS, set both CITADEL_DASHBOARD_SSL_CERT and CITADEL_DASHBOARD_SSL_KEY."
            )
        cmd.extend([
            f"--server.sslCertFile={cert_file}",
            f"--server.sslKeyFile={key_file}",
        ])

    return cmd, port, use_https


def main():
    """Launch the Citadel Bot dashboard."""
    dashboard_path = Path(__file__).parent / "citadel_bot" / "dashboard.py"
    
    if not dashboard_path.exists():
        print(f"❌ Error: Dashboard file not found at {dashboard_path}")
        return 1
    
    print("=" * 70)
    print("Starting Citadel Bot Dashboard")
    print("=" * 70)
    print("\nDashboard is starting in your browser...")
    print("   Sign up or log in with your platform user account.")
    print("\nRecommended production environment variables:")
    print("   - CITADEL_SECRET_KEY=generate_with_auth_store")
    print("   - CITADEL_AUTH_DB_PATH=data/auth.db")
    print("   - CITADEL_CONTROL_API_KEY=random_internal_api_key")
    print("\nPress Ctrl+C to stop the dashboard\n")
    print("=" * 70 + "\n")
    
    try:
        cmd, port, use_https = _build_streamlit_command(dashboard_path)
        protocol = "https" if use_https else "http"
        print(f"\n🌐 Open your browser to: {protocol}://localhost:{port}")
        proc = subprocess.run(cmd, cwd=str(Path(__file__).parent))
        return proc.returncode
    except KeyboardInterrupt:
        print("\n\nDashboard stopped.")
        return 0
    except Exception as e:
        print(f"Error starting dashboard: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
