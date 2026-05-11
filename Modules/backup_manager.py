"""
Backup Manager for MC-EcoHost
Handles world folder backup and restore operations.
"""

import shutil
import json
import time
import os
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Callable
import logging
import platform

from Modules.config import SERVER_DIR, CONFIG_FILE
from Modules.models import server_status

logger = logging.getLogger(__name__)

# Backup directory
BACKUP_DIR = SERVER_DIR / "backups"
WORLD_DIR = SERVER_DIR / "world"
METADATA_FILE = BACKUP_DIR / "metadata.json"

# Check if running on Windows
IS_WINDOWS = platform.system() == "Windows"


def copy_directory_fast(src: Path, dst: Path, progress_callback: Optional[Callable[[float, str], None]] = None) -> None:
    """
    Copy directory using the fastest available method.
    Uses robocopy on Windows for large file operations.
    
    Args:
        src: Source directory
        dst: Destination directory
        progress_callback: Optional callback for progress updates
    """
    if IS_WINDOWS:
        try:
            # Use robocopy for faster copying on Windows
            # /E - Copy subdirectories, including empty ones
            # /Z - Copy files in restartable mode
            # /R:5 - Retry 5 times if file is locked
            # /W:5 - Wait 5 seconds between retries
            # /NP - No progress (we'll report our own)
            # /NFL - No file list
            # /NDL - No directory list
            result = subprocess.run(
                ['robocopy', str(src), str(dst), '/E', '/Z', '/R:5', '/W:5', '/NP', '/NFL', '/NDL'],
                capture_output=True,
                text=True
            )
            
            # Robocopy returns 0-7 as success codes
            if result.returncode <= 7:
                logger.info(f"[Backup] Robocopy completed successfully (exit code: {result.returncode})")
                if progress_callback:
                    progress_callback(1.0, "Copy completed successfully")
            else:
                logger.warning(f"[Backup] Robocopy had issues (exit code: {result.returncode}), falling back to shutil")
                # Fall back to shutil if robocopy fails
                shutil.copytree(src, dst, dirs_exist_ok=False)
                if progress_callback:
                    progress_callback(1.0, "Copy completed with fallback method")
        except Exception as e:
            logger.warning(f"[Backup] Robocopy failed: {e}, falling back to shutil")
            # Fall back to shutil if robocopy is not available or fails
            shutil.copytree(src, dst, dirs_exist_ok=False)
    else:
        # Use shutil on non-Windows systems
        shutil.copytree(src, dst, dirs_exist_ok=False)


# Global state for auto-backup scheduler
backup_scheduler_running = False
backup_scheduler_thread: Optional[threading.Thread] = None


