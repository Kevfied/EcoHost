"""
MC-EcoHost Authentication System
Secure user management with JWT tokens and role-based access control
"""

import uuid
import time
import sqlite3
import bcrypt
import secrets
from enum import Enum
from typing import Optional
from datetime import datetime, timedelta
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from pathlib import Path
from fastapi import HTTPException, Request, Depends

# =============================================================================
# Configuration
# =============================================================================

AUTH_DB_FILE = Path(__file__).parent / "data" / "auth.db"
CONFIG_FILE = Path(__file__).parent / "data" / "config.json"

# JWT Configuration
SECRET_KEY: Optional[str] = None
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 hours

# Rate limiting
failed_attempts: dict[str, list[float]] = {}  # IP -> list of timestamps
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_MINUTES = 15

# API rate limiting for critical endpoints
api_rate_limits: dict[str, list[float]] = {}  # IP -> list of timestamps
API_RATE_LIMIT_REQUESTS = 10  # Max requests per window
API_RATE_LIMIT_WINDOW = 5  # Seconds per window

# =============================================================================
# Enums & Models
# =============================================================================

class UserRole(str, Enum):
    ADMIN = "admin"
    MODERATOR = "moderator"
    VIEWER = "viewer"
    VIEWER_PLUS = "viewer_plus"


class User(BaseModel):
    id: str
    username: str
    password_hash: str
    role: UserRole
    created_at: float
    last_login: Optional[float] = None
    is_active: bool = True
    require_password_change: bool = False


class UserResponse(BaseModel):
    id: str
    username: str
    role: UserRole
    created_at: float
    last_login: Optional[float] = None
    is_active: bool
    require_password_change: bool


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8)
    role: UserRole


class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8)


class TokenData(BaseModel):
    user_id: Optional[str] = None
    username: Optional[str] = None
    role: Optional[UserRole] = None


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str = Field(..., min_length=8)


# =============================================================================
# Permission Matrix
# =============================================================================

PERMISSIONS = {
    UserRole.ADMIN: {
        "server_start",
        "server_stop",
        "console_command",
        "view_dashboard",
        "view_players",
        "view_server_logs",
        "view_ecohost_logs",
        "change_settings",
        "user_management",
        "power_mode_control",
        "player_management",
    },
    UserRole.MODERATOR: {
        "server_start",
        "server_stop",
        "console_command",
        "view_dashboard",
        "view_players",
        "view_server_logs",
        "view_ecohost_logs",
        "player_management",
    },
    UserRole.VIEWER_PLUS: {
        "server_start",
        "view_dashboard",
        "view_players",
        "view_server_logs",
    },
    UserRole.VIEWER: {
        "view_dashboard",
        "view_players",
        "view_server_logs",
    },
}


def has_permission(role: UserRole, permission: str) -> bool:
    """Check if a role has a specific permission."""
    return permission in PERMISSIONS.get(role, set())


# =============================================================================
# FastAPI Dependencies
# =============================================================================

def require_auth():
    """Dependency to require authentication."""
    async def dependency(request: Request) -> User:
        token = request.cookies.get("access_token")
        if not token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        token_data = verify_token(token)
        if not token_data:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = get_user_by_id(token_data.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        
        return user
    return dependency


def require_admin():
    """Dependency to require admin role."""
    async def dependency(current_user: User = Depends(require_auth())) -> User:
        if current_user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Admin access required")
        return current_user
    return dependency


def require_moderator():
    """Dependency to require moderator or admin role."""
    async def dependency(current_user: User = Depends(require_auth())) -> User:
        if current_user.role not in [UserRole.ADMIN, UserRole.MODERATOR]:
            raise HTTPException(status_code=403, detail="Moderator access required")
        return current_user
    return dependency


def require_permission(permission: str):
    """Dependency to require a specific permission."""
    async def dependency(current_user: User = Depends(require_auth())) -> User:
        if not has_permission(current_user.role, permission):
            raise HTTPException(status_code=403, detail=f"Permission '{permission}' required")
        return current_user
    return dependency


# =============================================================================
# Database Operations
# =============================================================================

def init_auth_db():
    """Initialize SQLite database for authentication."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_login REAL,
            is_active INTEGER DEFAULT 1,
            require_password_change INTEGER DEFAULT 0
        )
    """)
    
    conn.commit()
    conn.close()
    
    # Create default admin if no users exist
    if not get_all_users():
        create_default_admin()


def create_default_admin():
    """Create default admin account on first run."""
    default_password = "admin"
    password_hash = bcrypt.hashpw(default_password.encode(), bcrypt.gensalt()).decode()
    
    admin_user = User(
        id=str(uuid.uuid4()),
        username="admin",
        password_hash=password_hash,
        role=UserRole.ADMIN,
        created_at=time.time(),
        is_active=True,
        require_password_change=True,
    )
    
    save_user(admin_user)
    print("[Auth] Default admin account created (username: admin, password: admin)")
    print("[Auth] Password change required on first login!")


