"""
Player Data Query System - NBT parsing and live player data queries.
"""

import asyncio
import json
import re
import struct
import time
from pathlib import Path

import logging

from .config import SERVER_DIR
from .models import ServerState, server_status, log_lines, log_lock
from .server_control import console_controller
from .player_sessions import player_sessions

logger = logging.getLogger(__name__)

# Cache for player data queries (to avoid spamming commands)
player_data_cache = {}
PLAYER_DATA_CACHE_TTL = 5  # seconds

# Window automation availability
try:
    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False


class SimpleNBTParser:
    """Simple NBT parser for reading Minecraft playerdata files."""
    
    @staticmethod
    def read_playerdat(filepath: Path) -> dict:
        """Read and parse a Minecraft .dat playerdata file."""
        try:
            import gzip
            
            with gzip.open(filepath, 'rb') as f:
                data = f.read()
            
            result = {}
            offset = [0]
            
            def read_tag():
                if offset[0] >= len(data):
                    return None, None
                
                tag_type = data[offset[0]]
                offset[0] += 1
                
                if tag_type == 0:
                    return None, None
                
                name_len = struct.unpack('>H', data[offset[0]:offset[0]+2])[0]
                offset[0] += 2
                name = data[offset[0]:offset[0]+name_len].decode('utf-8')
                offset[0] += name_len
                
                if tag_type == 1:
                    value = struct.unpack('>b', data[offset[0]:offset[0]+1])[0]
                    offset[0] += 1
                elif tag_type == 2:
                    value = struct.unpack('>h', data[offset[0]:offset[0]+2])[0]
                    offset[0] += 2
                elif tag_type == 3:
                    value = struct.unpack('>i', data[offset[0]:offset[0]+4])[0]
                    offset[0] += 4
                elif tag_type == 4:
                    value = struct.unpack('>q', data[offset[0]:offset[0]+8])[0]
                    offset[0] += 8
                elif tag_type == 5:
                    value = struct.unpack('>f', data[offset[0]:offset[0]+4])[0]
                    offset[0] += 4
                elif tag_type == 6:
                    value = struct.unpack('>d', data[offset[0]:offset[0]+8])[0]
                    offset[0] += 8
                elif tag_type == 7:
                    arr_len = struct.unpack('>i', data[offset[0]:offset[0]+4])[0]
                    offset[0] += 4
                    value = list(data[offset[0]:offset[0]+arr_len])
                    offset[0] += arr_len
                elif tag_type == 8:
                    str_len = struct.unpack('>H', data[offset[0]:offset[0]+2])[0]
                    offset[0] += 2
                    value = data[offset[0]:offset[0]+str_len].decode('utf-8')
                    offset[0] += str_len
                elif tag_type == 9:
                    list_type = data[offset[0]]
                    offset[0] += 1
                    list_len = struct.unpack('>i', data[offset[0]:offset[0]+4])[0]
                    offset[0] += 4
                    value = []
                    for _ in range(list_len):
                        if list_type == 10:
                            compound = {}
                            while True:
                                tag_name, tag_value = read_tag()
                                if tag_name is None:
                                    break
                                compound[tag_name] = tag_value
                            value.append(compound)
                        elif list_type == 1:
                            value.append(struct.unpack('>b', data[offset[0]:offset[0]+1])[0])
                            offset[0] += 1
                        elif list_type == 3:
                            value.append(struct.unpack('>i', data[offset[0]:offset[0]+4])[0])
                            offset[0] += 4
                        else:
                            break
                elif tag_type == 10:
                    value = {}
                    while True:
                        tag_name, tag_value = read_tag()
                        if tag_name is None:
                            break
                        value[tag_name] = tag_value
                elif tag_type == 11:
                    arr_len = struct.unpack('>i', data[offset[0]:offset[0]+4])[0]
                    offset[0] += 4
                    value = list(struct.unpack(f'>{arr_len}i', data[offset[0]:offset[0]+arr_len*4]))
                    offset[0] += arr_len * 4
                elif tag_type == 12:
                    arr_len = struct.unpack('>i', data[offset[0]:offset[0]+4])[0]
                    offset[0] += 4
                    value = list(struct.unpack(f'>{arr_len}q', data[offset[0]:offset[0]+arr_len*8]))
                    offset[0] += arr_len * 8
                else:
                    value = None
                
                return name, value
            
            while offset[0] < len(data):
                name, value = read_tag()
                if name is None:
                    break
                result[name] = value
            
            return result
            
        except Exception as e:
            logger.error(f"[NBT] Failed to parse playerdat: {e}")
            return {}


