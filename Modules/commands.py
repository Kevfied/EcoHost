"""
Commands Module - Centralized command execution for MC-EcoHost.
Uses RCON when available, falls back to pywinauto when needed.
"""

import asyncio
import logging
from typing import Optional

from .rcon_client import send_rcon_command, is_rcon_available
from .models import ServerState, server_status

logger = logging.getLogger(__name__)


async def execute_command(command: str) -> bool:
    """
    Execute a Minecraft server command using the best available method.
    
    Priority: RCON -> pywinauto fallback
    
    Args:
        command: Minecraft server command to execute
        
    Returns:
        True if command was sent successfully, False otherwise
    """
    # Always try RCON first when server is active
    if server_status.state == ServerState.ACTIVE:
        try:
            if is_rcon_available():
                logger.info(f"[Commands] RCON: {command}")
                # mcrcon is blocking - run in a thread so the event loop isn't stalled
                response = await asyncio.to_thread(send_rcon_command, command)
                if response is not None:
                    logger.info(f"[Commands] Command sent via RCON")
                    return True
                else:
                    logger.warning("[Commands] RCON failed, trying fallback")
        except Exception as e:
            logger.warning(f"[Commands] RCON error: {e}, trying fallback")
    
    # Fallback to pywinauto
    try:
        from .server_control import console_controller
        logger.info(f"[Commands] Fallback: {command}")
        success = await console_controller.send_command(command)
        if success:
            logger.info("[Commands] Command sent via fallback")
        return success
    except Exception as e:
        logger.error(f"[Commands] All methods failed: {e}")
        return False


def get_player_data(username: str) -> Optional[str]:
    """
    Get player data using RCON 'data get entity' command.
    
    Args:
        username: Player username to query
        
    Returns:
        Server response or None if failed
    """
    if server_status.state != ServerState.ACTIVE:
        return None
        
    if not is_rcon_available():
        logger.warning("[Commands] RCON not available for player data query")
        return None
    
    try:
        command = f"data get entity {username}"
        logger.info(f"[Commands] Querying player data: {username}")
        response = send_rcon_command(command)
        return response
    except Exception as e:
        logger.error(f"[Commands] Player data query failed: {e}")
        return None


def get_player_inventory(username: str) -> Optional[str]:
    """
    Get player inventory separately using RCON to avoid size limits.
    
    Args:
        username: Player username to query
        
    Returns:
        Server response with inventory NBT or None if failed
    """
    if server_status.state != ServerState.ACTIVE:
        return None
        
    if not is_rcon_available():
        logger.warning("[Commands] RCON not available for inventory query")
        return None
    
    try:
        # Query just the Inventory field to avoid RCON size limits
        command = f"data get entity {username} Inventory"
        logger.info(f"[Commands] Querying inventory for: {username}")
        response = send_rcon_command(command)
        return response
    except Exception as e:
        logger.error(f"[Commands] Inventory query failed: {e}")
        return None


async def set_gamemode(username: str, gamemode: str) -> bool:
    """
    Set player gamemode using RCON when available.
    
    Args:
        username: Player username
        gamemode: Game mode (survival, creative, adventure, spectator)
        
    Returns:
        True if successful, False otherwise
    """
    command = f"gamemode {gamemode} {username}"
    return await execute_command(command)


def list_players() -> Optional[str]:
    """
    Get online players list using RCON.
    
    Returns:
        Server response with player list or None if failed
    """
    if server_status.state != ServerState.ACTIVE or not is_rcon_available():
        return None
    
    try:
        return send_rcon_command("list")
    except Exception as e:
        logger.error(f"[Commands] List players failed: {e}")
        return None


async def send_message(message: str) -> bool:
    """
    Send message to all players using RCON.
    
    Args:
        message: Message to send
        
    Returns:
        True if successful, False otherwise
    """
    command = f"say {message}"
    return await execute_command(command)


async def teleport_player(username: str, target: str) -> bool:
    """
    Teleport player using RCON.
    
    Args:
        username: Player to teleport
        target: Target (player or coordinates)
        
    Returns:
        True if successful, False otherwise
    """
    command = f"tp {username} {target}"
    return await execute_command(command)


