"""
Commands Module - Centralized command execution for MC-EcoHost.
Uses RCON when available, falls back to pywinauto when needed.
"""

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
                response = send_rcon_command(command)
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


def set_gamemode(username: str, gamemode: str) -> bool:
    """
    Set player gamemode using RCON when available.
    
    Args:
        username: Player username
        gamemode: Game mode (survival, creative, adventure, spectator)
        
    Returns:
        True if successful, False otherwise
    """
    command = f"gamemode {gamemode} {username}"
    return execute_command(command)


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


def send_message(message: str) -> bool:
    """
    Send message to all players using RCON.
    
    Args:
        message: Message to send
        
    Returns:
        True if successful, False otherwise
    """
    command = f"say {message}"
    return execute_command(command)


def teleport_player(username: str, target: str) -> bool:
    """
    Teleport player using RCON.
    
    Args:
        username: Player to teleport
        target: Target (player or coordinates)
        
    Returns:
        True if successful, False otherwise
    """
    command = f"tp {username} {target}"
    return execute_command(command)


def give_item(username: str, item: str, count: int = 1) -> bool:
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
    return execute_command(command)


def kick_player(username: str, reason: str = "") -> bool:
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
    return execute_command(command)


def ban_player(username: str, reason: str = "") -> bool:
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
    return execute_command(command)


def stop_server() -> bool:
    """
    Stop the server using the best available method.
    
    Returns:
        True if command was sent, False otherwise
    """
    return execute_command("stop")


# Common command aliases for convenience
async def execute_console_command(command: str) -> bool:
    """Alias for execute_command - maintains backward compatibility."""
    return await execute_command(command)
