"""
Data models, enums, and response classes for MC-EcoHost.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Global variables for thread-safe access
start_time: float = 0.0
stop_initiated_time: Optional[float] = None
server_hung: bool = False

# =============================================================================
# Enums
# =============================================================================


class ServerState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"


# Thread-safe state management
state_lock = threading.Lock()

# Thread-safe global variable locks
time_lock = threading.Lock()
shutdown_lock = threading.Lock()

def get_start_time() -> float:
    """Thread-safe get start_time."""
    with time_lock:
        return start_time

def set_start_time(value: float):
    """Thread-safe set start_time."""
    with time_lock:
        global start_time
        start_time = value

def get_stop_initiated_time() -> Optional[float]:
    """Thread-safe get stop_initiated_time."""
    with shutdown_lock:
        return stop_initiated_time

def set_stop_initiated_time(value: Optional[float]):
    """Thread-safe set stop_initiated_time."""
    with shutdown_lock:
        global stop_initiated_time
        stop_initiated_time = value

def get_server_hung() -> bool:
    """Thread-safe get server_hung."""
    with shutdown_lock:
        return server_hung

def set_server_hung(value: bool):
    """Thread-safe set server_hung."""
    with shutdown_lock:
        global server_hung
        server_hung = value

def is_valid_transition(from_state: ServerState, to_state: ServerState) -> bool:
    """Check if state transition is valid."""
    valid_transitions = {
        ServerState.IDLE: [ServerState.STARTING, ServerState.ACTIVE],  # Allow direct IDLE -> ACTIVE for auto-start
        ServerState.STARTING: [ServerState.ACTIVE, ServerState.IDLE],
        ServerState.ACTIVE: [ServerState.STOPPING, ServerState.IDLE],
        ServerState.STOPPING: [ServerState.ACTIVE, ServerState.IDLE],
    }
    return to_state in valid_transitions.get(from_state, [])

def set_server_state(new_state: ServerState, reason: str = "") -> bool:
    """Thread-safe state transition with validation."""
    global server_status
    
    with state_lock:
        old_state = server_status.state
        
        if not is_valid_transition(old_state, new_state):
            logger.warning(f"[State] Invalid transition: {old_state} -> {new_state} ({reason})")
            return False
            
        server_status.state = new_state
        logger.info(f"[State] {old_state} -> {new_state} ({reason})")
        return True


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PlayerInfo:
    username: str
    joined_at: float = field(default_factory=time.time)


@dataclass
class ServerStatus:
    _state: ServerState = field(default=ServerState.IDLE, init=False)
    players: list[PlayerInfo] = field(default_factory=list)
    cpu_usage: float = 0.0
    ram_usage: float = 0.0
    uptime: float = 0.0
    last_player_time: float = field(default_factory=time.time)
    
    @property
    def state(self) -> ServerState:
        return self._state
    
    @state.setter
    def state(self, value: ServerState):
        # Always clear players when going to IDLE state
        if value == ServerState.IDLE and self._state != ServerState.IDLE:
            self.players.clear()
        self._state = value


@dataclass
class Settings:
    auto_shutdown_enabled: bool = True
    auto_shutdown_duration: int = 300  # seconds (5 minutes default)
    auto_start_on_ping: bool = True
    power_mode_scheduling_enabled: bool = True
    high_performance_start: str = "09:00"  # 9 AM
    high_performance_end: str = "22:00"  # 10 PM
    current_power_mode: str = "balanced"  # balanced, high_performance, power_saver, ultimate_performance
    ecohost_precision_enabled: bool = True  # EcoHost Precision smart power mode
    
    # RCON Settings
    rcon_enabled: bool = False
    rcon_port: int = 25575
    rcon_password: str = "Kersh159357"
    
    # Maintenance Mode Settings
    maintenance_mode: bool = False
    maintenance_ips: list[str] = field(default_factory=list)  # IPs allowed during maintenance


# =============================================================================
# Global State Instances
# =============================================================================

server_status = ServerStatus()
app_settings = Settings()
log_lines: list[str] = []
log_lock = threading.Lock()
ecohost_logs: list[str] = []
ecohost_logs_lock = threading.Lock()
start_time: float = 0
log_watcher_running = False
java_process_pid: Optional[int] = None


# =============================================================================
# Settings Management
# =============================================================================

from .config import CONFIG_FILE
import logging

logger = logging.getLogger(__name__)


def load_settings_from_file() -> None:
    """Load settings from config.json file."""
    global app_settings
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                # Merge with defaults to handle missing fields from old config files
                default_settings = asdict(Settings())
                # Only update with known settings fields (filter out jwt_secret, etc.)
                known_fields = set(default_settings.keys())
                filtered_data = {k: v for k, v in data.items() if k in known_fields}
                default_settings.update(filtered_data)
                
                # Update the existing app_settings object instead of creating a new one
                for key, value in default_settings.items():
                    if hasattr(app_settings, key):
                        setattr(app_settings, key, value)
                
                logger.info(f"[Settings] Loaded settings from {CONFIG_FILE}")
                logger.info(f"[Settings] EcoHost Precision: {app_settings.ecohost_precision_enabled}")
                logger.info(f"[Settings] Power Mode Scheduling: {app_settings.power_mode_scheduling_enabled}")
                logger.info(f"[Settings] Auto Shutdown: {app_settings.auto_shutdown_enabled}")
                logger.info(f"[Settings] Auto Shutdown Duration: {app_settings.auto_shutdown_duration}")
        else:
            logger.warning(f"[Settings] No config file found at {CONFIG_FILE}, using defaults")
    except Exception as e:
        logger.error(f"[Settings] Failed to load settings: {e}")
        logger.exception("[Settings] Full traceback:")


def save_settings_to_file() -> None:
    """Save current settings to config.json file."""
    global app_settings
    try:
        settings_dict = asdict(app_settings)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(settings_dict, f, indent=4)
        logger.info(f"[Settings] Saved settings to {CONFIG_FILE}: {settings_dict}")
    except Exception as e:
        logger.error(f"[Settings] Failed to save settings: {e}")


# =============================================================================
# Response Models
# =============================================================================


class PowerResponse(BaseModel):
    success: bool
    message: str
    state: ServerState


class StatusResponse(BaseModel):
    state: ServerState
    players: list[str]
    cpu_usage: float
    ram_usage: float
    uptime: float
    player_count: int
    max_players: int
    network: dict
    console_link: str
    server_hung: bool  # True if stop sent but server still running after 60s
    countdown_active: bool = False  # True if auto-shutdown countdown is running
    countdown_remaining: int = 0  # Seconds remaining until shutdown
    countdown_total: int = 0  # Total countdown duration in seconds
    avg_tick: float = 0.0  # Average tick time in milliseconds
    maintenance_mode: bool = False  # Whether maintenance mode is enabled


class LogsResponse(BaseModel):
    logs: list[str]
    count: int


class ConsoleStatusResponse(BaseModel):
    """Extended status with console window info."""
    window_title: str
    window_found: bool
    pywinauto_available: bool


class SettingsResponse(BaseModel):
    """Settings response model."""
    auto_shutdown_enabled: bool
    auto_shutdown_duration: int
    auto_start_on_ping: bool
    power_mode_scheduling_enabled: bool
    high_performance_start: str
    high_performance_end: str
    current_power_mode: str
    ecohost_precision_enabled: bool
    rcon_enabled: bool
    rcon_port: int
    rcon_password: str


class MetricsHistoryResponse(BaseModel):
    success: bool
    metrics: list = Field(default_factory=list)
    message: str = ""


class PlayerStatsResponse(BaseModel):
    success: bool
    players: list = Field(default_factory=list)
    online_count: int = 0
    total_count: int = 0
    message: str = ""


class PlayerDataResponse(BaseModel):
    success: bool
    username: str
    data: dict = Field(default_factory=dict)
    message: Optional[str] = ""
