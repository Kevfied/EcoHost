"""
MC-EcoHost - Minecraft Server Management System
The Core: FastAPI Backend with Ghost Console Controller
"""

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import psutil
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Authentication imports
from auth import (
    init_auth, init_auth_db, create_default_admin, get_all_users, get_user_by_username,
    get_user_by_id, save_user, delete_user_by_id, update_last_login,
    verify_password, hash_password, create_access_token, verify_token,
    require_auth, require_admin, require_moderator, require_permission,
    User, UserResponse, UserCreate, UserLogin, UserRegister, ChangePasswordRequest,
    UserRole, PERMISSIONS, is_rate_limited, record_failed_attempt,
    clear_failed_attempts, is_api_rate_limited, record_api_request,
    ACCESS_TOKEN_EXPIRE_MINUTES, validate_password_strength, has_permission
)

# Module imports
from Modules.config import (
    SERVER_DIR, RUN_BAT, LOG_FILE, CONSOLE_WINDOW_TITLE,
    LOG_BUFFER_SIZE, MAX_PLAYERS, MINECRAFT_PORT,
    GRACEFUL_SHUTDOWN_TIMEOUT, STATUS_CHECK_INTERVAL,
    CONFIG_FILE, BASE_DIR, IDLE_COUNTDOWN_SECONDS,
    AUTO_START_ON_PING, stop_initiated_time, server_hung,
    empty_server_countdown, countdown_active, ping_listener_running,
    ecohost_precision_last_mode_change, ecohost_precision_cooldown,
    ecohost_precision_empty_since, ECOHOST_LOG_BUFFER_SIZE
)

# Check for pywinauto availability
PYWINAUTO_AVAILABLE = False
try:
    import pywinauto
    PYWINAUTO_AVAILABLE = True
except ImportError:
    pywinauto = None
from Modules.models import (
    ServerState, PlayerInfo, ServerStatus, Settings,
    server_status, app_settings, log_lines, log_lock,
    ecohost_logs, ecohost_logs_lock, log_watcher_running,
    java_process_pid, load_settings_from_file, save_settings_to_file,
    PowerResponse, StatusResponse, LogsResponse, ConsoleStatusResponse,
    SettingsResponse, MetricsHistoryResponse, PlayerStatsResponse,
    PlayerDataResponse, get_start_time
)
from Modules.server_control import console_controller, is_minecraft_process_running
from Modules.log_watcher import start_log_watcher, start_ping_listener, record_player_join, record_player_leave, update_ecohost_precision_mode
from Modules.resource_monitor import get_system_resources
from Modules.commands import get_server_tps
from Modules.player_sessions import (
    player_sessions, server_uptime_stats, PlayerSession,
    format_duration, save_player_stats, load_player_stats,
    save_uptime_stats, load_uptime_stats, record_server_start,
    record_server_stop, get_total_uptime
)
from Modules.player_data import (
    PLAYER_DATA_CACHE_TTL,
    query_minecraft_player_data, SimpleNBTParser,
    parse_nbt_inventory, read_offline_player_data,
    has_following_entity_data, is_offline_response,
    parse_live_player_data, PYWINAUTO_AVAILABLE
)

import logging
from logging.handlers import MemoryHandler
from dataclasses import dataclass
from contextlib import asynccontextmanager
import subprocess

logger = logging.getLogger(__name__)

# Setup logging handler
class EcoHostLogHandler(logging.Handler):
    """Custom handler to capture logs to a buffer."""
    def emit(self, record):
        log_entry = self.format(record)
        with ecohost_logs_lock:
            ecohost_logs.append(log_entry)
            if len(ecohost_logs) > ECOHOST_LOG_BUFFER_SIZE:
                ecohost_logs.pop(0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        EcoHostLogHandler(),
        logging.StreamHandler()
    ]
)

# Add custom handler to capture logs
ecohost_handler = EcoHostLogHandler()
ecohost_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(ecohost_handler)


# =============================================================================
# Helper Functions
# =============================================================================


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


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a specific port is open on the given host."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, socket.error, OSError):
        return False


def get_network_status() -> dict[str, bool]:
    """Check network adapter status for VPN connections."""
    status = {"zerotier": False, "radmin": False}
    try:
        net_if_addrs = psutil.net_if_addrs()
        net_if_stats = psutil.net_if_stats()
        
        for interface, addresses in net_if_addrs.items():
            interface_lower = interface.lower()
            is_up = net_if_stats.get(interface, None)
            is_interface_up = is_up.isup if is_up else False
            
            if not is_interface_up:
                continue
            
            zerotier_patterns = ["zerotier", "zt", "zttap", "zttun"]
            if any(pattern in interface_lower for pattern in zerotier_patterns):
                status["zerotier"] = True
            
            if "radmin" in interface_lower:
                status["radmin"] = True
    except Exception as e:
        logger.error(f"Failed to get network status: {e}")
    
    return status