async def give_item(username: str, item: str, count: int = 1) -> bool:
    """
    Give item to player using RCON.
    
    Args:
        username: Player username
        item: Item ID/name
        count: Item count (default: 1)
        
    Returns:
        True if successful, False otherwise
    """
    command = f"give {username} {item} {count}"
    return await execute_command(command)


async def kick_player(username: str, reason: str = "") -> bool:
    """
    Kick player using RCON.
    
    Args:
        username: Player to kick
        reason: Kick reason (optional)
        
    Returns:
        True if successful, False otherwise
    """
    if reason:
        command = f"kick {username} {reason}"
    else:
        command = f"kick {username}"
    return await execute_command(command)


async def ban_player(username: str, reason: str = "") -> bool:
    """
    Ban player using RCON.
    
    Args:
        username: Player to ban
        reason: Ban reason (optional)
        
    Returns:
        True if successful, False otherwise
    """
    if reason:
        command = f"ban {username} {reason}"
    else:
        command = f"ban {username}"
    return await execute_command(command)


async def stop_server() -> bool:
    """
    Stop the server using the best available method.
    
    Returns:
        True if command was sent, False otherwise
    """
    return await execute_command("stop")


# Common command aliases for convenience
async def execute_console_command(command: str) -> bool:
    """Alias for execute_command - maintains backward compatibility."""
    return await execute_command(command)


# =============================================================================
# TPS Monitoring
# =============================================================================

_tps_cache: tuple = (0.0, None)  # (fetched_at, tps_value)
_TPS_CACHE_TTL = 8.0  # seconds — status/WS polls must not hammer RCON
_tps_unsupported: bool = False  # server answers neither `tps` nor `forge tps`


def _parse_tps(response: str) -> Optional[float]:
    """Parse TPS out of server-specific response formats.

    Paper/Purpur: "TPS from last 1m, 5m, 15m: 20.0, 20.0, 19.9"
    Parse after the LAST colon so the "1m/5m/15m" labels aren't mistaken
    for the value.
    """
    import re
    tail = (response or "").rsplit(":", 1)[-1]
    match = re.search(r"(\d+(?:\.\d+)?)", tail)
    if not match:
        return None
    try:
        value = float(match.group(1))
        return value if 0 < value <= 20.5 else None
    except ValueError:
        return None


def _parse_forge_tps(response: str) -> Optional[float]:
    """Forge `forge tps` prints per-dim 'Mean TPS: 20.0' — take the last one
    (overall dims line comes last) or the first match."""
    import re
    matches = re.findall(r"Mean TPS:\s*(\d+(?:\.\d+)?)", response or "")
    if not matches:
        return None
    try:
        value = float(matches[-1])
        return value if 0 < value <= 20.5 else None
    except ValueError:
        return None


async def get_server_tps() -> Optional[float]:
    """Get real server TPS via RCON, cached for ~8s.

    Runs the blocking mcrcon call in a thread (never asyncio.run() inside
    a running loop). Returns None when the server isn't ACTIVE, RCON is
    off, or the server type doesn't answer `tps`.
    """
    global _tps_cache, _tps_unsupported
    import time as _time

    if server_status.state != ServerState.ACTIVE or not is_rcon_available():
        _tps_unsupported = False  # fresh server process may be a different type
        return None
    if _tps_unsupported:
        return None

    now = _time.time()
    if now - _tps_cache[0] < _TPS_CACHE_TTL:
        return _tps_cache[1]

    try:
        response = await asyncio.to_thread(send_rcon_command, "tps")
        tps = _parse_tps(response)
        if tps is None:
            # Vanilla has no `tps` — try Forge's variant before giving up
            response = await asyncio.to_thread(send_rcon_command, "forge tps")
            tps = _parse_forge_tps(response)
        if tps is None:
            _tps_unsupported = True
            logger.debug("[TPS] Server answers neither `tps` nor `forge tps` — TPS disabled")
        _tps_cache = (now, tps)
        return tps
    except Exception as e:
        logger.debug(f"[TPS] Query failed: {e}")
        _tps_cache = (now, None)
        return None
