"""
Data models, enums, and response classes for MC-EcoHost.
"""

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

# =============================================================================
# Enums
# =============================================================================


class ServerState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PlayerInfo:
    username: str
    joined_at: float = field(default_factory=time.time)


@dataclass
class ServerStatus:
    state: ServerState = ServerState.IDLE
    players: list[PlayerInfo] = field(default_factory=list)
    cpu_usage: float = 0.0
    ram_usage: float = 0.0
    uptime: float = 0.0
    last_player_time: float = field(default_factory=time.time)


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
    print("[Settings] load_settings_from_file() called!")
    try:
        print(f"[Settings] Looking for config at: {CONFIG_FILE}")
        logger.info(f"[Settings] Looking for config at: {CONFIG_FILE}")
        print(f"[Settings] Config file exists: {CONFIG_FILE.exists()}")
        logger.info(f"[Settings] Config file exists: {CONFIG_FILE.exists()}")
        
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                logger.info(f"[Settings] Raw config data: {data}")
                # Merge with defaults to handle missing fields from old config files
                default_settings = asdict(Settings())
                print(f"[Settings] Default settings: {default_settings}")
                logger.info(f"[Settings] Default settings: {default_settings}")
                # Only update with known settings fields (filter out jwt_secret, etc.)
                known_fields = set(default_settings.keys())
                filtered_data = {k: v for k, v in data.items() if k in known_fields}
                print(f"[Settings] Filtered config data: {filtered_data}")
                logger.info(f"[Settings] Filtered config data: {filtered_data}")
                default_settings.update(filtered_data)
                print(f"[Settings] Merged settings: {default_settings}")
                logger.info(f"[Settings] Merged settings: {default_settings}")
                
                # Update the existing app_settings object instead of creating a new one
                for key, value in default_settings.items():
                    if hasattr(app_settings, key):
                        setattr(app_settings, key, value)
                
                print(f"[Settings] Updated app_settings object: {app_settings}")
                logger.info(f"[Settings] Loaded settings from {CONFIG_FILE}")
                logger.info(f"[Settings] EcoHost Precision: {app_settings.ecohost_precision_enabled}")
                logger.info(f"[Settings] Power Mode Scheduling: {app_settings.power_mode_scheduling_enabled}")
                logger.info(f"[Settings] Auto Shutdown: {app_settings.auto_shutdown_enabled}")
                logger.info(f"[Settings] Auto Shutdown Duration: {app_settings.auto_shutdown_duration}")
                logger.info(f"[Settings] app_settings object: {app_settings}")
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
    message: str = ""
