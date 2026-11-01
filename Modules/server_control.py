"""
Ghost Console Controller - Window-Based Management for Minecraft Server.
"""

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Optional

import logging

from . import config
from .config import SERVER_DIR, RUN_BAT, CONSOLE_WINDOW_TITLE, GRACEFUL_SHUTDOWN_TIMEOUT
from . import models
from .models import ServerState, server_status

# Window automation imports
try:
    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False
    print("WARNING: pywinauto not installed. Window control will not work.")

logger = logging.getLogger(__name__)


def find_mc_java_proc():
    """Find OUR Minecraft server's java process, or None.

    Scoped to SERVER_DIR: a java process only counts when its working
    directory or command line references the configured server directory,
    so unrelated java applications (and other MC servers) are never
    adopted or killed by mistake. Returns the psutil.Process so callers
    can also read create_time() / kill it.
    """
    try:
        import psutil
    except ImportError:
        return None

    server_dir_str = str(SERVER_DIR).lower().rstrip("\\/")
    minecraft_server_indicators = [
        'server.jar', 'minecraft_server', 'nogui', 'paper', 'purpur',
        'spigot', 'bukkit', 'forge', 'fabric',
    ]
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = (proc.info.get('name') or '').lower()
                # Skip client-side java (javaw) and non-java processes
                if 'java' not in name or 'javaw' in name:
                    continue
                cmdline_str = ' '.join(proc.info.get('cmdline') or []).lower()
                if not any(i in cmdline_str for i in minecraft_server_indicators):
                    continue

                # Scope: cwd match first (covers `java -jar server.jar` run
                # from the server dir), then absolute path in cmdline.
                try:
                    if proc.cwd().lower().rstrip("\\/") == server_dir_str:
                        return proc
                except Exception:
                    pass
                if server_dir_str in cmdline_str:
                    return proc
            except Exception:
                continue
        return None
    except Exception as e:
        logger.error(f"Failed to find Minecraft process: {e}")
        return None


def is_minecraft_process_running() -> bool:
    """Check if our Minecraft Java process is currently running."""
    return find_mc_java_proc() is not None


