"""
Player Sessions - Tracks player join/leave sessions and statistics.
"""

import json
import threading
import time
from pathlib import Path

import logging

from .config import BASE_DIR, CONFIG_FILE
from .models import ServerState, server_status

logger = logging.getLogger(__name__)

# File paths for persistent data
PLAYER_STATS_FILE = BASE_DIR / "data" / "player_stats.json"
UPTIME_STATS_FILE = BASE_DIR / "data" / "uptime_stats.json"

# Global session data
player_sessions: dict[str, 'PlayerSession'] = {}
server_uptime_stats = {
    "total_uptime_seconds": 0,
    "last_session_start": None,
    "session_count": 0,
    "first_start": None
}


class PlayerSession:
    def __init__(self):
        self.sessions = []  # List of {joined_at, left_at, duration_seconds}
        self.total_playtime_seconds = 0
        self.last_seen = None
        self.join_count = 0
        self.first_join = None


def format_duration(seconds: float) -> str:
    """Format seconds into human readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m"
    elif seconds < 86400:
        hours = int(seconds/3600)
        mins = int((seconds%3600)/60)
        return f"{hours}h {mins}m"
    else:
        days = int(seconds/86400)
        hours = int((seconds%86400)/3600)
        return f"{days}d {hours}h"


def save_player_stats():
    """Save player statistics to disk."""
    try:
        data = {}
        for username, session in player_sessions.items():
            data[username] = {
                "sessions": session.sessions,
                "total_playtime_seconds": session.total_playtime_seconds,
                "last_seen": session.last_seen,
                "join_count": session.join_count,
                "first_join": session.first_join
            }
        
        with open(PLAYER_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        
        logger.info(f"[PlayerStats] Saved {len(data)} players to {PLAYER_STATS_FILE}")
        logger.debug(f"[PlayerStats] File size: {PLAYER_STATS_FILE.stat().st_size} bytes")
        
        # Log sample of data for debugging
        if data:
            sample_user = next(iter(data.keys()))
            sample_data = data[sample_user]
            logger.debug(f"[PlayerStats] Sample data for {sample_user}: sessions={len(sample_data.get('sessions', []))}, playtime={sample_data.get('total_playtime_seconds', 0)}s, joins={sample_data.get('join_count', 0)}")
        
    except Exception as e:
        logger.error(f"[PlayerStats] Failed to save player stats: {e}")
        logger.exception("[PlayerStats] Full traceback:")


def load_player_stats():
    """Load player statistics from disk."""
    global player_sessions
    try:
        if PLAYER_STATS_FILE.exists():
            file_size = PLAYER_STATS_FILE.stat().st_size
            logger.info(f"[PlayerStats] Loading from {PLAYER_STATS_FILE} ({file_size} bytes)")
            
            with open(PLAYER_STATS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            logger.info(f"[PlayerStats] Found {len(data)} players in file")
            
            for username, pdata in data.items():
                session = PlayerSession()
                session.sessions = pdata.get("sessions", [])
                session.total_playtime_seconds = pdata.get("total_playtime_seconds", 0)
                session.last_seen = pdata.get("last_seen")
                session.join_count = pdata.get("join_count", 0)
                session.first_join = pdata.get("first_join")
                player_sessions[username] = session
                
                # Log detailed info for first few players for debugging
                if len(player_sessions) <= 3:
                    logger.debug(f"[PlayerStats] Loaded {username}: sessions={len(session.sessions)}, playtime={session.total_playtime_seconds}s, joins={session.join_count}")
            
            logger.info(f"[PlayerStats] Successfully loaded {len(player_sessions)} players from disk")
        else:
            logger.info(f"[PlayerStats] No existing player stats file found at {PLAYER_STATS_FILE}")
    except Exception as e:
        logger.error(f"[PlayerStats] Failed to load player stats from {PLAYER_STATS_FILE}: {e}")
        logger.exception("[PlayerStats] Full traceback:")


def save_uptime_stats():
    """Save server uptime statistics to disk."""
    try:
        with open(UPTIME_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(server_uptime_stats, f, indent=2)
        logger.debug("[Uptime] Saved uptime stats to disk")
    except Exception as e:
        logger.error(f"[Uptime] Failed to save uptime stats: {e}")


def load_uptime_stats():
    """Load server uptime statistics from disk."""
    global server_uptime_stats
    try:
        logger.info(f"[Uptime] Looking for stats file at: {UPTIME_STATS_FILE}")
        if UPTIME_STATS_FILE.exists():
            with open(UPTIME_STATS_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                server_uptime_stats.update(loaded_data)
            logger.info(f"[Uptime] Loaded stats: {format_duration(server_uptime_stats.get('total_uptime_seconds', 0))} total uptime, {server_uptime_stats.get('session_count', 0)} sessions")
        else:
            logger.info("[Uptime] No existing uptime stats file found, using defaults")
            server_uptime_stats = {
                "total_uptime_seconds": 0,
                "last_session_start": None,
                "session_count": 0,
                "first_start": None
            }
    except Exception as e:
        logger.error(f"[Uptime] Failed to load uptime stats: {e}")


def record_server_start():
    """Record server start for uptime tracking."""
    global server_uptime_stats
    current_time = time.time()
    server_uptime_stats["last_session_start"] = current_time
    server_uptime_stats["session_count"] += 1
    if server_uptime_stats["first_start"] is None:
        server_uptime_stats["first_start"] = current_time
    save_uptime_stats()
    logger.info(f"[Uptime] Server start recorded (session #{server_uptime_stats['session_count']})")


def record_server_stop():
    """Record server stop for uptime tracking."""
    global server_uptime_stats
    if server_uptime_stats["last_session_start"]:
        session_duration = time.time() - server_uptime_stats["last_session_start"]
        server_uptime_stats["total_uptime_seconds"] += session_duration
        server_uptime_stats["last_session_start"] = None
        save_uptime_stats()
        logger.info(f"[Uptime] Server stop recorded, session lasted {format_duration(session_duration)}")


def get_total_uptime() -> float:
    """Get total uptime including current session if running."""
    total = server_uptime_stats.get("total_uptime_seconds", 0)
    if server_status.state == ServerState.ACTIVE and server_uptime_stats.get("last_session_start"):
        total += time.time() - server_uptime_stats["last_session_start"]
    return total


def record_player_join(username: str):
    """Record a player joining the server."""
    global player_sessions
    current_time = time.time()
    
    if username not in player_sessions:
        player_sessions[username] = PlayerSession()
        player_sessions[username].first_join = current_time
    
    session = player_sessions[username]
    session.join_count += 1
    session.last_seen = current_time
    
    session.sessions.append({
        "joined_at": current_time,
        "left_at": None,
        "duration_seconds": 0
    })
    
    logger.info(f"[PlayerStats] {username} joined (session #{session.join_count})")
    save_player_stats()


def record_player_leave(username: str):
    """Record a player leaving the server."""
    global player_sessions
    current_time = time.time()
    
    if username not in player_sessions:
        return
    
    session = player_sessions[username]
    session.last_seen = current_time
    
    # Find the most recent session without a left_at
    for s in reversed(session.sessions):
        if s["left_at"] is None:
            s["left_at"] = current_time
            duration = current_time - s["joined_at"]
            s["duration_seconds"] = duration
            session.total_playtime_seconds += duration
            logger.info(f"[PlayerStats] {username} left, session lasted {format_duration(duration)}")
            break
    
    save_player_stats()


def periodic_save():
    """Save stats periodically every 5 minutes."""
    while True:
        time.sleep(300)  # 5 minutes
        if player_sessions:
            save_player_stats()
        save_uptime_stats()


# Load persistent data on startup
load_player_stats()
load_uptime_stats()

# Background save thread
save_thread = threading.Thread(target=periodic_save, daemon=True)
save_thread.start()