def save_user(user: User):
    """Save or update a user in the database."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT OR REPLACE INTO users 
        (id, username, password_hash, role, created_at, last_login, is_active, require_password_change)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user.id,
        user.username.lower(),
        user.password_hash,
        user.role.value,
        user.created_at,
        user.last_login,
        int(user.is_active),
        int(user.require_password_change),
    ))
    
    conn.commit()
    conn.close()


def get_user_by_username(username: str) -> Optional[User]:
    """Get a user by username (case-insensitive)."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE username = ?", (username.lower(),))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    return User(
        id=row[0],
        username=row[1],
        password_hash=row[2],
        role=UserRole(row[3]),
        created_at=row[4],
        last_login=row[5],
        is_active=bool(row[6]),
        require_password_change=bool(row[7]),
    )


def get_user_by_id(user_id: str) -> Optional[User]:
    """Get a user by ID."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    return User(
        id=row[0],
        username=row[1],
        password_hash=row[2],
        role=UserRole(row[3]),
        created_at=row[4],
        last_login=row[5],
        is_active=bool(row[6]),
        require_password_change=bool(row[7]),
    )


def get_all_users() -> list[User]:
    """Get all users from the database."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    
    return [
        User(
            id=row[0],
            username=row[1],
            password_hash=row[2],
            role=UserRole(row[3]),
            created_at=row[4],
            last_login=row[5],
            is_active=bool(row[6]),
            require_password_change=bool(row[7]),
        )
        for row in rows
    ]


def delete_user_by_id(user_id: str) -> bool:
    """Delete a user by ID. Returns True if deleted."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    deleted = cursor.rowcount > 0
    
    conn.commit()
    conn.close()
    
    return deleted


def update_last_login(user_id: str):
    """Update the last login timestamp."""
    conn = sqlite3.connect(AUTH_DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute(
        "UPDATE users SET last_login = ? WHERE id = ?",
        (time.time(), user_id)
    )
    
    conn.commit()
    conn.close()


# =============================================================================
# Password & Security
# =============================================================================

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def validate_password_strength(password: str) -> tuple[bool, str]:
    """Validate password strength. Returns (is_valid, error_message)."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    
    return True, ""


# =============================================================================
# JWT Token Handling
# =============================================================================

def load_or_generate_secret():
    """Load JWT secret from config or generate a new one."""
    global SECRET_KEY
    
    import json
    
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                if 'jwt_secret' in config and config['jwt_secret']:
                    SECRET_KEY = config['jwt_secret']
                    return
        except Exception:
            pass
    
    # Generate new secret
    SECRET_KEY = secrets.token_hex(32)
    
    # Save to config
    config = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
        except Exception:
            pass
    
    config['jwt_secret'] = SECRET_KEY
    
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"[Auth] Warning: Failed to save JWT secret: {e}")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[TokenData]:
    """Verify a JWT token and return token data."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return TokenData(
            user_id=payload.get("sub"),
            username=payload.get("username"),
            role=UserRole(payload.get("role")) if payload.get("role") else None,
        )
    except JWTError:
        return None


# =============================================================================
# Rate Limiting
# =============================================================================

def is_rate_limited(client_ip: str) -> tuple[bool, int]:
    """
    Check if a client IP is rate limited.
    Returns (is_limited, remaining_seconds).
    """
    now = time.time()
    cutoff = now - (LOCKOUT_DURATION_MINUTES * 60)
    
    # Clean old entries
    if client_ip in failed_attempts:
        failed_attempts[client_ip] = [
            t for t in failed_attempts[client_ip] if t > cutoff
        ]
        
        if len(failed_attempts[client_ip]) >= MAX_FAILED_ATTEMPTS:
            oldest_attempt = min(failed_attempts[client_ip])
            remaining = int((oldest_attempt + LOCKOUT_DURATION_MINUTES * 60) - now)
            return True, max(0, remaining)
    
    return False, 0


def record_failed_attempt(client_ip: str):
    """Record a failed login attempt."""
    now = time.time()
    if client_ip not in failed_attempts:
        failed_attempts[client_ip] = []
    failed_attempts[client_ip].append(now)


def is_api_rate_limited(client_ip: str) -> tuple[bool, int]:
    """
    Check if a client IP is rate limited for API requests.
    Returns (is_limited, remaining_seconds).
    """
    now = time.time()
    cutoff = now - API_RATE_LIMIT_WINDOW
    
    # Clean old entries
    if client_ip in api_rate_limits:
        api_rate_limits[client_ip] = [
            t for t in api_rate_limits[client_ip] if t > cutoff
        ]
        
        if len(api_rate_limits[client_ip]) >= API_RATE_LIMIT_REQUESTS:
            oldest_request = min(api_rate_limits[client_ip])
            remaining = int((oldest_request + API_RATE_LIMIT_WINDOW) - now)
            return True, max(0, remaining)
    
    return False, 0


def record_api_request(client_ip: str):
    """Record an API request for rate limiting."""
    now = time.time()
    if client_ip not in api_rate_limits:
        api_rate_limits[client_ip] = []
    api_rate_limits[client_ip].append(now)


def clear_failed_attempts(client_ip: str):
    """Clear failed attempts for a client IP (on successful login)."""
    if client_ip in failed_attempts:
        del failed_attempts[client_ip]


# =============================================================================
# Initialization
# =============================================================================

def init_auth():
    """Initialize authentication system."""
    load_or_generate_secret()
    init_auth_db()
