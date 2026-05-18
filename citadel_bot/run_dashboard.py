import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_chrome() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path

    chrome = shutil.which("chrome") or shutil.which("google-chrome")
    if chrome:
        return chrome

    raise FileNotFoundError(
        "Google Chrome not found. Install Chrome or set the BROWSER environment variable manually."
    )


def _build_streamlit_command():
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
        "dashboard.py",
        "--logger.level=error",
        "--server.address=0.0.0.0",
        f"--server.port={port}",
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


def main() -> int:
    # Set environment for Streamlit
    env = os.environ.copy()
    env["STREAMLIT_LOGGER_LEVEL"] = "error"
    env["TZ"] = "UTC"
    
    # Try to find Chrome
    try:
        chrome_path = find_chrome()
        env["BROWSER"] = chrome_path
        auto_open = True
    except FileNotFoundError:
        auto_open = False

    cmd, port, use_https = _build_streamlit_command()
    protocol = "https" if use_https else "http"
    
    print("\n" + "="*70)
    print("🚀 CITADEL BOT DASHBOARD")
    print("="*70)
    print("\n📊 Dashboard is starting...\n")
    print("Sign up or log in with your platform user account.")
    print("\nRecommended production environment variables:")
    print("   - CITADEL_SECRET_KEY=generate_with_auth_store")
    print("   - CITADEL_AUTH_DB_PATH=data/auth.db")
    print("   - CITADEL_CONTROL_API_KEY=random_internal_api_key")
    
    if auto_open:
        print(f"\n🌐 Browser will open automatically using: {chrome_path}")
    else:
        print(f"\n🌐 Open your browser to: {protocol}://localhost:{port}")
    
    print("\n⏹️  Press Ctrl+C to stop the dashboard")
    print("="*70 + "\n")

    proc = subprocess.Popen(cmd, env=env)
    try:
        proc.wait()
        return proc.returncode
    except KeyboardInterrupt:
        print("\n✅ Dashboard stopped.")
        proc.terminate()
        proc.wait()
        return proc.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