def attach_to_existing_server() -> bool:
    """Detect and attach to an already-running Minecraft server by window or Java."""
    from Modules.models import set_start_time
    
    if PYWINAUTO_AVAILABLE and console_controller.is_console_running():
        logger.info(f"[GhostConsole] Found existing console window '{CONSOLE_WINDOW_TITLE}'")
        set_start_time(time.time())
        
        port_open = is_port_open("127.0.0.1", MINECRAFT_PORT)
        if port_open:
            logger.info("[GhostConsole] Server port open - marking as ACTIVE")
            server_status.state = ServerState.ACTIVE
        else:
            logger.info("[GhostConsole] Server port closed - marking as STARTING")
            server_status.state = ServerState.STARTING
        return True
    
    if is_minecraft_process_running():
        logger.info("[GhostConsole] Found Java process without window, attaching...")
        set_start_time(time.time())
        
        port_open = is_port_open("127.0.0.1", MINECRAFT_PORT)
        if port_open:
            server_status.state = ServerState.ACTIVE
        else:
            server_status.state = ServerState.STARTING
        return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler (replaces deprecated on_event)."""
    logger.info("MC-EcoHost starting up...")
    
    init_auth()
    logger.info("[Auth] Authentication system initialized")
    
    load_settings_from_file()
    
    # Clear any stale player data from previous crashes
    server_status.players.clear()
    logger.info("[Startup] Cleared stale player data")
    
    app_settings.current_power_mode = get_current_power_mode()
    
    attach_to_existing_server()
    
    start_log_watcher()
    start_ping_listener()
    logger.info("MC-EcoHost ready")
    yield
    logger.info("MC-EcoHost shutting down...")


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(title="MC-EcoHost", lifespan=lifespan)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/items", StaticFiles(directory=str(BASE_DIR / "items")), name="items")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web")), name="static")


# =============================================================================
# Request/Response Models for Endpoints
# =============================================================================


class PowerModeRequest(BaseModel):
    mode: str


class ConsoleCommandRequest(BaseModel):
    command: str


class SettingsUpdate(BaseModel):
    auto_shutdown_enabled: Optional[bool] = None
    auto_shutdown_duration: Optional[int] = None
    auto_start_on_ping: Optional[bool] = None
    power_mode_scheduling_enabled: Optional[bool] = None
    high_performance_start: Optional[str] = None
    high_performance_end: Optional[str] = None
    ecohost_precision_enabled: Optional[bool] = None
    rcon_enabled: Optional[bool] = None
    rcon_port: Optional[int] = None
    rcon_password: Optional[str] = None


# =============================================================================
# Authentication Dependencies
# =============================================================================


async def get_current_user(request: Request) -> User:
    """Extract and verify user from JWT cookie."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    token_data = verify_token(token)
    if not token_data or not token_data.user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = get_user_by_id(token_data.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    
    return user


def require_auth():
    """Dependency that requires authentication."""
    return Depends(get_current_user)


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require admin role."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_moderator(user: User = Depends(get_current_user)) -> User:
    """Require moderator or admin role."""
    if user.role not in [UserRole.MODERATOR, UserRole.ADMIN]:
        raise HTTPException(status_code=403, detail="Moderator access required")
    return user


async def require_player_management(user: User = Depends(get_current_user)) -> User:
    """Require player management permission (moderator or admin)."""
    if not has_permission(user.role, "player_management"):
        raise HTTPException(status_code=403, detail="Player management permission required")
    return user


async def require_server_start(user: User = Depends(get_current_user)) -> User:
    """Require permission to start server."""
    if not has_permission(user.role, "server_start"):
        raise HTTPException(status_code=403, detail="Server start permission required")
    return user


def require_permission(permission: str):
    """Factory for permission-based dependency."""
    async def checker(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")
        return user
    return checker


# =============================================================================
# Security Headers Middleware
# =============================================================================

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# Get the directory where dashboard.html is located
DASHBOARD_DIR = Path(__file__).parent / "web"

@app.get("/")
async def serve_dashboard(current_user: User = require_auth()):
    """Serve the main dashboard (requires authentication)."""
    dashboard_path = DASHBOARD_DIR / "dashboard.html"
    if dashboard_path.exists():
        return FileResponse(dashboard_path)
    return {"error": "Dashboard not found"}


@app.get("/login.html")
async def serve_login():
    """Serve the login page."""
    login_path = DASHBOARD_DIR / "login.html"
    if login_path.exists():
        return FileResponse(login_path)
    return {"error": "Login page not found"}


@app.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    """Serve static files from web directory."""
    static_path = DASHBOARD_DIR / file_path
    if static_path.exists() and static_path.is_file():
        return FileResponse(static_path)
    return {"error": "File not found"}


# =============================================================================
# Authentication Endpoints
# =============================================================================

@app.post("/auth/login")
async def login(request: Request, login_data: UserLogin):
    """Authenticate user and set JWT cookie."""
    client_ip = request.client.host if request.client else "unknown"
    
    # Check rate limiting
    is_limited, remaining = is_rate_limited(client_ip)
    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining} seconds."
        )
    
    # Find user
    user = get_user_by_username(login_data.username)
    if not user or not user.is_active:
        record_failed_attempt(client_ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Verify password
    if not verify_password(login_data.password, user.password_hash):
        record_failed_attempt(client_ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Clear failed attempts on success
    clear_failed_attempts(client_ip)
    
    # Update last login
    update_last_login(user.id)
    
    # Create JWT token
    access_token = create_access_token(
        data={
            "sub": user.id,
            "username": user.username,
            "role": user.role.value,
        }
    )
    
    # Set cookie and return response
    response = JSONResponse({
        "success": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role.value,
            "created_at": user.created_at,
            "last_login": time.time(),
            "is_active": user.is_active,
            "require_password_change": user.require_password_change,
        },
        "require_password_change": user.require_password_change,
    })
    
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    
    logger.info(f"[Auth] User '{user.username}' logged in from {client_ip}")
    return response


@app.post("/auth/register")
async def register(request: Request, register_data: UserRegister):
    """Register a new user account with default Viewer role."""
    client_ip = request.client.host if request.client else "unknown"
    
    # Check rate limiting
    is_limited, remaining = is_rate_limited(client_ip)
    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {remaining} seconds."
        )
    
    # Check if username already exists
    existing_user = get_user_by_username(register_data.username)
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already exists")
    
    # Create new user with Viewer role
    password_hash = hash_password(register_data.password)
    new_user = User(
        id=str(uuid.uuid4()),
        username=register_data.username.lower(),
        password_hash=password_hash,
        role=UserRole.VIEWER,
        created_at=time.time(),
        last_login=None,
        is_active=True,
        require_password_change=False,
    )
    
    save_user(new_user)
    logger.info(f"[Auth] New user registered: {register_data.username} from {client_ip}")
    
    # Auto-login after registration
    access_token = create_access_token(
        data={
            "sub": new_user.id,
            "username": new_user.username,
            "role": new_user.role.value,
        }
    )
    
    # Set cookie and return response
    response = JSONResponse({
        "success": True,
        "message": "Account created successfully",
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "role": new_user.role.value,
        }
    })
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    return response