class GhostConsoleController:
    """Manages Minecraft server via visible CMD window using pywinauto."""

    def __init__(self, server_dir: Path, run_bat: Path, window_title: str):
        self.server_dir = server_dir
        self.run_bat = run_bat
        self.window_title = window_title
        self.process: Optional[subprocess.Popen] = None

    def find_console_window(self) -> Optional[object]:
        """Find the CMD window by title using pywinauto."""
        if not PYWINAUTO_AVAILABLE:
            return None
        
        try:
            # Get all windows and find ours
            desktop = Desktop(backend="win32")
            windows = desktop.windows()
            
            for window in windows:
                try:
                    title = window.window_text()
                    # Windows prepends "Administrator: " to elevated consoles
                    normalized = title.removeprefix("Administrator: ").strip()
                    if normalized == self.window_title or normalized.endswith(self.window_title):
                        return window
                except Exception:
                    continue
            return None
        except Exception as e:
            logger.error(f"[GhostConsole] Error finding window: {e}")
            return None

    def is_console_running(self) -> bool:
        """Check if the console window exists."""
        return self.find_console_window() is not None

    async def start(self) -> bool:
        """Start the Minecraft server in a visible CMD window with title."""
        if not self.run_bat.exists():
            logger.error(f"[GhostConsole] run.bat not found at {self.run_bat}")
            return False

        # Check if window already exists
        if self.is_console_running():
            logger.info("[GhostConsole] Console window already exists, attaching...")
            models.start_time = time.time()
            config.stop_initiated_time = None
            config.server_hung = False
            return True

        try:
            # Create a wrapper batch that sets the title and runs the server
            wrapper_bat = self.server_dir / "_mc_echost_wrapper.bat"
            wrapper_content = f'''@echo off
title {self.window_title}
cd /d "{self.server_dir}"
call "{self.run_bat}"
pause'''
            wrapper_bat.write_text(wrapper_content)

            # Launch the wrapper in a visible CMD window
            self.process = subprocess.Popen(
                ["cmd", "/c", "start", str(wrapper_bat)],
                cwd=str(self.server_dir),
                shell=True,
            )
            
            # Wait a moment for window to appear
            await asyncio.sleep(1)
            
            if self.is_console_running():
                logger.info(f"[GhostConsole] Console window '{self.window_title}' created successfully")
                models.start_time = time.time()
                config.stop_initiated_time = None
                config.server_hung = False
                return True
            else:
                logger.error("[GhostConsole] Window did not appear after start")
                return False
                
        except Exception as e:
            logger.error(f"[GhostConsole] Failed to start server: {e}")
            return False

    async def send_command(self, command: str) -> bool:
        """Send command using RCON when available, fallback to pywinauto."""
        # Try RCON first if enabled
        try:
            from .rcon_client import send_rcon_command, is_rcon_available
            if is_rcon_available():
                logger.info(f"[RCON] Sending command: {command}")
                response = send_rcon_command(command)
                if response is not None:
                    logger.info(f"[RCON] Command sent successfully")
                    return True
                else:
                    logger.warning("[RCON] Failed to send command, falling back to pywinauto")
        except ImportError:
            logger.debug("[RCON] RCON client not available, using pywinauto")
        except Exception as e:
            logger.warning(f"[RCON] Error: {e}, falling back to pywinauto")
        
        # Fallback to pywinauto
        if not PYWINAUTO_AVAILABLE:
            logger.error("[GhostConsole] pywinauto not available")
            return False

        window = self.find_console_window()
        if not window:
            logger.warning("[GhostConsole] Console window not found")
            return False

        try:
            # Bring window to foreground (optional but helps)
            try:
                window.set_focus()
            except Exception:
                pass  # Focus not critical

            # Type command and press Enter
            # Escape pywinauto special chars (+^%~(){}[]) so they type literally,
            # then send spaces literally via with_spaces=True
            escaped_command = ''.join(
                '{' + c + '}' if c in '+^%~(){}[]' else c for c in command
            )
            logger.info(f"[GhostConsole] Sending command '{command}' to console window...")
            send_keys(f"{escaped_command}{{ENTER}}", with_spaces=True)
            
            logger.info(f"[GhostConsole] Command '{command}' sent")
            return True
            
        except Exception as e:
            logger.error(f"[GhostConsole] Failed to send command: {e}")
            return False

    async def send_stop_command(self) -> bool:
        """Send 'stop' command to the console window using pywinauto."""
        success = await self.send_command("stop")
        if success:
            config.stop_initiated_time = time.time()
        return success

    async def stop(self) -> bool:
        """
        Stop the server safely (NO FORCE KILL to prevent world corruption):
        1. Send 'stop' via pywinauto keystrokes (if window available)
        2. Wait indefinitely for graceful shutdown
        3. Mark as 'hung' if takes longer than 60s (but DO NOT kill)
        """
        window_exists = self.is_console_running()
        java_running = is_minecraft_process_running()

        # Check if server is already offline
        if not window_exists and not java_running:
            logger.info("[GhostConsole] Server already offline")
            config.stop_initiated_time = None
            config.server_hung = False
            return True

        # Try graceful stop via window
        if window_exists:
            stop_sent = await self.send_stop_command()
            if not stop_sent:
                logger.error("[GhostConsole] Failed to send stop command to window")
                return False
            logger.info("[GhostConsole] Stop command sent to window. Waiting for shutdown...")
        else:
            # Java is running but window is gone - can't gracefully stop
            logger.warning("[GhostConsole] Java running but no console window found!")
            logger.warning("[GhostConsole] Please stop the server manually or close the Java process")
            return False

        # Wait for server to stop gracefully (NO TIMEOUT - prevent corruption)
        wait_start = time.time()
        last_log = time.time()
        java_was_running = True

        while True:
            window_exists = self.is_console_running()
            java_running = is_minecraft_process_running()

            # Abort shutdown if state changed back to ACTIVE (player joined during shutdown)
            if server_status.state == ServerState.ACTIVE:
                logger.info("[GhostConsole] Shutdown aborted - player joined during shutdown")
                config.stop_initiated_time = None
                config.server_hung = False
                return True

            # Check if server stopped completely
            if not window_exists and not java_running:
                elapsed = time.time() - wait_start
                logger.info(f"[GhostConsole] Server stopped gracefully after {elapsed:.1f}s")
                config.stop_initiated_time = None
                config.server_hung = False
                return True

            # Java just stopped but window still exists (CMD at "Press any key" prompt)
            if java_was_running and not java_running and window_exists:
                logger.info("[GhostConsole] Java process stopped. Closing CMD window...")
                await asyncio.sleep(1)  # Brief delay to let pause appear
                try:
                    window = self.find_console_window()
                    if window:
                        window.close()
                        logger.info("[GhostConsole] CMD window closed")
                except Exception as e:
                    logger.warning(f"[GhostConsole] Could not close CMD window: {e}")
                # Status will update on next loop iteration

            java_was_running = java_running

            # Mark as hung after 60s but keep waiting (don't kill)
            elapsed = time.time() - wait_start
            if elapsed > GRACEFUL_SHUTDOWN_TIMEOUT and not config.server_hung:
                logger.warning("[GhostConsole] Server is taking longer than 60s to stop. This is normal for large worlds. Continuing to wait...")
                config.server_hung = True
            
            # Log progress every 30s
            if time.time() - last_log > 30:
                logger.info(f"[GhostConsole] Still waiting for shutdown... ({elapsed:.0f}s elapsed)")
                last_log = time.time()
            
            await asyncio.sleep(1)


# Create global controller instance
console_controller = GhostConsoleController(SERVER_DIR, RUN_BAT, CONSOLE_WINDOW_TITLE)
