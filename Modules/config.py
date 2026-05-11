"""
Configuration and global state for MC-EcoHost.
"""

from pathlib import Path
from typing import Optional

# =============================================================================
# Paths
# =============================================================================

SERVER_DIR = Path(r"C:\Users\Kevin\AppData\Roaming\.minecraft")
RUN_BAT = SERVER_DIR / "run.bat"
LOG_FILE = SERVER_DIR / "logs" / "latest.log"
CONSOLE_WINDOW_TITLE = "MC_SERVER_CORE"
CONFIG_FILE = Path(__file__).parent.parent / "data" / "config.json"
BASE_DIR = Path(__file__).parent.parent

# =============================================================================
# Server Configuration
# =============================================================================

IDLE_TIMEOUT_MINUTES = 5
LOG_BUFFER_SIZE = 500
MAX_PLAYERS = 20
MINECRAFT_PORT = 25565
GRACEFUL_SHUTDOWN_TIMEOUT = 60  # seconds to wait before force kill
STATUS_CHECK_INTERVAL = 5  # seconds between window existence checks

# =============================================================================
# Smart Energy Saving Settings
# =============================================================================

IDLE_COUNTDOWN_SECONDS = 10  # 10 seconds for testing (use 180 for 3 minutes in production)
AUTO_START_ON_PING = True  # Auto-start server when someone tries to connect

# =============================================================================
# Global State
# =============================================================================

# State for hung server detection
stop_initiated_time: Optional[float] = None
server_hung = False

# Smart Energy Saving State
empty_server_countdown: Optional[float] = None  # Timestamp when countdown started
countdown_active = False
ping_listener_running = False

# EcoHost Precision State
ecohost_precision_last_mode_change: Optional[float] = None  # Timestamp of last mode change
ecohost_precision_cooldown = 10  # seconds between mode switches (reduced from 30)
ecohost_precision_empty_since: Optional[float] = None  # Timestamp when server became empty

# Log buffer
ECOHOST_LOG_BUFFER_SIZE = 200

# =============================================================================
# Backup Settings
# =============================================================================

BACKUP_ENABLED = True
BACKUP_AUTO_ENABLED = False
BACKUP_DURATION_HOURS = 24
BACKUP_DURATION_DAYS = 0
BACKUP_MAX_COUNT = 10
BACKUP_AUTO_DELETE_ENABLED = True
BACKUP_LAST_RUN = None