@app.post("/auth/logout")
async def logout():
    """Logout user by clearing cookie."""
    response = JSONResponse({"success": True, "message": "Logged out"})
    response.delete_cookie(key="access_token")
    return response


@app.get("/auth/verify")
async def verify_auth(current_user: User = require_auth()):
    """Verify current authentication status."""
    return {
        "authenticated": True,
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "role": current_user.role.value,
            "created_at": current_user.created_at,
            "last_login": current_user.last_login,
            "is_active": current_user.is_active,
            "require_password_change": current_user.require_password_change,
        },
    }


@app.get("/auth/me")
async def get_current_user_info(current_user: User = require_auth()):
    """Get current user information."""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role.value,
        "created_at": current_user.created_at,
        "last_login": current_user.last_login,
        "is_active": current_user.is_active,
        "require_password_change": current_user.require_password_change,
    }


@app.post("/auth/change-password")
async def change_password(
    request: ChangePasswordRequest,
    current_user: User = require_auth(),
):
    """Change user password."""
    # Validate new password strength
    is_valid, error_msg = validate_password_strength(request.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    
    # If changing own password, verify current password
    if request.current_password:
        if not verify_password(request.current_password, current_user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
    elif current_user.require_password_change:
        # First-time password change - no current password needed
        pass
    else:
        raise HTTPException(status_code=400, detail="Current password required")
    
    # Update password
    current_user.password_hash = hash_password(request.new_password)
    current_user.require_password_change = False
    save_user(current_user)
    
    logger.info(f"[Auth] User '{current_user.username}' changed password")
    return {"success": True, "message": "Password changed successfully"}


# =============================================================================
# User Management Endpoints (Admin Only)
# =============================================================================

@app.get("/users")
async def list_users(admin: User = Depends(require_admin)):
    """List all users (admin only)."""
    users = get_all_users()
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role.value,
            "created_at": u.created_at,
            "last_login": u.last_login,
            "is_active": u.is_active,
            "require_password_change": u.require_password_change,
        }
        for u in users
    ]


@app.post("/users")
async def create_user(user_data: UserCreate, admin: User = Depends(require_admin)):
    """Create a new user (admin only)."""
    # Check if username already exists
    if get_user_by_username(user_data.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    
    # Validate password strength
    is_valid, error_msg = validate_password_strength(user_data.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    
    # Create user
    new_user = User(
        id=str(uuid.uuid4()),
        username=user_data.username.lower(),
        password_hash=hash_password(user_data.password),
        role=user_data.role,
        created_at=time.time(),
        is_active=True,
        require_password_change=True,  # Force password change on first login
    )
    
    save_user(new_user)
    logger.info(f"[Auth] Admin '{admin.username}' created user '{new_user.username}' with role '{new_user.role.value}'")
    
    return {
        "success": True,
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "role": new_user.role.value,
            "created_at": new_user.created_at,
            "last_login": None,
            "is_active": new_user.is_active,
            "require_password_change": new_user.require_password_change,
        },
    }


@app.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: User = Depends(require_admin)):
    """Delete a user (admin only)."""
    # Prevent self-deletion
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    
    # Check if user exists
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Delete user
    if delete_user_by_id(user_id):
        logger.info(f"[Auth] Admin '{admin.username}' deleted user '{user.username}'")
        return {"success": True, "message": f"User '{user.username}' deleted"}
    
    raise HTTPException(status_code=500, detail="Failed to delete user")


@app.put("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    role: UserRole,
    admin: User = Depends(require_admin),
):
    """Update user role (admin only)."""
    # Prevent changing own role
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    old_role = user.role
    user.role = role
    save_user(user)
    
    logger.info(f"[Auth] Admin '{admin.username}' changed user '{user.username}' role from '{old_role.value}' to '{role.value}'")
    return {"success": True, "message": f"Role updated to '{role.value}'"}


