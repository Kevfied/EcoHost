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

from .config import LOG_FILE, LOG_BUFFER_SIZE, STATUS_CHECK_INTERVAL, MINECRAFT_PORT, GRACEFUL_SHUTDOWN_TIMEOUT, empty_server_countdown, countdown_active, ecohost_precision_last_mode_change, ecohost_precision_cooldown, ecohost_precision_empty_since
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


def is_minecraft_login_handshake(data: bytes) -> bool:
    """Check if the received data matches Minecraft login handshake packet format."""
    if len(data) < 3:
        return False
    
    try:
        # Minecraft handshake packet structure:
        # - VarInt packet length (1-3 bytes)
        # - VarInt packet ID (0x00 for handshake)
        # - VarInt protocol version
        # - String server address
        # - Unsigned short port
        # - VarInt next state (1 for status, 2 for login)
        
        # Basic check: first byte should be reasonable packet length
        if data[0] < 1 or data[0] > 50:  # Reasonable packet length
            return False
            
        if data[1] != 0x00:  # Must be handshake packet
            return False
            
        # Read protocol version (VarInt after packet ID)
        offset = 2  # Skip length and packet ID
        protocol_version = 0
        for i in range(5):  # Max 5 bytes for VarInt
            if offset + i >= len(data):
                break
            byte = data[offset + i]
            protocol_version |= (byte & 0x7F) << (7 * i)
            if not (byte & 0x80):  # Continuation bit
                offset += i + 1
                break
        else:
            return False
        
        # Check if protocol version is reasonable for Minecraft
        if protocol_version < 47 or protocol_version > 1000:
            return False
        
        # Skip server address (string) and port (unsigned short)
        # Read string length
        if offset >= len(data):
            return False
        string_length = 0
        for i in range(3):  # Max 3 bytes for string length
            if offset + i >= len(data):
                break
            byte = data[offset + i]
            string_length |= (byte & 0x7F) << (7 * i)
            if not (byte & 0x80):
                offset += i + 1
                break
        else:
            return False
        
        # Skip string content
        offset += string_length
        
        # Skip port (2 bytes)
        offset += 2
        
        # Read next state (VarInt) - this is what we care about!
        if offset >= len(data):
            return False
        
        next_state = 0
        for i in range(2):  # Next state is usually 1 byte
            if offset + i >= len(data):
                break
            byte = data[offset + i]
            next_state |= (byte & 0x7F) << (7 * i)
            if not (byte & 0x80):
                break
        
        # Only trigger on login state (2), not status state (1)
        return next_state == 2
        
    except Exception:
        return False


def ping_listener():
    """Background thread that listens for Minecraft client connections to auto-start server."""
    global ping_listener_running, server_status
    
    import socket
    import threading
    from .models import app_settings
    
    ping_listener_running = True
    logger.info("[PingListener] Starting Minecraft client listener on port 25565")
    
    while ping_listener_running:
        try:
            if app_settings.auto_start_on_ping and server_status.state == ServerState.IDLE:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1)
                        s.bind(("0.0.0.0", MINECRAFT_PORT))
                        s.listen(1)
                        logger.info("[PingListener] Waiting for Minecraft client connection...")
                        
                        # Wait for a connection attempt
                        conn, addr = s.accept()
                        
                        # Filter out localhost connections to prevent self-pings
                        if addr[0] in ['127.0.0.1', '::1', 'localhost']:
                            logger.debug(f"[PingListener] Ignoring localhost connection from {addr[0]}")
                            conn.close()
                            time.sleep(1)
                            continue
                        
                        logger.info(f"[PingListener] Connection attempt from {addr[0]} - checking for Minecraft login")
                        
                        # Set timeout for reading handshake data
                        conn.settimeout(2.0)
                        
                        try:
                            # Read more bytes to get full handshake packet for login detection
                            data = conn.recv(64)  # Read enough for complete handshake packet
                            
                            if is_minecraft_login_handshake(data):
                                logger.info(f"[PingListener] Minecraft login attempt detected from {addr[0]}")
                                conn.close()
                                
                                # Check maintenance mode and IP whitelist
                                if app_settings.maintenance_mode:
                                    # Check if IP is in maintenance whitelist
                                    if addr[0] not in app_settings.maintenance_ips:
                                        logger.warning(f"[PingListener] Rejected login from {addr[0]} - maintenance mode active and IP not whitelisted")
                                        continue
                                    else:
                                        logger.info(f"[PingListener] Allowed login from {addr[0]} - maintenance mode but IP whitelisted")
                                
                                # Start the server
                                logger.info("[PingListener] Auto-starting server for Minecraft login")
                                from .server_control import console_controller
                                import asyncio
                                
                                # Start the server in a new thread to avoid blocking
                                def start_server_thread():
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    loop.run_until_complete(console_controller.start())
                                    loop.close()
                                
                                start_thread = threading.Thread(target=start_server_thread, daemon=True)
                                start_thread.start()
                                
                                # Wait a bit before listening again
                                time.sleep(5)
                            else:
                                logger.debug(f"[PingListener] Non-login connection from {addr[0]} - ignoring (status ping or other)")
                                conn.close()
                                
                        except socket.timeout:
                            logger.debug(f"[PingListener] Timeout reading from {addr[0]} - not a Minecraft client")
                            conn.close()
                        except Exception as e:
                            logger.debug(f"[PingListener] Error reading from {addr[0]}: {e}")
                            conn.close()
                        
                except socket.timeout:
                    # No connection, continue listening
                    continue
                except OSError as e:
                    if e.errno == 10048:  # Address already in use
                        # Port is already in use, server might be running
                        time.sleep(1)
                    else:
                        logger.error(f"[PingListener] Socket error: {e}")
                        time.sleep(1)
                except Exception as e:
                    logger.error(f"[PingListener] Error: {e}")
                    time.sleep(1)
            else:
                # Auto-start disabled or server not idle, just wait
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"[PingListener] Listener error: {e}")
            time.sleep(1)
    
    logger.info("[PingListener] Minecraft client listener stopped")


