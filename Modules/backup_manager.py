"""
Backup Manager for MC-EcoHost.
World backup/restore with corruption-safe live copies.

Correctness rules (fixing the original branch implementation):
- Backing up while the server is ACTIVE requires RCON: save-off ->
  save-all flush -> copy -> save-on. Refused outright without RCON
  rather than producing a torn world copy.
- Restore requires the server to be IDLE (checked here AND at endpoint).
- All three dimension folders are covered, not just world/.
- Metadata writes are atomic (tmp + replace) and backup ids are
  validated before they ever touch a path.
"""

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Callable

from Modules.config import SERVER_DIR
from Modules.models import server_status, ServerState, app_settings

logger = logging.getLogger(__name__)

BACKUP_DIR = SERVER_DIR / "backups"
METADATA_FILE = BACKUP_DIR / "metadata.json"

# Every dimension folder a vanilla-compatible server may write.
DIMENSION_DIRS = ("world", "world_nether", "world_the_end")

IS_WINDOWS = platform.system() == "Windows"

_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

backup_scheduler_running = False
backup_scheduler_thread: Optional[threading.Thread] = None


def is_valid_backup_id(backup_id: str) -> bool:
    """Reject anything that isn't a plain folder name (traversal guard)."""
    return bool(_BACKUP_ID_RE.fullmatch(backup_id or ""))


def _dimension_dirs() -> list[Path]:
    """Existing dimension folders under SERVER_DIR."""
    return [SERVER_DIR / d for d in DIMENSION_DIRS if (SERVER_DIR / d).is_dir()]