@app.put("/users/{user_id}/password")
async def admin_reset_password(
    user_id: str,
    new_password: str,
    admin: User = Depends(require_admin),
):
    """Reset user password (admin only)."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Validate password strength
    is_valid, error_msg = validate_password_strength(new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    
    user.password_hash = hash_password(new_password)
    user.require_password_change = True  # Force password change on next login
    save_user(user)
    
    logger.info(f"[Auth] Admin '{admin.username}' reset password for user '{user.username}'")
    return {"success": True, "message": "Password reset successfully"}


@app.put("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    is_active: bool,
    admin: User = Depends(require_admin),
):
    """Enable/disable user account (admin only)."""
    # Prevent disabling own account
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")
    
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_active = is_active
    save_user(user)
    
    status = "enabled" if is_active else "disabled"
    logger.info(f"[Auth] Admin '{admin.username}' {status} user '{user.username}'")
    return {"success": True, "message": f"Account {status}"}


@app.post("/settings/maintenance/toggle", response_model=dict)
async def toggle_maintenance_mode(current_user: User = Depends(require_admin)):
    """Toggle maintenance mode (admin only)."""
    global app_settings
    app_settings.maintenance_mode = not app_settings.maintenance_mode
    save_settings_to_file()
    
    status = "enabled" if app_settings.maintenance_mode else "disabled"
    logger.info(f"[Settings] Admin '{current_user.username}' {status} maintenance mode")
    
    return {
        "success": True,
        "maintenance_mode": app_settings.maintenance_mode,
        "message": f"Maintenance mode {status}"
    }


@app.post("/settings/maintenance/ips", response_model=dict)
async def update_maintenance_ips(request: dict, current_user: User = Depends(require_admin)):
    """Update maintenance mode IP whitelist (admin only)."""
    global app_settings
    ips_text = request.get("ips", "")
    
    # Parse IPs (one per line, filter empty lines)
    ips = [ip.strip() for ip in ips_text.split('\n') if ip.strip()]
    app_settings.maintenance_ips = ips
    save_settings_to_file()
    
    logger.info(f"[Settings] Admin '{current_user.username}' updated maintenance IPs: {ips}")
    
    return {
        "success": True,
        "ips": ips,
        "message": f"Updated {len(ips)} maintenance IPs"
    }


# =============================================================================
# Protected Status Endpoint
# =============================================================================

@app.get("/status", response_model=StatusResponse)
async def get_status(current_user: User = require_auth()):
    """Get current server status including console link state."""
    global server_status, server_hung, empty_server_countdown, countdown_active

    cpu, ram = get_system_resources()
    server_status.cpu_usage = cpu
    server_status.ram_usage = ram

    if server_status.state == ServerState.ACTIVE:
        server_status.uptime = time.time() - get_start_time()
        if server_status.players:
            server_status.last_player_time = time.time()

    # Check console link status
    console_link = "connected" if console_controller.is_console_running() else "disconnected"
    
    # Get network status
    network = get_network_status()

    # Calculate countdown status
    countdown_remaining = 0
    if countdown_active and empty_server_countdown:
        elapsed = time.time() - empty_server_countdown
        countdown_remaining = max(0, app_settings.auto_shutdown_duration - int(elapsed))

    # Get real server performance metrics
    avg_tick = 50.0  # Default fallback
    real_tps = None
    
    if server_status.state == ServerState.ACTIVE:
        try:
            # Try to get real TPS from server
            import asyncio
            real_tps = asyncio.run(get_server_tps())
            if real_tps is not None:
                # Calculate tick time from real TPS
                avg_tick = 1000.0 / real_tps if real_tps > 0 else 50.0
                logger.debug(f"[Status] Real TPS: {real_tps:.2f}, Avg tick: {avg_tick:.2f}ms")
            else:
                # Fallback to estimation if real TPS unavailable
                base_tick = 50.0  # 20 TPS = 50ms per tick
                cpu_impact = cpu * 0.5  # CPU usage increases tick time
                player_impact = len(server_status.players) * 2.0  # Each player adds ~2ms
                avg_tick = base_tick + cpu_impact + player_impact
                avg_tick = max(20.0, min(200.0, avg_tick))
                logger.debug(f"[Status] Estimated tick time: {avg_tick:.2f}ms (real TPS unavailable)")
        except Exception as e:
            logger.warning(f"[Status] Failed to get real TPS, using estimation: {e}")
            # Fallback estimation
            base_tick = 50.0
            cpu_impact = cpu * 0.5
            player_impact = len(server_status.players) * 2.0
            avg_tick = base_tick + cpu_impact + player_impact
            avg_tick = max(20.0, min(200.0, avg_tick))

    return StatusResponse(
        state=server_status.state,
        players=[p.username for p in server_status.players],
        cpu_usage=cpu,
        ram_usage=ram,
        uptime=server_status.uptime,
        player_count=len(server_status.players),
        max_players=MAX_PLAYERS,
        network=network,
        console_link=console_link,
        server_hung=server_hung,
        countdown_active=countdown_active,
        countdown_remaining=countdown_remaining,
        countdown_total=app_settings.auto_shutdown_duration,
        avg_tick=avg_tick,
        maintenance_mode=app_settings.maintenance_mode,
    )


@app.get("/console/status", response_model=ConsoleStatusResponse)
async def get_console_status(current_user: User = require_auth()):
    """Get detailed console window status."""
    window_found = console_controller.is_console_running()
    return ConsoleStatusResponse(
        window_title=CONSOLE_WINDOW_TITLE,
        window_found=window_found,
        pywinauto_available=PYWINAUTO_AVAILABLE,
    )


@app.post("/power/start", response_model=PowerResponse)
async def start_server(request: Request, current_user: User = Depends(require_server_start)):
    """Start the Minecraft server in a visible CMD window."""
    
    global server_status
    
    # Check maintenance mode
    if app_settings.maintenance_mode:
        # Allow admins to start server during maintenance
        if current_user.role.value != UserRole.ADMIN:
            return PowerResponse(
                success=False,
                message="Server is in Maintenance Mode - Only administrators can start the server",
                state=server_status.state
            )
    client_ip = request.client.host if request.client else "unknown"
    
    # Check API rate limiting
    is_limited, remaining = is_api_rate_limited(client_ip)
    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {remaining} seconds."
        )
    
    # Record this request
    record_api_request(client_ip)

    if server_status.state != ServerState.IDLE:
        return PowerResponse(
            success=False,
            message=f"Cannot start: Server is {server_status.state.value}",
            state=server_status.state,
        )

    # Verify run.bat exists
    if not RUN_BAT.exists():
        logger.error(f"[GhostConsole] run.bat not found at {RUN_BAT}")
        return PowerResponse(
            success=False,
            message=f"run.bat not found at {SERVER_DIR}",
            state=ServerState.IDLE,
        )

    server_status.state = ServerState.STARTING
    logger.info(f"[GhostConsole] Starting Minecraft server from {RUN_BAT}")

    # Switch to High Performance power mode when starting server
    set_windows_power_mode("high_performance")

    try:
        success = await console_controller.start()
        
        if success:
            # Record server start for uptime tracking
            record_server_start()
            return PowerResponse(
                success=True,
                message=f"Server started in window '{CONSOLE_WINDOW_TITLE}'",
                state=ServerState.STARTING,
            )
        else:
            server_status.state = ServerState.IDLE
            return PowerResponse(
                success=False,
                message="Failed to start server window",
                state=ServerState.IDLE,
            )
        
    except Exception as e:
        logger.error(f"[GhostConsole] Failed to start server: {e}")
        server_status.state = ServerState.IDLE
        return PowerResponse(
            success=False,
            message=f"Failed to start server: {str(e)}",
            state=ServerState.IDLE,
        )


@app.post("/power/stop", response_model=PowerResponse)
async def stop_server(request: Request, current_user: User = Depends(require_permission("server_stop"))):
    """Stop the Minecraft server via window control."""
    client_ip = request.client.host if request.client else "unknown"
    
    # Check API rate limiting
    is_limited, remaining = is_api_rate_limited(client_ip)
    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {remaining} seconds."
        )
    
    # Record this request
    record_api_request(client_ip)
    
    global server_status, server_hung

    if server_status.state == ServerState.IDLE:
        return PowerResponse(
            success=False,
            message="Server is not running",
            state=server_status.state,
        )

    # CRITICAL: Send RCON stop command WHILE server is still ACTIVE
    # RCON becomes unavailable once server state changes to STOPPING
    try:
        from Modules.commands import execute_command
        logger.info("[PowerStop] Sending stop command via RCON (server still ACTIVE)")
        rcon_success = execute_command("stop")
        if rcon_success:
            logger.info("[PowerStop] Stop command sent successfully via RCON")
        else:
            logger.warning("[PowerStop] RCON failed, will use fallback method")
    except Exception as e:
        logger.error(f"[PowerStop] RCON error: {e}")

    # Record server stop for uptime tracking
    record_server_stop()
    
    # Now change server state after attempting RCON
    server_status.state = ServerState.STOPPING
    server_hung = False  # Reset hung state

    # Start fallback stop process (will only be used if RCON failed)
    success = await console_controller.stop()

    if success:
        # Switch to Power Saver mode after stopping server
        set_windows_power_mode("power_saver")
        return PowerResponse(
            success=True,
            message="Stop command sent to console window",
            state=ServerState.STOPPING,
        )
    else:
        server_status.state = ServerState.IDLE
        return PowerResponse(
            success=False,
            message="Failed to stop server",
            state=ServerState.IDLE,
        )


@app.get("/logs", response_model=LogsResponse)
async def get_logs(current_user: User = require_auth()):
    """Get the last 50 lines of server logs."""
    with log_lock:
        return LogsResponse(
            logs=log_lines[-LOG_BUFFER_SIZE:] if log_lines else [],
            count=len(log_lines),
        )


@app.get("/ecohost-logs", response_model=LogsResponse)
async def get_ecohost_logs(current_user: User = require_auth()):
    """Get the last 50 lines of EcoHost application logs."""
    with ecohost_logs_lock:
        return LogsResponse(
            logs=ecohost_logs[-ECOHOST_LOG_BUFFER_SIZE:] if ecohost_logs else [],
            count=len(ecohost_logs),
        )


@app.post("/power/mode")
async def set_power_mode(request: PowerModeRequest):
    """Manually set Windows power mode and disable auto power modes."""
    global app_settings
    
    valid_modes = ["ultimate_performance", "high_performance", "balanced", "power_saver"]
    if request.mode not in valid_modes:
        return {"success": False, "message": f"Invalid power mode. Valid modes: {', '.join(valid_modes)}"}
    
    # Disable auto power modes when user manually switches
    app_settings.ecohost_precision_enabled = False
    app_settings.power_mode_scheduling_enabled = False
    app_settings.current_power_mode = request.mode
    
    # Set the power mode
    success = set_windows_power_mode(request.mode)
    
    if success:
        logger.info(f"[PowerMode] Manual power mode set to {request.mode}")
        logger.info("[PowerMode] Auto power modes disabled (EcoHost Precision and Power Mode Scheduling)")
        save_settings_to_file()
        return {"success": True, "message": f"Power mode set to {request.mode}", "mode": request.mode}
    else:
        return {"success": False, "message": "Failed to set power mode"}


@app.post("/console/command")
async def execute_console_command(
    request: ConsoleCommandRequest,
    current_user: User = Depends(require_permission("console_command"))
):
    """Execute a command in the Minecraft server console."""
    if server_status.state != ServerState.ACTIVE:
        return {"success": False, "message": "Server is not running"}
    
    try:
        from Modules.commands import execute_command
        success = await execute_command(request.command)
        if success:
            logger.info(f"[Console] Command executed: {request.command}")
            return {"success": True, "message": f"Command sent: {request.command}"}
        else:
            return {"success": False, "message": "Failed to send command"}
    except Exception as e:
        logger.error(f"[Console] Failed to execute command: {e}")
        return {"success": False, "message": f"Failed to execute command: {str(e)}"}


@app.get("/test")
async def test_endpoint():
    """Simple test endpoint."""
    return {"message": "API is working", "timestamp": time.time()}


@app.get("/settings")
async def get_settings(current_user: User = require_auth()):
    """Get current application settings."""
    global app_settings
    from Modules.log_watcher import is_work_hours, calculate_target_power_mode
    from Modules.resource_monitor import get_system_resources
    
    # Get current status
    player_count = len(server_status.players)
    is_work_time = is_work_hours()
    target_mode = calculate_target_power_mode(player_count)
    cpu, ram = get_system_resources()
    
    return {
        "auto_shutdown_enabled": app_settings.auto_shutdown_enabled,
        "auto_shutdown_duration": app_settings.auto_shutdown_duration,
        "auto_start_on_ping": app_settings.auto_start_on_ping,
        "power_mode_scheduling_enabled": app_settings.power_mode_scheduling_enabled,
        "high_performance_start": app_settings.high_performance_start,
        "high_performance_end": app_settings.high_performance_end,
        "current_power_mode": app_settings.current_power_mode,
        "ecohost_precision_enabled": app_settings.ecohost_precision_enabled,
        "rcon_enabled": app_settings.rcon_enabled,
        "rcon_port": app_settings.rcon_port,
        "rcon_password": app_settings.rcon_password,
        "maintenance_mode": app_settings.maintenance_mode,
        "maintenance_ips": app_settings.maintenance_ips,
        "ecohost_precision_status": {
            "enabled": app_settings.ecohost_precision_enabled,
            "player_count": player_count,
            "is_work_hours": is_work_time,
            "target_mode": target_mode,
            "cpu_usage": cpu,
            "ram_usage": ram
        }
    }


@app.put("/settings")
async def update_settings(
    settings: SettingsUpdate,
    current_user: User = Depends(require_permission("change_settings"))
):
    """Update application settings."""
    global app_settings
    
    if settings.auto_shutdown_enabled is not None:
        app_settings.auto_shutdown_enabled = settings.auto_shutdown_enabled
        logger.info(f"[Settings] Auto-shutdown enabled: {settings.auto_shutdown_enabled}")
    
    if settings.auto_shutdown_duration is not None:
        if settings.auto_shutdown_duration < 10:
            return {"success": False, "message": "Auto-shutdown duration must be at least 10 seconds"}
        app_settings.auto_shutdown_duration = settings.auto_shutdown_duration
        logger.info(f"[Settings] Auto-shutdown duration: {settings.auto_shutdown_duration}s")
    
    if settings.auto_start_on_ping is not None:
        app_settings.auto_start_on_ping = settings.auto_start_on_ping
        logger.info(f"[Settings] Auto-start on ping: {settings.auto_start_on_ping}")
    
    if settings.power_mode_scheduling_enabled is not None:
        app_settings.power_mode_scheduling_enabled = settings.power_mode_scheduling_enabled
        logger.info(f"[Settings] Power mode scheduling: {settings.power_mode_scheduling_enabled}")
    
    if settings.high_performance_start is not None:
        # Validate time format
        try:
            from datetime import datetime
            datetime.strptime(settings.high_performance_start, "%H:%M")
            app_settings.high_performance_start = settings.high_performance_start
            logger.info(f"[Settings] High performance start: {settings.high_performance_start}")
        except ValueError:
            return {"success": False, "message": "Invalid time format. Use HH:MM"}
    
    if settings.high_performance_end is not None:
        # Validate time format
        try:
            from datetime import datetime
            datetime.strptime(settings.high_performance_end, "%H:%M")
            app_settings.high_performance_end = settings.high_performance_end
            logger.info(f"[Settings] High performance end: {settings.high_performance_end}")
        except ValueError:
            return {"success": False, "message": "Invalid time format. Use HH:MM"}
    
    if settings.ecohost_precision_enabled is not None:
        app_settings.ecohost_precision_enabled = settings.ecohost_precision_enabled
        logger.info(f"[Settings] EcoHost Precision: {settings.ecohost_precision_enabled}")
        # If EcoHost Precision is being enabled, run immediate power mode check
        if settings.ecohost_precision_enabled:
            if server_status.state == ServerState.ACTIVE:
                logger.info("[Settings] EcoHost Precision enabled - running immediate power mode check")
                update_ecohost_precision_mode()
            else:
                # Server is idle/stopped - set to power_saver to save energy
                logger.info("[Settings] Server not active - setting power_saver mode")
                set_windows_power_mode("power_saver")
                app_settings.current_power_mode = "power_saver"
    
    if settings.rcon_enabled is not None:
        app_settings.rcon_enabled = settings.rcon_enabled
        logger.info(f"[Settings] RCON enabled: {settings.rcon_enabled}")
    
    if settings.rcon_port is not None:
        if not (1 <= settings.rcon_port <= 65535):
            return {"success": False, "message": "RCON port must be between 1 and 65535"}
        app_settings.rcon_port = settings.rcon_port
        logger.info(f"[Settings] RCON port: {settings.rcon_port}")
    
    if settings.rcon_password is not None:
        if len(settings.rcon_password) < 4:
            return {"success": False, "message": "RCON password must be at least 4 characters"}
        app_settings.rcon_password = settings.rcon_password
        logger.info(f"[Settings] RCON password updated")
    
    # Save settings to file
    save_settings_to_file()
    
    return {"success": True, "message": "Settings updated"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates."""
    # Check authentication from cookie
    token = websocket.cookies.get("access_token")
    if not token:
        await websocket.close(code=1008, reason="Not authenticated")
        return
    
    token_data = verify_token(token)
    if not token_data or not token_data.user_id:
        await websocket.close(code=1008, reason="Invalid token")
        return
    
    user = get_user_by_id(token_data.user_id)
    if not user or not user.is_active:
        await websocket.close(code=1008, reason="User not found or inactive")
        return
    
    await websocket.accept()
    logger.info(f"[WebSocket] Client connected: {user.username} ({user.role.value})")

    try:
        while True:
            # Send status update every second
            cpu, ram = get_system_resources()
            network = get_network_status()
            server_status.cpu_usage = cpu
            server_status.ram_usage = ram

            if server_status.state == ServerState.ACTIVE:
                server_status.uptime = time.time() - get_start_time()

            # Check console link status
            console_link = console_controller.is_console_running() if PYWINAUTO_AVAILABLE else False

            # Calculate countdown remaining time
            countdown_remaining = 0
            countdown_total = app_settings.auto_shutdown_duration
            if countdown_active and empty_server_countdown:
                countdown_remaining = max(0, app_settings.auto_shutdown_duration - (time.time() - empty_server_countdown))

            # Get real server performance metrics
            avg_tick = 50.0  # Default fallback
            real_tps = None
            
            if server_status.state == ServerState.ACTIVE:
                try:
                    # Try to get real TPS from server
                    real_tps = await get_server_tps()
                    if real_tps is not None:
                        # Calculate tick time from real TPS
                        avg_tick = 1000.0 / real_tps if real_tps > 0 else 50.0
                        logger.debug(f"[Status] Real TPS: {real_tps:.2f}, Avg tick: {avg_tick:.2f}ms")
                    else:
                        # Fallback to estimation if real TPS unavailable
                        base_tick = 50.0  # 20 TPS = 50ms per tick
                        cpu_impact = cpu * 0.5  # CPU usage increases tick time
                        player_impact = len(server_status.players) * 2.0  # Each player adds ~2ms
                        avg_tick = base_tick + cpu_impact + player_impact
                        avg_tick = max(20.0, min(200.0, avg_tick))
                        logger.debug(f"[Status] Estimated tick time: {avg_tick:.2f}ms (real TPS unavailable)")
                except Exception as e:
                    logger.warning(f"[Status] Failed to get real TPS, using estimation: {e}")
                    # Fallback estimation
                    base_tick = 50.0
                    cpu_impact = cpu * 0.5
                    player_impact = len(server_status.players) * 2.0
                    avg_tick = base_tick + cpu_impact + player_impact
                    avg_tick = max(20.0, min(200.0, avg_tick))

            data = {
                "state": server_status.state.value,
                "players": [p.username for p in server_status.players],
                "cpu_usage": cpu,
                "ram_usage": ram,
                "uptime": server_status.uptime,
                "player_count": len(server_status.players),
                "max_players": MAX_PLAYERS,
                "network": network,
                "console_link": console_link,
                "server_hung": server_hung,
                "pywinauto_available": PYWINAUTO_AVAILABLE,
                "countdown_active": countdown_active,
                "countdown_remaining": int(countdown_remaining),
                "countdown_total": countdown_total,
                "current_power_mode": app_settings.current_power_mode,
                "avg_tick": avg_tick,
                "maintenance_mode": app_settings.maintenance_mode,
            }

            await websocket.send_json(data)
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")


