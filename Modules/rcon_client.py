"""
RCON Client - Direct RCON communication with Minecraft server.
Replaces UI automation for more reliable command execution.
"""

import logging
from typing import Optional

try:
    from mcrcon import MCRcon
    MCRCON_AVAILABLE = True
except ImportError:
    MCRCON_AVAILABLE = False
    MCRcon = None

from .config import MINECRAFT_PORT
from .models import app_settings

logger = logging.getLogger(__name__)

# Default RCON settings
DEFAULT_RCON_PORT = 25575
DEFAULT_RCON_PASSWORD = "Kersh159357"


class RCONClient:
    """RCON client for direct Minecraft server communication."""
    
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_RCON_PORT, password: str = None):
        self.host = host
        self.port = port
        self.password = password or DEFAULT_RCON_PASSWORD
        self.connection = None
        
    def connect(self) -> bool:
        """Establish RCON connection."""
        if not MCRCON_AVAILABLE:
            logger.error("[RCON] mcrcon library not available. Install with: pip install mcrcon")
            return False
            
        try:
            self.connection = MCRcon(self.host, self.password, port=self.port)
            self.connection.connect()
            logger.info(f"[RCON] Connected to {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"[RCON] Failed to connect: {e}")
            return False
    
    def disconnect(self):
        """Close RCON connection."""
        if self.connection:
            try:
                self.connection.disconnect()
                logger.debug("[RCON] Disconnected")
            except Exception as e:
                logger.warning(f"[RCON] Error during disconnect: {e}")
            finally:
                self.connection = None
    
    def send_command(self, command: str) -> Optional[str]:
        """Send command and return response."""
        if not self.connection:
            if not self.connect():
                return None
        
        try:
            response = self.connection.command(command)
            # Don't log TPS commands to reduce log spam
            if command.lower() != "tps":
                logger.info(f"[RCON] Command sent: {command}")
            if response:
                # Don't log response length for TPS commands to reduce log spam
                if command.lower() != "tps":
                    logger.info(f"[RCON] Response length: {len(response)} bytes")
                if len(response) < 500:
                    # Don't log debug responses for TPS commands
                    if command.lower() != "tps":
                        logger.debug(f"[RCON] Response: {response.strip()}")
                else:
                    # Don't log debug responses for TPS commands
                    if command.lower() != "tps":
                        logger.debug(f"[RCON] Response: {response[:200]}... (truncated)")
            else:
                logger.warning(f"[RCON] Empty response received")
            return response
        except Exception as e:
            # Don't log TPS command errors to reduce log spam
            if command.lower() != "tps":
                logger.error(f"[RCON] Failed to send command '{command}': {e}")
            # Try to reconnect on next command
            self.disconnect()
            return None


# Global RCON client instance
_rcon_client: Optional[RCONClient] = None


def get_rcon_client() -> RCONClient:
    """Get or create RCON client instance."""
    global _rcon_client
    if _rcon_client is None:
        # Get RCON settings from config or use defaults
        rcon_port = getattr(app_settings, 'rcon_port', DEFAULT_RCON_PORT)
        rcon_password = getattr(app_settings, 'rcon_password', DEFAULT_RCON_PASSWORD)
        _rcon_client = RCONClient(port=rcon_port, password=rcon_password)
    return _rcon_client


def send_rcon_command(command: str) -> Optional[str]:
    """
    Send RCON command to Minecraft server.
    
    Args:
        command: Minecraft server command to execute
        
    Returns:
        Server response string or None if failed
    """
    client = get_rcon_client()
    return client.send_command(command)


def test_rcon_connection() -> bool:
    """Test RCON connection and return status."""
    client = get_rcon_client()
    try:
        response = client.send_command("list")
        return response is not None
    except Exception:
        return False


def is_rcon_available() -> bool:
    """Check if RCON is available and configured."""
    return MCRCON_AVAILABLE and hasattr(app_settings, 'rcon_enabled') and app_settings.rcon_enabled
