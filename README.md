# MC-EcoHost

A headless Minecraft server management suite with a Python FastAPI backend and modern web dashboard.

## Quick Start

### 1. Install Dependencies

```powershell
pip install -r requirements.txt
```

### 2. Configure Server Path

Edit `Modules/config.py` and update the server directory path:

```python
SERVER_DIR = Path(r"C:\Path\To\Your\Minecraft\Server")
```

### 3. Run the Backend

```powershell
python main.py
```

The API will be available at `http://localhost:8000`

### 4. Access the Dashboard

Open `web/dashboard.html` in your browser, or serve it:

```powershell
# Using Python's built-in server
python -m http.server 8080 --directory web
```

Then navigate to `http://localhost:8080/dashboard.html`

## Authentication

### Default Admin Account

On first run, a default admin account is automatically created:

- **Username**: `admin`
- **Password**: `admin`
- **⚠️ IMPORTANT**: You must change this password on first login!

### Security Setup

1. Login with the default admin credentials
2. You will be prompted to change your password immediately
3. Create additional users with appropriate roles (admin, moderator, viewer, viewer_plus)
4. Configure RCON settings in the dashboard if needed

### Role Permissions

- **Admin**: Full access including server control, settings, user management
- **Moderator**: Server control, player management, viewing logs
- **Viewer Plus**: Can start server and view dashboard/logs
- **Viewer**: Read-only access to dashboard and logs

## Auto-Generated Files

The following files are automatically created on first run (do not commit to git):

- `data/config.json` - Application settings and JWT secret
- `data/auth.db` - User authentication database
- `data/player_stats.json` - Player session statistics
- `data/uptime_stats.json` - Server uptime tracking

## API Endpoints

| Endpoint | Method | Description | Auth Required |
|----------|--------|-------------|---------------|
| `/status` | GET | Get server status (state, players, resources) | Yes |
| `/power/start` | POST | Start the Minecraft server | Moderator+ |
| `/power/stop` | POST | Stop the Minecraft server | Moderator+ |
| `/logs` | GET | Get last 50 log lines | Viewer+ |
| `/ws` | WebSocket | Real-time status updates | Yes |
| `/health` | GET | Health check | No |
| `/auth/login` | POST | Login and get JWT token | No |
| `/auth/register` | POST | Register new user | No |
| `/settings` | GET/POST | Get/update settings | Admin |

## Features

- **Authentication System**: JWT-based auth with role-based access control
- **Asynchronous Process Management**: Start/stop Minecraft server via subprocess
- **Real-time Log Watching**: Monitor `latest.log` for player events
- **Eco-Logic Watchdog**: Auto-shutdown after configurable idle time
- **Resource Monitoring**: CPU and RAM usage tracking
- **WebSocket Updates**: Real-time dashboard updates
- **Player Statistics**: Track player sessions and playtime
- **Power Mode Scheduling**: Automatic performance mode switching
- **RCON Integration**: Remote console control (optional)

## Configuration

### Server Configuration

Edit `Modules/config.py` to customize:

```python
SERVER_DIR = Path(r"C:\Path\To\Server")  # Server directory
RUN_BAT = SERVER_DIR / "run.bat"         # Launch script
LOG_FILE = SERVER_DIR / "logs" / "latest.log"  # Log location
IDLE_TIMEOUT_MINUTES = 5                 # Auto-shutdown timeout
```

### Application Settings

Settings can be configured via the web dashboard or by editing `data/config.json`:

- `auto_shutdown_enabled` - Enable auto-shutdown on idle
- `auto_shutdown_duration` - Idle timeout in seconds
- `auto_start_on_ping` - Auto-start server when pinged
- `power_mode_scheduling_enabled` - Enable scheduled power modes
- `high_performance_start` - Time to switch to high performance
- `high_performance_end` - Time to switch back to balanced
- `ecohost_precision_enabled` - Smart power mode optimization
- `rcon_enabled` - Enable RCON integration
- `rcon_port` - RCON port (default: 25575)
- `rcon_password` - RCON password (change from default!)

## Security Notes

- **Change default passwords immediately** (admin account and RCON)
- The `data/` directory contains sensitive information and is gitignored
- JWT secrets are auto-generated and stored in `data/config.json`
- Rate limiting is enabled for login attempts (5 failed = 15 min lockout)
- Keep your `data/` directory secure and backed up

## Troubleshooting

### Server won't start
- Check that `SERVER_DIR` in `Modules/config.py` points to your Minecraft server
- Ensure `run.bat` exists in your server directory
- Check the logs in the dashboard for error messages

### Authentication issues
- Default credentials: `admin` / `admin`
- If locked out, delete `data/auth.db` and restart to recreate default admin
- Check that your browser accepts cookies for JWT token storage

### Dashboard not loading
- Ensure the backend is running on `http://localhost:8000`
- Check browser console for CORS errors
- Try accessing API endpoints directly to verify backend is working