def parse_nbt_inventory(inventory_list: list) -> list:
    """Parse NBT inventory list to standard format."""
    items = []
    armor_slots = [100, 101, 102, 103]
    
    logger.debug(f"[InventoryParser] Parsing {len(inventory_list)} NBT items")
    
    for item in inventory_list:
        if not isinstance(item, dict):
            continue
        
        slot = item.get('Slot', 0)
        item_id = item.get('id', 'unknown')
        count = item.get('Count', 1)
        
        if isinstance(item_id, str):
            item_id = item_id.replace('minecraft:', '')
        
        if slot in armor_slots:
            armor_names = {103: 'Helmet', 102: 'Chestplate', 101: 'Leggings', 100: 'Boots'}
            logger.info(f"[InventoryParser] Armor slot {slot} ({armor_names.get(slot, 'Unknown')}): {item_id} x{count}")
        
        items.append({
            'slot': slot,
            'id': item_id,
            'count': count
        })
    
    logger.info(f"[InventoryParser] Parsed {len(items)} items total")
    return items


def read_offline_player_data(username: str) -> dict:
    """Read player data from playerdata NBT file when server is offline."""
    try:
        player_uuid = None
        
        usercache_file = SERVER_DIR / "usercache.json"
        if usercache_file.exists():
            try:
                with open(usercache_file, 'r', encoding='utf-8') as f:
                    usercache = json.load(f)
                    for entry in usercache:
                        if entry.get("name", "").lower() == username.lower():
                            player_uuid = entry.get("uuid")
                            break
            except:
                pass
        
        if not player_uuid:
            return {
                "username": username,
                "online": False,
                "gamemode": None,
                "level": None,
                "xp": 0,
                "health": None,
                "food": None,
                "inventory": [],
                "position": None,
                "dimension": None,
                "source": "offline_no_uuid"
            }
        
        playerdata_file = SERVER_DIR / "world" / "playerdata" / f"{player_uuid}.dat"
        
        if not playerdata_file.exists():
            return {
                "username": username,
                "online": False,
                "gamemode": None,
                "level": None,
                "xp": 0,
                "health": None,
                "food": None,
                "inventory": [],
                "position": None,
                "dimension": None,
                "source": "offline_no_file"
            }
        
        nbt_data = SimpleNBTParser.read_playerdat(playerdata_file)
        
        if not nbt_data:
            return {
                "username": username,
                "online": False,
                "gamemode": None,
                "level": None,
                "xp": 0,
                "health": None,
                "food": None,
                "inventory": [],
                "position": None,
                "dimension": None,
                "source": "offline_parse_failed"
            }
        
        gamemode_id = nbt_data.get('playerGameType', 0)
        gm_map = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
        gamemode = gm_map.get(gamemode_id, "unknown")
        
        level = nbt_data.get('XpLevel', 0)
        health = nbt_data.get('Health', 20.0)
        if isinstance(health, int):
            health = float(health)
        food = nbt_data.get('foodLevel', 20)
        
        pos = nbt_data.get('Pos', [0.0, 0.0, 0.0])
        if isinstance(pos, list) and len(pos) >= 3:
            position = {
                "x": round(float(pos[0]), 1),
                "y": round(float(pos[1]), 1),
                "z": round(float(pos[2]), 1)
            }
        else:
            position = {"x": 0, "y": 0, "z": 0}
        
        dimension = nbt_data.get('Dimension', 'minecraft:overworld')
        inventory_raw = nbt_data.get('Inventory', [])
        inventory = parse_nbt_inventory(inventory_raw)
        
        return {
            "username": username,
            "online": False,
            "gamemode": gamemode,
            "level": level,
            "xp": nbt_data.get('XpP', 0),
            "health": health,
            "food": food,
            "inventory": inventory,
            "position": position,
            "dimension": dimension,
            "uuid": player_uuid,
            "source": "offline_playerdat"
        }
        
    except Exception as e:
        logger.error(f"[PlayerData] Failed to read offline data for {username}: {e}")
        logger.exception("[PlayerData] Full traceback:")
        return {"error": f"Failed to read offline data: {e}"}