# =============================================================================
# Health Check
# =============================================================================


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "MC-EcoHost"}


# =============================================================================
# Resource Metrics History
# =============================================================================

# Store resource metrics history (keep last 24 hours = 1440 minutes at 1-min intervals)
MAX_METRICS_HISTORY = 1440
resource_metrics_history = []


@dataclass
class ResourceMetrics:
    timestamp: float
    cpu_percent: float
    ram_percent: float
    ram_used_mb: float
    ram_total_mb: float


def record_resource_metrics():
    """Record current resource metrics to history."""
    global resource_metrics_history
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        
        metrics = ResourceMetrics(
            timestamp=time.time(),
            cpu_percent=cpu_percent,
            ram_percent=memory.percent,
            ram_used_mb=memory.used / (1024 * 1024),
            ram_total_mb=memory.total / (1024 * 1024)
        )
        
        resource_metrics_history.append(metrics)
        
        if len(resource_metrics_history) > MAX_METRICS_HISTORY:
            resource_metrics_history.pop(0)
    except Exception as e:
        logger.error(f"[Metrics] Failed to record resource metrics: {e}")


def metrics_recorder():
    """Background thread to record metrics every minute."""
    while True:
        record_resource_metrics()
        time.sleep(60)


metrics_thread = threading.Thread(target=metrics_recorder, daemon=True)
metrics_thread.start()


