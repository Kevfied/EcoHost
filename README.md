# MC-EcoHost

A headless Minecraft server management suite with a Python FastAPI backend and modern web dashboard.

## Quick Start

### 1. Install Dependencies

```powershell
pip install -r requirements.txt
```

### 2. Configure Server Path

Edit `main.py` and update the server directory path:

```python
SERVER_DIR = Path(r"C:\Path\To\Your\Minecraft\Server")
```

### 3. Run the Backend

```powershell
python main.py
```

The API will be available at `http://localhost:8000`

### 4. Open the Dashboard

Open `dashboard.html` in your browser, or serve it:

```powershell
# Using Python's built-in server
python -m http.server 8080
```

Then navigate to `http://localhost:8080/dashboard.html`

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Get server status (state, players, resources) |
| `/power/start` | POST | Start the Minecraft server |
| `/power/stop` | POST | Stop the Minecraft server |
| `/logs` | GET | Get last 50 log lines |
| `/ws` | WebSocket | Real-time status updates |
| `/health` | GET | Health check |

## Features

- **Asynchronous Process Management**: Start/stop Minecraft server via subprocess
- **Real-time Log Watching**: Monitor `latest.log` for player events
- **Eco-Logic Watchdog**: Auto-shutdown after 5 minutes of idle time
- **Resource Monitoring**: CPU and RAM usage tracking
- **WebSocket Updates**: Real-time dashboard updates

## Configuration

Edit `main.py` to customize:

```python
SERVER_DIR = Path(r"C:\Path\To\Server")  # Server directory
RUN_BAT = SERVER_DIR / "run.bat"         # Launch script
LOG_FILE = SERVER_DIR / "logs" / "latest.log"  # Log location
IDLE_TIMEOUT_MINUTES = 5                 # Auto-shutdown timeout
```