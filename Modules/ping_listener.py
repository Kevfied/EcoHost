"""
Ping Listener - Wake-on-connect for the Minecraft server.

While the server is IDLE and app_settings.auto_start_on_ping is on, this
module holds a temporary TCP listener on the Minecraft port. The first
inbound connection (a client ping/join attempt) releases the socket and
starts the real server, which binds the port itself during boot.

Respects maintenance mode: never auto-starts while maintenance_mode is on.
Managed by log_watcher via sync() on every monitor tick.
"""

import asyncio
import logging
import socket
import threading
import time
from typing import Optional

from .config import MINECRAFT_PORT
from . import config
from .models import ServerState, server_status, app_settings

logger = logging.getLogger(__name__)

_sock: socket.socket | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()
_bind_error_logged = False
_suppress_until = 0.0  # post-wake cooldown: don't rebind while java takes the port


def is_running() -> bool:
    """True while the wake listener holds the MC port."""
    return _sock is not None


def _release_socket() -> None:
    global _sock
    sock, _sock = _sock, None
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def _recv_varint(conn: socket.socket) -> Optional[int]:
    """Read one VarInt off an accepted socket. None on EOF/timeout/garbage."""
    num = 0
    for i in range(5):
        try:
            b = conn.recv(1)
        except (socket.timeout, OSError):
            return None
        if not b:
            return None
        num |= (b[0] & 0x7F) << (7 * i)
        if not (b[0] & 0x80):
            return num
    return None


def _is_login_attempt(conn: socket.socket) -> bool:
    """Peek the MC handshake; True only for a real join (next_state 2 = login
    or 3 = transfer).

    Status pings (next_state == 1, i.e. server-list refreshes), port probes
    and our own health checks connect but send nothing — all return False.
    """
    try:
        conn.settimeout(1.5)
        if _recv_varint(conn) is None:          # packet length
            return False
        if _recv_varint(conn) != 0:             # packet id (handshake = 0x00)
            return False
        if _recv_varint(conn) is None:          # protocol version
            return False
        addr_len = _recv_varint(conn)
        if addr_len is None or addr_len > 255:
            return False
        to_skip = addr_len + 2                  # address string + port ushort
        while to_skip > 0:
            chunk = conn.recv(to_skip)
            if not chunk:
                return False
            to_skip -= len(chunk)
        return _recv_varint(conn) in (2, 3)     # next_state: 1=status, 2=login, 3=transfer
    except (socket.timeout, OSError):
        return False


def _trigger_start() -> None:
    """Run the real start path off the listener thread."""
    try:
        from .server_control import console_controller
        from .log_watcher import set_windows_power_mode
        from .player_sessions import record_server_start

        set_windows_power_mode("high_performance")
        if asyncio.run(console_controller.start()):
            record_server_start()
            logger.info("[PingListener] Server auto-started by inbound ping")
        else:
            logger.warning("[PingListener] Auto-start failed - server window did not appear")
    except Exception as e:
        logger.error(f"[PingListener] Auto-start error: {e}")


def _listen_loop() -> None:
    global _bind_error_logged, _sock
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", MINECRAFT_PORT))
        sock.listen(4)
        sock.settimeout(1.0)
    except OSError as e:
        if not _bind_error_logged:
            logger.warning(f"[PingListener] Cannot bind :{MINECRAFT_PORT} for auto-start: {e}")
            _bind_error_logged = True
        config.ping_listener_running = False
        return

    _bind_error_logged = False
    with _lock:
        _sock = sock
    config.ping_listener_running = True
    logger.info(f"[PingListener] Listening on :{MINECRAFT_PORT} - first ping auto-starts the server")

    woke = False
    try:
        while config.ping_listener_running:
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if _is_login_attempt(conn):
                logger.info(f"[PingListener] Login attempt from {addr[0]} - waking server")
                conn.close()
                woke = True
                break
            conn.close()  # status ping / probe — keep listening
    finally:
        _release_socket()
        config.ping_listener_running = False

    if woke and server_status.state == ServerState.IDLE:
        globals()["_suppress_until"] = time.time() + 30
        _trigger_start()


def stop() -> None:
    """Shut down the wake listener and release the port."""
    config.ping_listener_running = False
    _release_socket()
    thread = _thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)


def sync() -> None:
    """Ensure the listener runs exactly while the server is IDLE and eligible.

    Called every monitor tick by log_watcher. Starts the listener when the
    server is IDLE + auto_start_on_ping + not in maintenance; stops it as
    soon as the server leaves IDLE or eligibility ends.
    """
    global _thread

    eligible = (
        server_status.state == ServerState.IDLE
        and app_settings.auto_start_on_ping
        and not app_settings.maintenance_mode
        and time.time() >= max(_suppress_until, config.ping_suppress_until)
    )

    if eligible:
        if not is_running() and (_thread is None or not _thread.is_alive()):
            config.ping_listener_running = False  # cleared again once loop confirms bind
            _thread = threading.Thread(target=_listen_loop, name="ping-listener", daemon=True)
            _thread.start()
    else:
        if is_running() or config.ping_listener_running:
            stop()