@app.get("/resources/history", response_model=MetricsHistoryResponse)
async def get_resource_history(
    hours: int = 1,
    current_user: User = require_auth()
):
    """Get resource usage history for the specified hours."""
    try:
        cutoff_time = time.time() - (hours * 3600)
        
        filtered_metrics = [
            {
                "timestamp": m.timestamp,
                "cpu_percent": m.cpu_percent,
                "ram_percent": m.ram_percent,
                "ram_used_mb": round(m.ram_used_mb, 1),
                "ram_total_mb": round(m.ram_total_mb, 1)
            }
            for m in resource_metrics_history
            if m.timestamp >= cutoff_time
        ]
        
        return MetricsHistoryResponse(
            success=True,
            metrics=filtered_metrics,
            message=f"Retrieved {len(filtered_metrics)} data points"
        )
    except Exception as e:
        logger.error(f"[Metrics] Failed to get history: {e}")
        return MetricsHistoryResponse(
            success=False,
            metrics=[],
            message=f"Failed to get history: {str(e)}"
        )


# =============================================================================
# Emergency Shutdown Endpoint
# =============================================================================

@app.post("/emergency-shutdown", response_model=dict)
async def emergency_shutdown(
    current_user: User = Depends(require_admin)
):
    """Emergency shutdown that kills the Minecraft server process immediately."""
    try:
        from Modules.models import server_status
        from Modules.server_control import console_controller
        
        logger.warning(f"[EmergencyShutdown] User '{current_user.username}' initiated emergency shutdown")
        
        # Kill the Java process immediately
        success = False
        
        if PYWINAUTO_AVAILABLE:
            # Force kill the server console window
            try:
                import pywinauto
                # Find and kill the server console window
                for window in pywinauto.Desktop(backend="uia").windows():
                    if CONSOLE_WINDOW_TITLE in window.window_text():
                        logger.warning(f"[EmergencyShutdown] Killing console window: {window.window_text()}")
                        window.close()
                        success = True
                        break
            except Exception as e:
                logger.error(f"[EmergencyShutdown] Failed to kill console window: {e}")
        
        # Also try to kill Java process directly
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name']):
                if 'java' in proc.info['name'].lower():
                    logger.warning(f"[EmergencyShutdown] Killing Java process: PID {proc.pid}")
                    proc.kill()
                    success = True
                    break
        except Exception as e:
            logger.error(f"[EmergencyShutdown] Failed to kill Java process: {e}")
        
        # Update server status
        if success:
            server_status.state = ServerState.STOPPING
            logger.info("[EmergencyShutdown] Emergency shutdown completed")
            return {
                "success": True,
                "message": "Emergency shutdown completed - server process killed"
            }
        else:
            return {
                "success": False,
                "message": "Failed to kill server process"
            }
            
    except Exception as e:
        logger.error(f"[EmergencyShutdown] Emergency shutdown failed: {e}")
        return {
            "success": False,
            "message": f"Emergency shutdown failed: {str(e)}"
        }