def ensure_backup_dir() -> None:
    """Ensure backup directory exists."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def load_metadata() -> Dict:
    """Load backup metadata from file."""
    if not METADATA_FILE.exists():
        return {"backups": [], "next_auto_backup": None}
    
    try:
        with open(METADATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[Backup] Failed to load metadata: {e}")
        return {"backups": [], "next_auto_backup": None}


def save_metadata(metadata: Dict) -> None:
    """Save backup metadata to file."""
    try:
        with open(METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
    except Exception as e:
        logger.error(f"[Backup] Failed to save metadata: {e}")


def get_backup_id(timestamp: Optional[float] = None) -> str:
    """Generate a unique backup ID based on timestamp."""
    if timestamp is None:
        timestamp = time.time()
    dt = datetime.fromtimestamp(timestamp)
    return f"backup_{dt.strftime('%Y-%m-%d_%H-%M-%S')}"


def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def get_dir_size(path: Path) -> int:
    """Calculate total size of a directory."""
    total_size = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = Path(dirpath) / filename
                if filepath.exists():
                    total_size += filepath.stat().st_size
    except Exception as e:
        logger.error(f"[Backup] Failed to calculate directory size: {e}")
    return total_size


def create_backup(description: str = "Manual backup", progress_callback: Optional[Callable[[float, str], None]] = None) -> Dict:
    """
    Create a backup of the world folder.
    
    Args:
        description: Description of the backup
        progress_callback: Optional callback function(progress, message) for progress updates
        
    Returns:
        Dict with backup information or error
    """
    ensure_backup_dir()
    
    # Check if world directory exists
    if not WORLD_DIR.exists():
        return {
            "success": False,
            "error": f"World directory not found at {WORLD_DIR}"
        }
    
    # Generate backup ID
    backup_id = get_backup_id()
    backup_path = BACKUP_DIR / backup_id
    
    try:
        logger.info(f"[Backup] Creating backup: {backup_id}")
        
        if progress_callback:
            progress_callback(0.0, "Initializing backup...")
        
        # Create backup directory
        backup_path.mkdir(exist_ok=True)
        
        # Copy world folder to backup directory with progress tracking
        world_backup_path = backup_path / "world"
        logger.info(f"[Backup] Copying world folder to {world_backup_path}")
        
        if progress_callback:
            progress_callback(0.1, "Calculating world size...")
        
        # Calculate total size for progress tracking
        total_size = get_dir_size(WORLD_DIR)
        copied_size = 0
        
        if progress_callback:
            progress_callback(0.2, f"Copying world ({format_size(total_size)})...")
        
        # Use fast copy method (robocopy on Windows, shutil on other platforms)
        copy_directory_fast(WORLD_DIR, world_backup_path, progress_callback)
        
        if progress_callback:
            progress_callback(0.9, "Finalizing backup...")
        
        # Calculate backup size
        backup_size = get_dir_size(world_backup_path)
        
        # Update metadata
        metadata = load_metadata()
        backup_info = {
            "id": backup_id,
            "timestamp": time.time(),
            "size_bytes": backup_size,
            "world_size": format_size(backup_size),
            "description": description
        }
        metadata["backups"].insert(0, backup_info)  # Add to beginning of list
        save_metadata(metadata)
        
        if progress_callback:
            progress_callback(1.0, "Backup complete")
        
        logger.info(f"[Backup] Backup created successfully: {backup_id} ({format_size(backup_size)})")
        
        return {
            "success": True,
            "backup_id": backup_id,
            "timestamp": backup_info["timestamp"],
            "size": format_size(backup_size),
            "description": description
        }
        
    except Exception as e:
        logger.error(f"[Backup] Failed to create backup: {e}")
        # Clean up partial backup
        if backup_path.exists():
            shutil.rmtree(backup_path, ignore_errors=True)
        
        if progress_callback:
            progress_callback(0.0, f"Backup failed: {str(e)}")
        
        return {
            "success": False,
            "error": str(e)
        }


def restore_backup(backup_id: str, progress_callback: Optional[Callable[[float, str], None]] = None) -> Dict:
    """
    Restore world from backup.
    
    Args:
        backup_id: ID of backup to restore from
        progress_callback: Optional callback for progress updates
        
    Returns:
        Dict with operation result
    """
    ensure_backup_dir()
    
    logger.info(f"[Backup] Restoring from backup: {backup_id}")
    
    if progress_callback:
        progress_callback(0.0, "Initializing restore...")
    
    # Validate backup exists
    backup_path = BACKUP_DIR / backup_id
    if not backup_path.exists():
        return {"success": False, "error": f"Backup {backup_id} not found"}
    
    world_backup_path = backup_path / "world"
    if not world_backup_path.exists():
        return {"success": False, "error": f"World folder not found in backup {backup_id}"}
    
    # Initialize safety_backup_id to make it available in exception handler
    safety_backup_id = None
    
    try:
        logger.info(f"[Backup] Restoring from backup: {backup_id}")
        
        if progress_callback:
            progress_callback(0.0, "Initializing restore...")
        
        # Create a backup of current world before restore (safety measure)
        if WORLD_DIR.exists():
            if progress_callback:
                progress_callback(0.1, "Creating safety backup...")
            
            safety_backup_id = get_backup_id()
            safety_backup_path = BACKUP_DIR / f"{safety_backup_id}_pre_restore"
            safety_backup_path.mkdir(exist_ok=True)
            logger.info(f"[Backup] Creating safety backup: {safety_backup_id} at {safety_backup_path}")
            copy_directory_fast(WORLD_DIR, safety_backup_path / "world", progress_callback)
            
            # Add safety backup to metadata
            try:
                metadata = load_metadata()
                logger.info(f"[Backup] Loaded metadata, current backup count: {len(metadata.get('backups', []))}")
                # Parse the timestamp from the backup_id (format: backup_YYYY-MM-DD_HH-MM-SS)
                timestamp_str = safety_backup_id.replace('backup_', '').replace('_', ':')
                backup_info = {
                    "id": safety_backup_id,
                    "timestamp": timestamp_str,
                    "description": "Safety backup before restore",
                    "size": get_dir_size(safety_backup_path),
                    "is_safety_backup": True
                }
                metadata["backups"].append(backup_info)
                save_metadata(metadata)
                logger.info(f"[Backup] Safety backup added to metadata: {safety_backup_id}, new total: {len(metadata.get('backups', []))}")
            except Exception as e:
                logger.error(f"[Backup] Failed to add safety backup to metadata: {e}")
                logger.exception("[Backup] Full traceback:")
        
        if progress_callback:
            progress_callback(0.3, "Removing current world...")
        
        # Remove current world directory
        if WORLD_DIR.exists():
            logger.info(f"[Backup] Removing current world directory")
            shutil.rmtree(WORLD_DIR)
        
        if progress_callback:
            progress_callback(0.5, "Restoring world from backup...")
        
        # Copy backup to world directory using fast copy method
        logger.info(f"[Backup] Copying backup to world directory")
        copy_directory_fast(world_backup_path, WORLD_DIR, progress_callback)
        
        if progress_callback:
            progress_callback(0.9, "Finalizing restore...")
        
        logger.info(f"[Backup] Restore completed successfully: {backup_id}")
        
        if progress_callback:
            progress_callback(1.0, "Restore complete")
        
        logger.info(f"[Backup] Restore operation finished for backup: {backup_id}")
        
        return {
            "success": True,
            "backup_id": backup_id,
            "message": "World restored successfully"
        }
        
    except Exception as e:
        logger.error(f"[Backup] Failed to restore backup: {e}")
        
        if progress_callback:
            progress_callback(0.0, f"Restore failed: {str(e)}")
        
        # Try to restore from safety backup if it exists
        safety_backup_path = BACKUP_DIR / f"{safety_backup_id}_pre_restore"
        if safety_backup_path.exists():
            logger.info(f"[Backup] Attempting to restore from safety backup: {safety_backup_id}")
            try:
                if WORLD_DIR.exists():
                    shutil.rmtree(WORLD_DIR)
                copy_directory_fast(safety_backup_path / "world", WORLD_DIR, progress_callback)
                logger.info(f"[Backup] Safety backup restored successfully")
            except Exception as safety_error:
                logger.error(f"[Backup] Failed to restore safety backup: {safety_error}")
        
        return {
            "success": False,
            "error": str(e)
        }


def delete_backup(backup_id: str) -> Dict:
    """
    Delete a specific backup.
    
    Args:
        backup_id: ID of the backup to delete
        
    Returns:
        Dict with operation result
    """
    ensure_backup_dir()
    
    backup_path = BACKUP_DIR / backup_id
    if not backup_path.exists():
        return {
            "success": False,
            "error": f"Backup not found: {backup_id}"
        }
    
    try:
        logger.info(f"[Backup] Deleting backup: {backup_id}")
        shutil.rmtree(backup_path)
        
        # Update metadata
        metadata = load_metadata()
        metadata["backups"] = [b for b in metadata["backups"] if b["id"] != backup_id]
        save_metadata(metadata)
        
        logger.info(f"[Backup] Backup deleted successfully: {backup_id}")
        
        return {
            "success": True,
            "backup_id": backup_id,
            "message": "Backup deleted successfully"
        }
        
    except Exception as e:
        logger.error(f"[Backup] Failed to delete backup: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def list_backups() -> Dict:
    """
    List all available backups.
    
    Returns:
        Dict with list of backups
    """
    ensure_backup_dir()
    
    metadata = load_metadata()
    backups = metadata.get("backups", [])
    
    logger.info(f"[Backup] list_backups: Found {len(backups)} backups in metadata")
    for i, backup in enumerate(backups):
        logger.info(f"[Backup] Backup {i}: id={backup['id']}, is_safety_backup={backup.get('is_safety_backup', False)}")
    
    # Scan for any pre_restore folders that exist on disk but aren't in metadata
    existing_backup_ids = {backup["id"] for backup in backups}
    disk_folders = [f.name for f in BACKUP_DIR.iterdir() if f.is_dir()]
    
    for folder in disk_folders:
        if folder.endswith("_pre_restore"):
            backup_id = folder.replace("_pre_restore", "")
            if backup_id not in existing_backup_ids:
                logger.info(f"[Backup] Found orphaned safety backup on disk: {backup_id}, adding to metadata")
                # Parse timestamp from folder name
                timestamp_str = backup_id.replace('backup_', '').replace('_', ':')
                backup_path = BACKUP_DIR / folder
                backup_info = {
                    "id": backup_id,
                    "timestamp": timestamp_str,
                    "description": "Safety backup before restore",
                    "size": get_dir_size(backup_path),
                    "is_safety_backup": True
                }
                backups.append(backup_info)
                logger.info(f"[Backup] Added orphaned safety backup to list: {backup_id}")
    
    # Verify backups still exist on disk
    valid_backups = []
    for backup in backups:
        # Safety backups have _pre_restore suffix in folder name
        backup_path = BACKUP_DIR / (backup["id"] + "_pre_restore") if backup.get("is_safety_backup") else BACKUP_DIR / backup["id"]
        logger.info(f"[Backup] Checking backup {backup['id']} at path {backup_path}, exists: {backup_path.exists()}")
        if backup_path.exists():
            valid_backups.append(backup)
            logger.info(f"[Backup] Backup {backup['id']} is valid, adding to list")
        else:
            logger.warning(f"[Backup] Backup {backup['id']} not found on disk, removing from metadata")
    
    # Update metadata if any backups were removed or added
    if len(valid_backups) != len(backups):
        metadata["backups"] = valid_backups
        save_metadata(metadata)
        logger.info(f"[Backup] Updated metadata with {len(valid_backups)} backups")
    
    logger.info(f"[Backup] Returning {len(valid_backups)} valid backups")
    return {
        "success": True,
        "backups": valid_backups,
        "count": len(valid_backups)
    }


def cleanup_old_backups(max_count: int) -> Dict:
    """
    Delete oldest backups if count exceeds max_count.
    
    Args:
        max_count: Maximum number of backups to keep
        
    Returns:
        Dict with operation result
    """
    ensure_backup_dir()
    
    metadata = load_metadata()
    backups = metadata.get("backups", [])
    
    if len(backups) <= max_count:
        return {
            "success": True,
            "deleted_count": 0,
            "message": f"Backup count ({len(backups)}) within limit ({max_count})"
        }
    
    # Sort by timestamp (oldest first) and delete excess
    backups_sorted = sorted(backups, key=lambda x: x["timestamp"])
    to_delete = backups_sorted[:len(backups) - max_count]
    
    logger.info(f"[Backup] Auto-delete: Total backups={len(backups)}, max_count={max_count}, deleting={len(to_delete)} backups")
    for i, backup in enumerate(to_delete):
        backup_type = "safety" if backup.get("is_safety_backup") else "regular"
        logger.info(f"[Backup] Auto-delete: Deleting {backup_type} backup {backup['id']} ({backup['timestamp']})")
        
        # Handle safety backups with _pre_restore suffix
        backup_id = backup["id"]
        if backup.get("is_safety_backup"):
            backup_id = backup_id + "_pre_restore"
        result = delete_backup(backup_id)
        if result["success"]:
            deleted_count += 1
            logger.info(f"[Backup] Auto-delete: Successfully deleted {backup_type} backup {backup['id']}")
        else:
            logger.error(f"[Backup] Failed to delete old backup {backup['id']}: {result.get('error')}")
    
    logger.info(f"[Backup] Cleanup completed: deleted {deleted_count} old backups")
    
    return {
        "success": True,
        "deleted_count": deleted_count,
        "message": f"Deleted {deleted_count} old backups"
    }


def calculate_next_backup_time(duration_hours: int, duration_days: int, last_run: Optional[float] = None) -> Optional[float]:
    """
    Calculate the timestamp for the next automatic backup.
    
    Args:
        duration_hours: Hours between backups
        duration_days: Days between backups
        last_run: Timestamp of last backup run
        
    Returns:
        Timestamp of next backup or None if disabled
    """
    if duration_hours == 0 and duration_days == 0:
        return None
    
    total_seconds = (duration_hours * 3600) + (duration_days * 86400)
    
    if total_seconds <= 0:
        return None
    
    if last_run is None:
        last_run = time.time()
    
    return last_run + total_seconds


def backup_scheduler_worker(backup_settings: Dict) -> None:
    """
    Background worker for automatic backup scheduling.
    
    Args:
        backup_settings: Dictionary with backup settings
    """
    global backup_scheduler_running
    
    logger.info("[Backup] Auto-backup scheduler started")
    
    while backup_scheduler_running:
        try:
            # Load current settings
            if not backup_settings.get("backup_auto_enabled", False):
                time.sleep(60)
                continue
            
            # Calculate next backup time
            last_run = backup_settings.get("backup_last_run")
            duration_hours = backup_settings.get("backup_duration_hours", 24)
            duration_days = backup_settings.get("backup_duration_days", 0)
            
            next_backup = calculate_next_backup_time(duration_hours, duration_days, last_run)
            
            if next_backup is None:
                time.sleep(60)
                continue
            
            # Check if it's time for backup
            current_time = time.time()
            if current_time >= next_backup:
                logger.info("[Backup] Auto-backup triggered")
                
                # Wait for server to close if it's running
                if server_status.state != ServerState.IDLE:
                    logger.info("[Backup] Server is running, waiting for it to close before backup...")
                    wait_start = time.time()
                    max_wait_time = 3600  # Wait max 1 hour for server to close
                    
                    while server_status.state != ServerState.IDLE:
                        if time.time() - wait_start > max_wait_time:
                            logger.warning("[Backup] Waited too long for server to close, skipping this backup")
                            # Reset next backup time to retry soon
                            backup_settings["backup_last_run"] = time.time() - (duration_hours * 3600 + duration_days * 86400) / 2
                            break
                        time.sleep(30)  # Check every 30 seconds
                    
                    if server_status.state != ServerState.IDLE:
                        continue  # Skip this backup cycle
                
                # Create backup
                result = create_backup("Automatic backup")
                
                if result["success"]:
                    # Update last run time
                    backup_settings["backup_last_run"] = time.time()
                    
                    # Cleanup old backups if enabled
                    if backup_settings.get("backup_auto_delete_enabled", False):
                        max_count = backup_settings.get("backup_max_count", 10)
                        cleanup_old_backups(max_count)
                else:
                    logger.error(f"[Backup] Auto-backup failed: {result.get('error')}")
            
            # Sleep for 1 minute before checking again
            time.sleep(60)
            
        except Exception as e:
            logger.error(f"[Backup] Scheduler error: {e}")
            time.sleep(60)
    
    logger.info("[Backup] Auto-backup scheduler stopped")


def start_auto_backup_scheduler(backup_settings: Dict) -> None:
    """
    Start the automatic backup scheduler.
    
    Args:
        backup_settings: Dictionary with backup settings
    """
    global backup_scheduler_running, backup_scheduler_thread
    
    if backup_scheduler_running:
        logger.warning("[Backup] Scheduler already running")
        return
    
    backup_scheduler_running = True
    backup_scheduler_thread = threading.Thread(
        target=backup_scheduler_worker,
        args=(backup_settings,),
        daemon=True
    )
    backup_scheduler_thread.start()
    logger.info("[Backup] Auto-backup scheduler started")


def stop_auto_backup_scheduler() -> None:
    """Stop the automatic backup scheduler."""
    global backup_scheduler_running
    
    backup_scheduler_running = False
    if backup_scheduler_thread:
        backup_scheduler_thread.join(timeout=5)
    logger.info("[Backup] Auto-backup scheduler stopped")