def has_following_entity_data(line: str, username: str) -> bool:
    """Check if log line indicates player has entity data (is online)."""
    target = f"{username} has the following entity data"
    return target.lower() in line.lower()


def is_offline_response(line: str, username: str, escaped_name: str) -> bool:
    """Check if log line indicates player is offline/not found."""
    if "No entity was found" in line:
        return username.lower() in line.lower() or escaped_name.lower() in line.lower()
    return False


def parse_live_player_data(username: str, log_line: str) -> dict:
    """Parse player data from server log output."""
    response_data = {
        "username": username,
        "online": True,
        "gamemode": None,
        "level": 0,
        "xp": 0,
        "health": 20,
        "food": 20,
        "inventory": [],
        "position": {"x": 0, "y": 0, "z": 0},
        "dimension": "minecraft:overworld",
        "source": "live_query"
    }
    
    try:
        nbt_start = log_line.find("{")
        if nbt_start > 0:
            nbt_data = log_line[nbt_start:]
            
            if "playerGameType" in nbt_data:
                gm_match = re.search(r'playerGameType:\s*(\d+)', nbt_data)
                if gm_match:
                    gm_map = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
                    response_data["gamemode"] = gm_map.get(int(gm_match.group(1)), "unknown")
            
            if "XpLevel" in nbt_data:
                xp_match = re.search(r'XpLevel:\s*(\d+)', nbt_data)
                if xp_match:
                    response_data["level"] = int(xp_match.group(1))
            
            if "Health" in nbt_data:
                health_match = re.search(r'Health:\s*(\d+\.?\d*)', nbt_data)
                if health_match:
                    response_data["health"] = float(health_match.group(1))
            
            if "foodLevel" in nbt_data:
                food_match = re.search(r'foodLevel:\s*(\d+)', nbt_data)
                if food_match:
                    response_data["food"] = int(food_match.group(1))
            
            pos_match = re.search(r'Pos:\s*\[(-?\d+\.?\d*)d,\s*(-?\d+\.?\d*)d,\s*(-?\d+\.?\d*)d\]', nbt_data)
            if pos_match:
                response_data["position"] = {
                    "x": round(float(pos_match.group(1)), 1),
                    "y": round(float(pos_match.group(2)), 1),
                    "z": round(float(pos_match.group(3)), 1)
                }
            
            dim_match = re.search(r'Dimension:\s*"([^"]+)"', nbt_data)
            if dim_match:
                response_data["dimension"] = dim_match.group(1)
            
            inv_match = re.search(r'Inventory:\s*\[(.*?)\]', nbt_data, re.DOTALL)
            if inv_match:
                inv_items = []
                inv_content = inv_match.group(1)
                
                brace_depth = 0
                current_item = ""
                in_item = False
                
                for char in inv_content:
                    if char == '{':
                        brace_depth += 1
                        if brace_depth == 1:
                            in_item = True
                        current_item += char
                    elif char == '}':
                        current_item += char
                        brace_depth -= 1
                        if brace_depth == 0 and in_item:
                            entry = current_item
                            
                            slot_match = re.search(r'Slot:\s*(\d+)[bs]?', entry)
                            if not slot_match:
                                current_item = ""
                                in_item = False
                                continue
                            slot = int(slot_match.group(1))
                            
                            id_match = re.search(r'id:\s*"([^"]+)"', entry)
                            if not id_match:
                                current_item = ""
                                in_item = False
                                continue
                            item_id = id_match.group(1).replace("minecraft:", "")
                            
                            count_match = re.search(r'Count:\s*(\d+)[bs]?', entry)
                            count = int(count_match.group(1)) if count_match else 1
                            
                            inv_items.append({
                                "slot": slot,
                                "id": item_id,
                                "count": count
                            })
                            
                            current_item = ""
                            in_item = False
                    elif in_item:
                        current_item += char
                
                response_data["inventory"] = inv_items
                
            logger.info(f"[PlayerData] Parsed {username}: online={response_data['online']}, gamemode={response_data['gamemode']}, health={response_data['health']}, inventory={len(response_data['inventory'])} items")
    
    except Exception as e:
        logger.error(f"[PlayerData] Failed to parse live data: {e}")
        logger.error(f"[PlayerData] Log line snippet: {log_line[:200] if len(log_line) > 200 else log_line}")
    
    return response_data