# =============================================================================
# Player Statistics Endpoint
# =============================================================================


@app.get("/players/stats", response_model=PlayerStatsResponse)
async def get_player_statistics_endpoint(request: Request, current_user: User = Depends(require_auth)):
    """Get all player statistics and history."""
    client_ip = request.client.host if request.client else "unknown"
    
    # Check API rate limiting
    is_limited, remaining = is_api_rate_limited(client_ip)
    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {remaining} seconds."
        )
    
    # Record this request
    record_api_request(client_ip)
    
    try:
        stats = []
        current_time = time.time()
        
        actual_online_players = {p.username.lower() for p in server_status.players if hasattr(p, 'username')}
        
        for username, session in player_sessions.items():
            is_online = username.lower() in actual_online_players
            
            current_session_duration = 0
            if is_online and session.sessions and session.sessions[-1]["left_at"] is None:
                current_session_duration = current_time - session.sessions[-1]["joined_at"]
            elif not is_online and session.sessions and session.sessions[-1]["left_at"] is None:
                record_player_leave(username)
            
            total_playtime = session.total_playtime_seconds + current_session_duration
            
            stats.append({
                "username": username,
                "is_online": is_online,
                "join_count": session.join_count,
                "total_playtime_seconds": round(total_playtime),
                "total_playtime_formatted": format_duration(total_playtime),
                "last_seen": session.last_seen,
                "first_join": session.first_join,
                "current_session_duration": round(current_session_duration) if is_online else 0
            })
        
        stats.sort(key=lambda x: (-x["is_online"], -x["total_playtime_seconds"]))
        online_count = sum(1 for p in stats if p["is_online"])
        
        return PlayerStatsResponse(
            success=True,
            players=stats,
            online_count=online_count,
            total_count=len(stats),
            message=f"Retrieved {len(stats)} players"
        )
    except Exception as e:
        logger.error(f"[PlayerStats] Failed to get statistics: {e}")
        return PlayerStatsResponse(
            success=False,
            players=[],
            online_count=0,
            total_count=0,
            message=f"Failed to get statistics: {str(e)}"
        )