def copy_directory_fast(src: Path, dst: Path) -> None:
    """Copy a directory tree; robocopy on Windows, shutil elsewhere."""
    if IS_WINDOWS:
        try:
            # /E copy subdirs incl. empty, /Z restartable, /R:5 /W:5 retries,
            # /NP /NFL /NDL quiet. Robocopy exit codes 0-7 are success.
            result = subprocess.run(
                ["robocopy", str(src), str(dst), "/E", "/Z", "/R:5", "/W:5",
                 "/NP", "/NFL", "/NDL"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode <= 7:
                return
            logger.warning(f"[Backup] Robocopy exit {result.returncode}, falling back to shutil")
        except Exception as e:
            logger.warning(f"[Backup] Robocopy failed ({e}), falling back to shutil")
    shutil.copytree(src, dst, dirs_exist_ok=True)


def ensure_backup_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def load_metadata() -> Dict:
    if not METADATA_FILE.exists():
        return {"backups": [], "next_auto_backup": None}
    try:
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("metadata root is not an object")
        data.setdefault("backups", [])
        return data
    except Exception as e:
        logger.error(f"[Backup] Corrupt metadata, starting fresh: {e}")
        return {"backups": [], "next_auto_backup": None}


def save_metadata(metadata: Dict) -> None:
    """Atomic metadata write — never leave a half-written file."""
    try:
        tmp = METADATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        os.replace(tmp, METADATA_FILE)
    except Exception as e:
        logger.error(f"[Backup] Failed to save metadata: {e}")


def get_backup_id(timestamp: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(timestamp or time.time())
    return f"backup_{dt.strftime('%Y-%m-%d_%H-%M-%S')}"


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def get_dir_size(path: Path) -> int:
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for name in filenames:
                fp = Path(dirpath) / name
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
    except Exception as e:
        logger.error(f"[Backup] get_dir_size failed: {e}")
    return total


def _record_backup(metadata_id: str, description: str, backup_path: Path,
                   is_safety: bool = False) -> None:
    metadata = load_metadata()
    metadata["backups"].append({
        "id": metadata_id,
        "timestamp": time.time(),
        "description": description,
        "size": get_dir_size(backup_path),
        "size_human": format_size(get_dir_size(backup_path)),
        "is_safety_backup": is_safety,
    })
    save_metadata(metadata)


def _with_save_lock(fn) -> Dict:
    """Run fn() between save-off/save-on when the server is live.

    Requires RCON when ACTIVE. Without it we refuse rather than copy a
    world that is mid-write.
    """
    from .rcon_client import send_rcon_command, is_rcon_available

    live = server_status.state == ServerState.ACTIVE
    if live and not is_rcon_available():
        return {"success": False,
                "error": "Server is running and RCON is unavailable — refusing to copy a live world"}

    if live:
        try:
            send_rcon_command("save-off")
            send_rcon_command("save-all flush")
        except Exception as e:
            logger.error(f"[Backup] save-off failed: {e}")
            try:
                send_rcon_command("save-on")
            except Exception:
                pass
            return {"success": False, "error": f"Failed to pause world saving: {e}"}
    try:
        return fn()
    finally:
        if live:
            try:
                send_rcon_command("save-on")
            except Exception as e:
                logger.error(f"[Backup] save-on failed: {e}")


def create_backup(description: str = "Manual backup",
                  progress_callback: Optional[Callable[[float, str], None]] = None) -> Dict:
    """Create a backup of every existing dimension folder."""
    ensure_backup_dir()

    dims = _dimension_dirs()
    if not dims:
        return {"success": False, "error": f"No world folders found under {SERVER_DIR}"}

    backup_id = get_backup_id()
    backup_path = BACKUP_DIR / backup_id

    def _do_copy() -> Dict:
        try:
            backup_path.mkdir(exist_ok=True)
            sizes = {d: get_dir_size(d) for d in dims}  # single walk, not two
            total = max(sum(sizes.values()), 1)
            copied = 0
            for dim in dims:
                copied += sizes[dim]
                if progress_callback:
                    progress_callback(min(0.05 + 0.9 * copied / total, 0.95),
                                      f"Copying {dim.name}...")
                copy_directory_fast(dim, backup_path / dim.name)
            _record_backup(backup_id, description, backup_path)
            if progress_callback:
                progress_callback(1.0, "Backup complete")
            return {"success": True, "backup_id": backup_id,
                    "message": f"Backup created: {backup_id}",
                    "size": get_dir_size(backup_path)}
        except Exception as e:
            logger.error(f"[Backup] Failed: {e}")
            try:
                shutil.rmtree(backup_path, ignore_errors=True)
            except Exception:
                pass
            return {"success": False, "error": str(e)}

    return _with_save_lock(_do_copy)


def restore_backup(backup_id: str,
                   progress_callback: Optional[Callable[[float, str], None]] = None) -> Dict:
    """Restore world folders from a backup. Server MUST be idle."""
    ensure_backup_dir()

    if server_status.state != ServerState.IDLE:
        return {"success": False,
                "error": "Stop the server before restoring a backup"}

    if not is_valid_backup_id(backup_id):
        return {"success": False, "error": "Invalid backup id"}

    backup_path = BACKUP_DIR / backup_id
    if not backup_path.is_dir():
        return {"success": False, "error": f"Backup not found: {backup_id}"}

    dims_in_backup = [backup_path / d for d in DIMENSION_DIRS
                      if (backup_path / d).is_dir()]
    if not dims_in_backup:
        return {"success": False, "error": f"No world folders inside backup {backup_id}"}

    # Safety copy of current dims before destructive restore
    safety_id = get_backup_id() + "_pre_restore"
    safety_path = BACKUP_DIR / safety_id
    try:
        if progress_callback:
            progress_callback(0.1, "Creating safety backup of current world...")
        current_dims = _dimension_dirs()
        if current_dims:
            safety_path.mkdir(exist_ok=True)
            for dim in current_dims:
                copy_directory_fast(dim, safety_path / dim.name)
            _record_backup(safety_id, "Safety backup before restore", safety_path, is_safety=True)

        if progress_callback:
            progress_callback(0.4, "Restoring world folders...")
        for dim_path in dims_in_backup:
            target = SERVER_DIR / dim_path.name
            if target.exists():
                shutil.rmtree(target)
            copy_directory_fast(dim_path, target)

        if progress_callback:
            progress_callback(1.0, "Restore complete")
        logger.info(f"[Backup] Restored {backup_id}")
        return {"success": True, "backup_id": backup_id,
                "message": "World restored successfully"}

    except Exception as e:
        logger.error(f"[Backup] Restore failed: {e} — attempting safety rollback")
        try:
            if safety_path.is_dir():
                for dim in safety_path.iterdir():
                    if dim.is_dir():
                        target = SERVER_DIR / dim.name
                        if target.exists():
                            shutil.rmtree(target)
                        copy_directory_fast(dim, target)
                logger.info("[Backup] Safety rollback applied")
        except Exception as e2:
            logger.error(f"[Backup] Safety rollback failed: {e2}")
        return {"success": False, "error": str(e)}


def delete_backup(backup_id: str) -> Dict:
    """Delete a backup folder + its metadata entry.

    Accepts either the metadata id or the on-disk folder name
    (safety backups live at <id>_pre_restore).
    """
    ensure_backup_dir()

    if not is_valid_backup_id(backup_id):
        return {"success": False, "error": "Invalid backup id"}

    backup_path = BACKUP_DIR / backup_id
    if not backup_path.exists():
        return {"success": False, "error": f"Backup not found: {backup_id}"}

    try:
        shutil.rmtree(backup_path)
        metadata = load_metadata()
        # Match either the plain id or a safety entry's base id
        base = backup_id[:-len("_pre_restore")] if backup_id.endswith("_pre_restore") else backup_id
        metadata["backups"] = [
            b for b in metadata["backups"]
            if b.get("id") not in (backup_id, base)
        ]
        save_metadata(metadata)
        logger.info(f"[Backup] Deleted {backup_id}")
        return {"success": True, "backup_id": backup_id,
                "message": "Backup deleted"}
    except Exception as e:
        logger.error(f"[Backup] Delete failed: {e}")
        return {"success": False, "error": str(e)}


def list_backups() -> Dict:
    """List backups, reconciling metadata against folders on disk."""
    ensure_backup_dir()
    metadata = load_metadata()
    backups = metadata.get("backups", [])

    def folder_for(entry: Dict) -> Path:
        bid = entry["id"]
        if entry.get("is_safety_backup") and not bid.endswith("_pre_restore"):
            bid = bid + "_pre_restore"
        return BACKUP_DIR / bid

    # Pick up folders on disk with no metadata entry
    known = set()
    for b in backups:
        known.add(folder_for(b).name)
    for f in BACKUP_DIR.iterdir():
        if f.is_dir() and f.name not in known:
            logger.info(f"[Backup] Found untracked folder {f.name}, adding to metadata")
            entry = {
                "id": f.name,
                "timestamp": f.stat().st_mtime,
                "description": "Untracked backup",
                "size": get_dir_size(f),
                "size_human": format_size(get_dir_size(f)),
                "is_safety_backup": f.name.endswith("_pre_restore"),
            }
            backups.append(entry)
            known.add(f.name)

    # Drop entries whose folder vanished
    valid = [b for b in backups if folder_for(b).exists()]
    if len(valid) != len(metadata.get("backups", [])) or len(valid) != len(backups):
        metadata["backups"] = valid
        save_metadata(metadata)

    for b in valid:
        b["size_human"] = b.get("size_human") or format_size(b.get("size", 0))

    return {"success": True, "backups": valid, "count": len(valid)}


def cleanup_old_backups(max_count: int) -> Dict:
    """Delete oldest backups beyond max_count (safety backups exempt)."""
    ensure_backup_dir()
    metadata = load_metadata()
    backups = metadata.get("backups", [])
    deleted_count = 0

    regular = [b for b in backups if not b.get("is_safety_backup")]
    if len(regular) <= max_count:
        return {"success": True, "deleted_count": 0,
                "message": f"Backup count ({len(regular)}) within limit ({max_count})"}

    regular.sort(key=lambda b: b.get("timestamp", 0))
    for backup in regular[:len(regular) - max_count]:
        folder = backup["id"] + ("_pre_restore" if backup.get("is_safety_backup") else "")
        result = delete_backup(folder)
        if result["success"]:
            deleted_count += 1
        else:
            logger.error(f"[Backup] Cleanup failed for {backup['id']}: {result.get('error')}")

    return {"success": True, "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} old backups"}


def calculate_next_backup_time(duration_hours: int, duration_days: int,
                               last_run: Optional[float] = None) -> Optional[float]:
    total_seconds = duration_hours * 3600 + duration_days * 86400
    if total_seconds <= 0:
        return None
    return (last_run or time.time()) + total_seconds


def backup_scheduler_worker() -> None:
    """Auto-backup loop; create_backup itself handles live-safe copies."""
    global backup_scheduler_running
    logger.info("[Backup] Auto-backup scheduler started")

    while backup_scheduler_running:
        try:
            if not (app_settings.backup_enabled and app_settings.backup_auto_enabled):
                time.sleep(60)
                continue

            next_backup = calculate_next_backup_time(
                app_settings.backup_duration_hours,
                app_settings.backup_duration_days,
                app_settings.backup_last_run,
            )
            if next_backup is None or time.time() < next_backup:
                time.sleep(60)
                continue

            if server_status.backup_in_progress:
                time.sleep(60)
                continue

            logger.info("[Backup] Auto-backup triggered")
            server_status.backup_in_progress = True
            server_status.backup_operation = "backup"
            try:
                result = create_backup("Automatic backup")
            finally:
                server_status.backup_in_progress = False
                server_status.backup_operation = None
                server_status.backup_progress = 0.0
                server_status.backup_status_message = ""

            if result["success"]:
                app_settings.backup_last_run = time.time()
                from .models import save_settings_to_file
                save_settings_to_file()
                if app_settings.backup_auto_delete_enabled:
                    cleanup_old_backups(app_settings.backup_max_count)
            else:
                logger.error(f"[Backup] Auto-backup failed: {result.get('error')}")
                # Retry in an hour rather than hot-looping on a persistent failure
                app_settings.backup_last_run = time.time()

            time.sleep(60)
        except Exception as e:
            logger.error(f"[Backup] Scheduler error: {e}")
            time.sleep(60)

    logger.info("[Backup] Auto-backup scheduler stopped")


def start_auto_backup_scheduler() -> None:
    global backup_scheduler_running, backup_scheduler_thread
    if backup_scheduler_running:
        return
    backup_scheduler_running = True
    backup_scheduler_thread = threading.Thread(
        target=backup_scheduler_worker, name="backup-scheduler", daemon=True)
    backup_scheduler_thread.start()
    logger.info("[Backup] Scheduler thread started")


def stop_auto_backup_scheduler() -> None:
    global backup_scheduler_running
    backup_scheduler_running = False
    if backup_scheduler_thread and backup_scheduler_thread.is_alive():
        backup_scheduler_thread.join(timeout=5)