async def query_minecraft_player_data(username: str) -> dict:
    """Query player data from Minecraft server or offline files."""
    query_start_time = time.time()
    debug_info = {
        "username": username,
        "server_state": str(server_status.state),
        "is_running": False,
        "pywinauto": PYWINAUTO_AVAILABLE,
        "command_sent": False,
        "logs_checked": 0,
        "found_response": None,
        "fallback_reason": None
    }
    
    is_running = server_status.state == ServerState.ACTIVE and console_controller.is_console_running()
    debug_info["is_running"] = is_running
    
    if is_running and PYWINAUTO_AVAILABLE:
        try:
            escaped_name = username.replace(" ", "_")
            command = f"data get entity {escaped_name}"
            
            time_before = time.time()
            
            success = await console_controller.send_command(command)
            debug_info["command_sent"] = success
            
            if success:
                await asyncio.sleep(2.5)
                
                with log_lock:
                    recent_logs = log_lines[-200:]
                debug_info["logs_checked"] = len(recent_logs)
                
                found_lines = []
                for i, line in enumerate(reversed(recent_logs)):
                    if username.lower() in line.lower():
                        found_lines.append((i, line[:150]))
                    
                    if has_following_entity_data(line, username):
                        debug_info["found_response"] = "online"
                        debug_info["matched_line"] = line[:200]
                        logger.info(f"[PlayerData] {username} found ONLINE in logs (line #{i})")
                        return parse_live_player_data(username, line)
                    elif is_offline_response(line, username, escaped_name):
                        debug_info["found_response"] = "offline"
                        debug_info["matched_line"] = line[:200]
                        logger.info(f"[PlayerData] {username} found OFFLINE in logs (line #{i})")
                        break
                
                if found_lines and debug_info["found_response"] is None:
                    logger.warning(f"[PlayerData] {username}: Found {len(found_lines)} lines containing username but no match. Lines: {found_lines[:3]}")
                
                if debug_info["found_response"] is None:
                    debug_info["fallback_reason"] = "no_response_in_logs"
                    logger.warning(f"[PlayerData] {username}: No response found in {len(recent_logs)} recent logs")
            else:
                debug_info["fallback_reason"] = "command_failed"
        
        except Exception as e:
            debug_info["fallback_reason"] = f"exception: {e}"
            logger.warning(f"[PlayerData] Live query failed for {username}: {e}")
    else:
        debug_info["fallback_reason"] = f"server_not_running (state={server_status.state}, console={console_controller.is_console_running()})"
    
    logger.info(f"[PlayerData] {username}: Falling back to offline data - {debug_info['fallback_reason']}")
    offline_data = read_offline_player_data(username)
    
    offline_data["_debug"] = debug_info
    
    if username in player_sessions:
        session = player_sessions[username]
        offline_data["session_history"] = {
            "total_playtime_seconds": session.total_playtime_seconds,
            "join_count": session.join_count,
            "first_join": session.first_join,
            "last_seen": session.last_seen
        }
    
    return offline_data