# =============================================================================
# Player Data Endpoints
# =============================================================================


@app.get("/players/{username}/data", response_model=PlayerDataResponse)
async def get_player_data(username: str, request: Request, current_user: User = Depends(require_auth)):
    """Get detailed player data (inventory, gamemode, health, level, etc.)"""
    client_ip = request.client.host if request.client else "unknown"
    
    # Check API rate limiting
    is_limited, remaining = is_api_rate_limited(client_ip)
    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {remaining} seconds."
        )
    
    # Record this request
    record_api_request(client_ip)
    
    try:
        cache_key = username.lower()
        now = time.time()
        
        # Use a per-request cache dictionary instead of global
        # This prevents cache pollution between different users
        if not hasattr(get_player_data, 'cache'):
            get_player_data.cache = {}
        
        if cache_key in get_player_data.cache:
            cached_time, cached_data = get_player_data.cache[cache_key]
            if now - cached_time < PLAYER_DATA_CACHE_TTL:
                return PlayerDataResponse(
                    success=True,
                    username=username,
                    data=cached_data,
                    message="From cache"
                )
        
        data = await query_minecraft_player_data(username)
        logger.info(f"[PlayerData] Raw data returned for {username}: {data}")
        
        if "error" in data and data["error"] is not None:
            return PlayerDataResponse(
                success=False,
                username=username,
                data={},
                message=data["error"]
            )
        
        get_player_data.cache[cache_key] = (now, data)
        
        return PlayerDataResponse(
            success=True,
            username=username,
            data=data,
            message="Data retrieved successfully"
        )
        
    except Exception as e:
        logger.error(f"[PlayerData] Endpoint error: {e}")
        return PlayerDataResponse(
            success=False,
            username=username,
            data={},
            message=f"Failed to get player data: {str(e)}"
        )


@app.post("/players/{username}/gamemode", response_model=PlayerDataResponse)
async def set_player_gamemode(
    username: str,
    gamemode: str,
    current_user: User = Depends(require_player_management)
):
    """Change player gamemode."""
    try:
        from Modules.commands import set_gamemode
        success = set_gamemode(username, gamemode)
        
        if success:
            return PlayerDataResponse(
                success=True,
                username=username,
                data={},
                message=f"Gamemode set to {gamemode}"
            )
        else:
            return PlayerDataResponse(
                success=False,
                username=username,
                data={},
                message="Failed to send command"
            )
    except Exception as e:
        logger.error(f"[PlayerData] Gamemode change error: {e}")
        return PlayerDataResponse(
            success=False,
            username=username,
            data={},
            message=f"Failed to change gamemode: {str(e)}"
        )


@app.delete("/players/{username}", response_model=dict)
async def delete_player(
    username: str,
    current_user: User = Depends(require_admin)
):
    """Delete a player from EcoHost memory (removes from player stats and current online players)."""
    try:
        from Modules.models import server_status
        from Modules.player_sessions import delete_player_stats
        
        # Remove from current online players if present
        removed_from_online = False
        original_players = list(server_status.players)  # Make a copy
        server_status.players = [p for p in server_status.players if p.username != username]
        if len(server_status.players) < len(original_players):
            removed_from_online = True
            logger.info(f"[PlayerManagement] Removed {username} from online players list")
        
        # Delete from player statistics
        stats_deleted = delete_player_stats(username)
        
        if stats_deleted or removed_from_online:
            logger.info(f"[PlayerManagement] User '{current_user.username}' deleted player '{username}' from memory")
            return {
                "success": True,
                "message": f"Player '{username}' has been removed from EcoHost memory",
                "removed_from_online": removed_from_online,
                "stats_deleted": stats_deleted
            }
        else:
            return {
                "success": False,
                "message": f"Player '{username}' not found in EcoHost memory"
            }
            
    except Exception as e:
        logger.error(f"[PlayerManagement] Failed to delete player '{username}': {e}")
        return {
            "success": False,
            "message": f"Failed to delete player: {str(e)}"
        }


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting MC-EcoHost server on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)