def start_ping_listener():
    """Start the ping listener in a background thread."""
    global ping_listener_running
    ping_listener_running = True
    listener_thread = threading.Thread(target=ping_listener, daemon=True)
    listener_thread.start()
    logger.info("Ping listener thread started")


def parse_player_joined(line: str) -> Optional[str]:
    """Parse player join from log line."""
    patterns = [
        r"<([^>]+)> joined the game",
        r"(\S+) joined the game",
        r"joined the game.*?: (\S+)",
        r"(\S+) has joined",
        r"UUID of player (\S+) is",
        r"(\S+)\[.*?\] logged in",
        r"(\S+) made the connection",
        r"(\S+) joined",
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


# Import the real session tracking functions
from .player_sessions import record_player_join as real_record_player_join, record_player_leave as real_record_player_leave

def record_player_join(username: str):
    """Record player session start."""
    real_record_player_join(username)


def record_player_leave(username: str):
    """Record player session end."""
    real_record_player_leave(username)


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
    """Background thread that monitors server logs and updates player list."""
    global server_status, log_watcher_running, last_position
    global empty_server_countdown, countdown_active, last_countdown_log, last_window_check, java_was_running, log_lines
    
    log_watcher_running = True
    logger.info("Log watcher started")
    
    # Reset position and clear log buffer when starting to ensure we get fresh logs
    last_position = 0
    last_window_check = 0
    java_was_running = False
    last_countdown_log = 0
    with log_lock:
        log_lines.clear()
    
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
                        from .models import set_stop_initiated_time, set_server_hung
                        set_stop_initiated_time(None)
                        set_server_hung(False)
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
                            from .models import set_stop_initiated_time, set_server_hung
                            set_stop_initiated_time(None)
                            set_server_hung(False)
                
                from .models import get_server_hung, get_stop_initiated_time
                if get_stop_initiated_time() and not get_server_hung():
                    elapsed = current_time - get_stop_initiated_time()
                    if elapsed > GRACEFUL_SHUTDOWN_TIMEOUT:
                        logger.warning("[GhostConsole] Server appears hung!")
                        from .models import set_server_hung
                        set_server_hung(True)

            if LOG_FILE.exists():
                current_size = LOG_FILE.stat().st_size

                if current_size < last_position:
                    logger.info(f"[GhostConsole] Log file rotated, resetting position (was {last_position}, now {current_size})")
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
                                # Remove oldest lines from the beginning (in-place)
                                del log_lines[:-LOG_BUFFER_SIZE]
                        
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
            
            from .models import get_server_hung, get_start_time
            if server_status.state == ServerState.ACTIVE and not get_server_hung() and app_settings.auto_shutdown_enabled:
                # Don't start auto-shutdown if server just started (give it time to load)
                server_uptime = current_time - get_start_time()
                startup_grace_period = 30  # 30 seconds grace period after server start
                
                if len(server_status.players) == 0 and server_uptime > startup_grace_period:
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
                            
                            # CRITICAL: Send RCON stop command WHILE server is still ACTIVE
                            # RCON becomes unavailable once server state changes to STOPPING
                            try:
                                from .commands import execute_command
                                logger.info("[SmartEnergy] Sending stop command via RCON (server still ACTIVE)")
                                rcon_success = execute_command("stop")
                                if rcon_success:
                                    logger.info("[SmartEnergy] Stop command sent successfully via RCON")
                                else:
                                    logger.warning("[SmartEnergy] RCON failed, will use fallback method")
                            except Exception as e:
                                logger.error(f"[SmartEnergy] RCON error: {e}")
                            
                            # Now change server state after attempting RCON
                            from .models import set_server_state
                            set_server_state(ServerState.STOPPING, "auto-shutdown countdown expired")
                            
                            # Start the fallback stop process (will only be used if RCON failed)
                            try:
                                from .commands import stop_server
                                import asyncio
                                import threading
                                
                                # Run async stop_server in a new event loop
                                def stop_server_thread():
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    try:
                                        loop.run_until_complete(stop_server())
                                    finally:
                                        loop.close()
                                
                                stop_thread = threading.Thread(target=stop_server_thread, daemon=True)
                                stop_thread.start()
                                logger.info("[SmartEnergy] Server stop process initiated")
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
                from .models import set_server_state
                set_server_state(ServerState.ACTIVE, "player joined during shutdown - cancelling")
                from .models import set_stop_initiated_time, set_server_hung
                set_stop_initiated_time(None)
                set_server_hung(False)

            time.sleep(1)

        except Exception as e:
            import traceback
            logger.error(f"[GhostConsole] Status monitor error: {e}")
            logger.error(f"[GhostConsole] Error type: {type(e).__name__}")
            logger.error(f"[GhostConsole] Full traceback:")
            for line in traceback.format_exc().split('\n'):
                if line.strip():
                    logger.error(f"[GhostConsole] {line}")
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
