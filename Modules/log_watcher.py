"""
Log Watcher Service - Monitors Minecraft server logs for player events.
"""

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Optional

import logging

from .config import LOG_FILE, LOG_BUFFER_SIZE, STATUS_CHECK_INTERVAL, MINECRAFT_PORT, GRACEFUL_SHUTDOWN_TIMEOUT, stop_initiated_time, server_hung, empty_server_countdown, countdown_active, ecohost_precision_last_mode_change, ecohost_precision_cooldown, ecohost_precision_empty_since
from .models import PlayerInfo, ServerState, server_status, log_lines, log_lock, app_settings
from .server_control import console_controller, is_minecraft_process_running

logger = logging.getLogger(__name__)


def is_port_open(host: str, port: int) -> bool:
    """Check if a port is open."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, port))
            return result == 0
    except Exception:
        return False


def parse_player_joined(line: str) -> Optional[str]:
    """Parse player join from log line."""
    patterns = [
        r"<([^>]+)> joined the game",
        r"(\S+) joined the game",
        r"joined the game.*?: (\S+)",
        r"(\S+) has joined",
    ]
    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            username = match.group(1)
            if username and username != "0":
                logger.debug(f"[PlayerParser] Matched player join: {username} from line: {line}")
                return username
    return None


def parse_player_left(line: str) -> Optional[str]:
    """Parse player leave from log line."""
    patterns = [
        r"<([^>]+)> left the game",
        r"([\w.]+) left the game",
    ]
    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            username = match.group(1)
            if username and username != "0":
                logger.debug(f"[PlayerParser] Matched player left: {username} from line: {line[:100]}")
                return username
    return None


def scan_for_existing_players():
    """Scan the log file for existing players when server becomes ACTIVE."""
    global server_status
    
    if not LOG_FILE.exists():
        return
    
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        
        joined_players = set()
        left_players = set()
        
        for line in all_lines:
            line = line.strip()
            if not line:
                continue
            
            if player := parse_player_joined(line):
                joined_players.add(player)
            
            if player := parse_player_left(line):
                left_players.add(player)
        
        current_players = joined_players - left_players
        
        with log_lock:
            server_status.players.clear()
            for username in current_players:
                server_status.players.append(PlayerInfo(username=username))
        
        player_count = len(server_status.players)
        if player_count > 0:
            logger.info(f"[GhostConsole] Scanned log file - found {player_count} existing players: {list(current_players)}")
        
    except Exception as e:
        logger.error(f"[GhostConsole] Failed to scan for existing players: {e}")


# Placeholder functions for player session tracking (will be in player_sessions.py)
def record_player_join(username: str):
    """Record player session start."""
    pass


def record_player_leave(username: str):
    """Record player session end."""
    pass


def set_windows_power_mode(mode: str) -> bool:
    """Set Windows power mode using powercfg command."""
    try:
        if mode == "ultimate_performance":
            guid = "e9a42b02-d5df-448d-aa00-03f14749eb61"
        elif mode == "high_performance":
            guid = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
        elif mode == "power_saver":
            guid = "a1841308-3541-4fab-bc81-f71556f20b4a"
        else:
            guid = "381b4222-f694-41f0-9685-ff5bb260df2e"
        
        import subprocess
        result = subprocess.run(
            ["powercfg", "/setactive", guid],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info(f"[PowerMode] Set power mode to {mode}")
        return True
    except subprocess.CalledProcessError as e:
        if mode == "ultimate_performance" and ("Not Supported" in str(e.stderr) or "does not exist" in str(e.stderr).lower()):
            logger.warning("[PowerMode] Ultimate Performance plan not available, falling back to High Performance")
            return set_windows_power_mode("high_performance")
        logger.error(f"[PowerMode] Failed to set power mode: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"[PowerMode] Error setting power mode: {e}")
        return False


def get_current_power_mode() -> str:
    """Get current Windows power mode."""
    try:
        import subprocess
        result = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True,
            text=True,
            check=True
        )
        output = result.stdout
        if "e9a42b02-d5df-448d-aa00-03f14749eb61" in output:
            return "ultimate_performance"
        elif "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c" in output:
            return "high_performance"
        elif "a1841308-3541-4fab-bc81-f71556f20b4a" in output:
            return "power_saver"
        else:
            return "balanced"
    except Exception as e:
        logger.error(f"[PowerMode] Failed to get power mode: {e}")
        return "balanced"


def is_work_hours() -> bool:
    """Check if current time is within work hours (9 AM - 9 PM)."""
    current_hour = time.localtime().tm_hour
    return 9 <= current_hour < 21


def calculate_target_power_mode(player_count: int) -> str:
    """Calculate target power mode based on player count and work hours."""
    is_work_time = is_work_hours()
    
    # Check if both EcoHost Precision and Power Mode Scheduling are enabled
    if app_settings.ecohost_precision_enabled and app_settings.power_mode_scheduling_enabled:
        # Combined mode: respect work hours as ceiling
        if is_work_time:
            # Work hours: full range available
            if player_count <= 3:
                return "power_saver"
            elif player_count <= 5:
                return "balanced"
            elif player_count <= 6:
                return "high_performance"
            else:
                return "ultimate_performance"
        else:
            # Non-work hours: capped at balanced
            if player_count <= 3:
                return "power_saver"
            else:
                return "balanced"
    elif app_settings.ecohost_precision_enabled:
        # EcoHost Precision standalone: ignore work hours, full range always available
        if player_count <= 3:
            return "power_saver"
        elif player_count <= 5:
            return "balanced"
        elif player_count <= 6:
            return "high_performance"
        else:
            return "ultimate_performance"
    else:
        # Neither enabled, shouldn't reach here but return balanced as fallback
        return "balanced"


def update_power_mode_scheduling():
    """Update power mode based on time schedule (standalone Power Mode Scheduling)."""
    if not app_settings.power_mode_scheduling_enabled or app_settings.ecohost_precision_enabled:
        # Don't run if EcoHost Precision is also enabled (combined mode handles it)
        return
    
    global ecohost_precision_last_mode_change
    current_time = time.time()
    
    # Check cooldown period
    if (ecohost_precision_last_mode_change and 
        current_time - ecohost_precision_last_mode_change < ecohost_precision_cooldown):
        return
    
    is_work_time = is_work_hours()
    current_mode = get_current_power_mode()
    
    # Power Mode Scheduling standalone logic
    if is_work_time:
        target_mode = "high_performance"
    else:
        target_mode = "power_saver"
    
    # Only update if mode needs to change
    if current_mode != target_mode:
        logger.info(f"[PowerModeScheduling] Work hours: {is_work_time}")
        logger.info(f"[PowerModeScheduling] Switching {current_mode} -> {target_mode}")
        
        if set_windows_power_mode(target_mode):
            ecohost_precision_last_mode_change = current_time
            app_settings.current_power_mode = target_mode


def update_ecohost_precision_mode():
    """Update EcoHost Precision mode based on server state and player count."""
    if not app_settings.ecohost_precision_enabled:
        return
    
    global ecohost_precision_last_mode_change, ecohost_precision_empty_since
    current_time = time.time()
    
    # Check cooldown period
    if (ecohost_precision_last_mode_change and 
        current_time - ecohost_precision_last_mode_change < ecohost_precision_cooldown):
        return
    
    player_count = len(server_status.players)
    target_mode = calculate_target_power_mode(player_count)
    current_mode = get_current_power_mode()
    
    # Only update if mode needs to change
    if current_mode != target_mode:
        logger.info(f"[EcoHostPrecision] Player count: {player_count}, Work hours: {is_work_hours()}")
        logger.info(f"[EcoHostPrecision] Switching {current_mode} -> {target_mode}")
        
        if set_windows_power_mode(target_mode):
            ecohost_precision_last_mode_change = current_time
            app_settings.current_power_mode = target_mode
            
            # Update empty server tracking
            if player_count == 0:
                if ecohost_precision_empty_since is None:
                    ecohost_precision_empty_since = current_time
            else:
                ecohost_precision_empty_since = None


def log_watcher():
    """Background thread that monitors server status and log file."""
    global server_status, log_lines, log_watcher_running, stop_initiated_time, server_hung
    global empty_server_countdown, countdown_active

    log_watcher_running = True
    last_position = 0
    last_window_check = 0
    java_was_running = False
    last_countdown_log = 0

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger.info("[GhostConsole] Status monitor started")

    while log_watcher_running:
        try:
            current_time = time.time()
            
            if current_time - last_window_check >= STATUS_CHECK_INTERVAL:
                last_window_check = current_time
                
                window_exists = console_controller.is_console_running()
                java_running = is_minecraft_process_running()
                port_open = is_port_open("127.0.0.1", MINECRAFT_PORT)
                
                if java_was_running and not java_running and window_exists:
                    logger.info("[GhostConsole] Java stopped. Auto-closing CMD window...")
                    try:
                        window = console_controller.find_console_window()
                        if window:
                            window.close()
                            logger.info("[GhostConsole] CMD window auto-closed")
                    except Exception as e:
                        logger.warning(f"[GhostConsole] Could not auto-close CMD: {e}")
                
                java_was_running = java_running
                
                if server_status.state == ServerState.STOPPING:
                    if not window_exists and not java_running:
                        server_status.state = ServerState.IDLE
                        server_status.players.clear()
                        stop_initiated_time = None
                        server_hung = False
                        logger.info("[GhostConsole] Stop complete - state changed to IDLE")
                elif server_status.state != ServerState.STOPPING:
                    if window_exists or java_running:
                        if port_open:
                            if server_status.state != ServerState.ACTIVE:
                                server_status.state = ServerState.ACTIVE
                                scan_for_existing_players()
                        else:
                            server_status.state = ServerState.STARTING
                    else:
                        if server_status.state != ServerState.IDLE:
                            server_status.state = ServerState.IDLE
                            server_status.players.clear()
                            stop_initiated_time = None
                            server_hung = False
                
                if stop_initiated_time and not server_hung:
                    elapsed = current_time - stop_initiated_time
                    if elapsed > GRACEFUL_SHUTDOWN_TIMEOUT:
                        logger.warning("[GhostConsole] Server appears hung!")
                        server_hung = True

            if LOG_FILE.exists():
                current_size = LOG_FILE.stat().st_size

                if current_size < last_position:
                    last_position = 0

                if current_size > last_position:
                    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                        f.seek(last_position)
                        new_lines = f.readlines()
                        last_position = f.tell()

                    for line in new_lines:
                        line = line.strip()
                        if not line:
                            continue

                        with log_lock:
                            log_lines.append(line)
                            if len(log_lines) > LOG_BUFFER_SIZE:
                                log_lines = log_lines[-LOG_BUFFER_SIZE:]
                        
                        if player := parse_player_joined(line):
                            if not any(p.username == player for p in server_status.players):
                                server_status.players.append(PlayerInfo(username=player))
                                logger.info(f"Player joined: {player}")
                                record_player_join(player)
                                update_ecohost_precision_mode()
                        
                        if player := parse_player_left(line):
                            server_status.players = [
                                p for p in server_status.players if p.username != player
                            ]
                            logger.info(f"Player left: {player}")
                            record_player_leave(player)
                            update_ecohost_precision_mode()
            
            # Update power modes based on schedule and player count
            update_ecohost_precision_mode()
            update_power_mode_scheduling()
            
            if server_status.state == ServerState.ACTIVE and not server_hung and app_settings.auto_shutdown_enabled:
                if len(server_status.players) == 0:
                    if not countdown_active:
                        empty_server_countdown = current_time
                        countdown_active = True
                        logger.info(f"[SmartEnergy] Server empty - starting {app_settings.auto_shutdown_duration}s countdown")
                    else:
                        elapsed = current_time - empty_server_countdown
                        remaining = app_settings.auto_shutdown_duration - elapsed

                        if current_time - last_countdown_log >= 2 and remaining > 0:
                            logger.info(f"[SmartEnergy] Auto-shutdown in {int(remaining)}s...")
                            last_countdown_log = current_time

                        if elapsed >= app_settings.auto_shutdown_duration:
                            logger.info("[SmartEnergy] Countdown expired - auto-shutting down")
                            countdown_active = False
                            empty_server_countdown = None
                            server_status.state = ServerState.STOPPING
                            try:
                                from .commands import stop_server
                                stop_server()
                            except Exception as e:
                                logger.error(f"[SmartEnergy] Auto-shutdown failed: {e}")
                else:
                    if countdown_active:
                        logger.info("[SmartEnergy] Player joined - countdown cancelled")
                        countdown_active = False
                        empty_server_countdown = None
                        last_countdown_log = 0

            if server_status.state == ServerState.STOPPING and len(server_status.players) > 0:
                logger.info("[SmartEnergy] Player joined during shutdown - cancelling shutdown and reverting to ACTIVE")
                server_status.state = ServerState.ACTIVE
                stop_initiated_time = None
                server_hung = False

            time.sleep(1)

        except Exception as e:
            logger.error(f"[GhostConsole] Status monitor error: {e}")
            time.sleep(1)

    logger.info("[GhostConsole] Status monitor stopped")


log_watcher_running = False


def start_log_watcher():
    """Start the log watcher in a background thread."""
    global log_watcher_running
    log_watcher_running = True
    watcher_thread = threading.Thread(target=log_watcher, daemon=True)
    watcher_thread.start()
    logger.info("Log watcher thread started")
