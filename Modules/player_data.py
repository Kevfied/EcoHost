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

from .config import SERVER_DIR, CONFIG_FILE
from .models import ServerState, server_status, log_lines, log_lock, PlayerDataResponse
from .server_control import console_controller
from .player_sessions import player_sessions

logger = logging.getLogger(__name__)

# Cache TTL configuration
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
                try:
                    name = data[offset[0]:offset[0]+name_len].decode('utf-8')
                except UnicodeDecodeError:
                    # Handle corrupted or binary data in tag names
                    try:
                        name = data[offset[0]:offset[0]+name_len].decode('utf-8', errors='replace')
                    except Exception:
                        name = data[offset[0]:offset[0]+name_len].hex()
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
                    try:
                        value = data[offset[0]:offset[0]+str_len].decode('utf-8')
                    except UnicodeDecodeError:
                        # Handle corrupted or binary data in strings
                        try:
                            # Try with error handling
                            value = data[offset[0]:offset[0]+str_len].decode('utf-8', errors='replace')
                        except Exception:
                            # If still fails, use hex representation
                            value = data[offset[0]:offset[0]+str_len].hex()
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
            
        except UnicodeDecodeError as e:
            logger.error(f"[NBT] UTF-8 decoding error in playerdat: {e}")
            logger.debug(f"[NBT] Error position: {e.start}, data sample: {data[e.start-10:e.start+10].hex() if e.start > 10 else data[:20].hex()}")
            return {}
        except Exception as e:
            logger.error(f"[NBT] Failed to parse playerdat: {e}")
            logger.debug(f"[NBT] Data length: {len(data)}, first 50 bytes: {data[:50].hex()}")
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


def parse_nbt_compound_list(content: str) -> list:
    """
    Parse a list of NBT compound tags from inventory content.
    Handles nested structures and various NBT formats reliably.
    """
    items = []
    if not content or content.strip() == '':
        return items
    
    # Parse individual compound items using stack-based brace tracking
    i = 0
    content_len = len(content)
    
    while i < content_len:
        # Skip whitespace and commas between items
        while i < content_len and content[i] in ' \t\n\r,':
            i += 1
        
        if i >= content_len:
            break
        
        # Look for start of compound tag
        if content[i] != '{':
            i += 1
            continue
        
        # Found a compound start, find its matching end using stack
        start_idx = i
        brace_stack = 1
        i += 1
        in_string = False
        string_char = None
        
        while i < content_len and brace_stack > 0:
            char = content[i]
            
            if not in_string:
                if char in '"\'':
                    in_string = True
                    string_char = char
                elif char == '{':
                    brace_stack += 1
                elif char == '}':
                    brace_stack -= 1
            else:
                # In string, look for closing quote (handle escapes)
                if char == '\\' and i + 1 < content_len:
                    i += 1  # Skip escaped character
                elif char == string_char:
                    in_string = False
                    string_char = None
            
            i += 1
        
        if brace_stack == 0:
            # Successfully found a complete compound tag
            compound_str = content[start_idx:i]
            item = parse_nbt_item(compound_str)
            if item:
                items.append(item)
    
    return items


def parse_nbt_item(compound_str: str) -> dict:
    """
    Parse a single NBT compound item string into slot/id/count dict.
    Handles various NBT number formats and nested tags.
    """
    # Extract Slot - handles formats: Slot: 0, Slot: 0b, Slot: 0s
    slot_match = re.search(r'Slot:\s*(-?\d+)[bsl]?', compound_str)
    if not slot_match:
        logger.debug(f"[PlayerData] Skipping item without Slot: {compound_str[:100]}...")
        return None
    
    slot = int(slot_match.group(1))
    
    # Extract id - handles formats: id: "minecraft:stone", id: "stone"
    id_match = re.search(r'id:\s*"([^"]+)"', compound_str)
    if not id_match:
        logger.debug(f"[PlayerData] Skipping item without id: {compound_str[:100]}...")
        return None
    
    item_id = id_match.group(1)
    # Remove minecraft: prefix if present
    if item_id.startswith('minecraft:'):
        item_id = item_id[10:]
    
    # Extract Count - handles formats: Count: 1, Count: 1b, Count: 1s
    # Default to 1 if not found
    count_match = re.search(r'Count:\s*(\d+)[bsl]?', compound_str)
    count = int(count_match.group(1)) if count_match else 1
    
    logger.debug(f"[PlayerData] Parsed NBT item: slot={slot}, id={item_id}, count={count}")
    
    return {
        "slot": slot,
        "id": item_id,
        "count": count
    }


def convert_filedata_to_api_format(username: str, file_data: dict) -> dict:
    """
    Convert NBT data from playerdata file to API format.
    
    Args:
        username: Player username
        file_data: Raw NBT data from SimpleNBTParser
        
    Returns:
        Dictionary in API format with username, online, gamemode, inventory, etc.
    """
    api_data = {
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
        "source": "playerdata_file",
        "error": None
    }
    
    try:
        # Extract gamemode
        if "playerGameType" in file_data:
            gm_map = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
            api_data["gamemode"] = gm_map.get(file_data["playerGameType"], "unknown")
        
        # Extract level
        if "XpLevel" in file_data:
            api_data["level"] = file_data["XpLevel"]
        
        # Extract health
        if "Health" in file_data:
            api_data["health"] = float(file_data["Health"])
        
        # Extract food
        if "foodLevel" in file_data:
            api_data["food"] = file_data["foodLevel"]
        
        # Extract position
        if "Pos" in file_data and isinstance(file_data["Pos"], list) and len(file_data["Pos"]) >= 3:
            api_data["position"] = {
                "x": round(float(file_data["Pos"][0]), 1),
                "y": round(float(file_data["Pos"][1]), 1),
                "z": round(float(file_data["Pos"][2]), 1)
            }
        
        # Extract dimension
        if "Dimension" in file_data:
            api_data["dimension"] = file_data["Dimension"]
        
        # Extract inventory
        if "Inventory" in file_data and isinstance(file_data["Inventory"], list):
            inv_items = []
            for item in file_data["Inventory"]:
                if isinstance(item, dict):
                    slot = item.get("Slot", 0)
                    item_id = item.get("id", "")
                    if item_id.startswith("minecraft:"):
                        item_id = item_id[10:]
                    count = item.get("Count", 1)
                    
                    if item_id:  # Only add if we have an ID
                        inv_items.append({
                            "slot": slot,
                            "id": item_id,
                            "count": count
                        })
            
            api_data["inventory"] = inv_items
            logger.debug(f"[PlayerData] Converted {len(inv_items)} inventory items from file")
        
    except Exception as e:
        logger.error(f"[PlayerData] Failed to convert filedata to API format: {e}")
    
    return api_data


def get_player_uuid(username: str) -> str:
    """
    Get player UUID from various sources.
    
    Args:
        username: Player username
        
    Returns:
        UUID string or None if not found
    """
    try:
        # Try usercache.json first (most reliable) - it's in SERVER_DIR, not world folder
        usercache_path = SERVER_DIR / "usercache.json"
        if usercache_path.exists():
            import json
            with open(usercache_path, 'r') as f:
                data = json.load(f)
                for entry in data:
                    if entry.get('name') == username:
                        player_uuid = entry.get('uuid')
                        logger.info(f"[PlayerData] Found UUID in usercache.json for {username}: {player_uuid}")
                        return player_uuid
        
        # Try RCON query as fallback
        if server_status.state == ServerState.ACTIVE:
            from .commands import send_rcon_command
            response = send_rcon_command(f"data get entity {username} UUID")
            logger.debug(f"[PlayerData] UUID RCON response: {response}")
            if response:
                import re
                # Handle format: [I; 68264093, -724878668, -1170139508, -987008962]
                uuid_match = re.search(r'\[I;\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\]', response)
                if uuid_match:
                    # Convert int array to UUID
                    import uuid
                    most_sig = (int(uuid_match.group(1)) << 32) | (int(uuid_match.group(2)) & 0xFFFFFFFF)
                    least_sig = (int(uuid_match.group(3)) << 32) | (int(uuid_match.group(4)) & 0xFFFFFFFF)
                    player_uuid = str(uuid.UUID(int=most_sig << 64 | least_sig))
                    logger.info(f"[PlayerData] Retrieved UUID via RCON for {username}: {player_uuid}")
                    return player_uuid
        
        # Try searching playerdata files
        world_path = SERVER_DIR / "world"
        playerdata_path = world_path / "playerdata"
        if playerdata_path.exists():
            for dat_file in playerdata_path.glob("*.dat"):
                try:
                    nbt_data = read_playerdata_file_nbtlib_raw(dat_file)
                    if nbt_data and nbt_data.get('Name') == username:
                        return dat_file.stem  # Return UUID from filename
                except Exception:
                    continue
        
        return None
    except Exception as e:
        logger.error(f"[PlayerData] Failed to get UUID for {username}: {e}")
        return None


def read_playerdata_file_nbtlib(username: str) -> dict:
    """
    Read player data using nbtlib library (most reliable method).
    
    Args:
        username: Player username
        
    Returns:
        NBT data dictionary or None if file not found
    """
    try:
        import nbtlib
        
        world_path = SERVER_DIR / "world"
        playerdata_path = world_path / "playerdata"
        
        logger.debug(f"[PlayerData] Checking playerdata path: {playerdata_path}")
        if not playerdata_path.exists():
            logger.warning(f"[PlayerData] Playerdata directory not found: {playerdata_path}")
            return None
        
        # Get player UUID
        logger.debug(f"[PlayerData] Getting UUID for {username}")
        player_uuid = get_player_uuid(username)
        if not player_uuid:
            logger.warning(f"[PlayerData] Could not find UUID for {username}")
            return None
        
        dat_file = playerdata_path / f"{player_uuid}.dat"
        logger.debug(f"[PlayerData] Looking for playerdata file: {dat_file}")
        if not dat_file.exists():
            logger.warning(f"[PlayerData] Playerdata file not found: {dat_file}")
            return None
        
        logger.info(f"[PlayerData] Reading playerdata with nbtlib: {dat_file.name}")
        nbt_data = nbtlib.load(dat_file)
        
        # Convert nbtlib object to dict for easier processing
        logger.debug(f"[PlayerData] Successfully loaded NBT data")
        return dict(nbt_data)
        
    except FileNotFoundError:
        logger.warning(f"[PlayerData] Playerdata file not found for {username}")
        return None
    except Exception as e:
        logger.error(f"[PlayerData] Failed to read playerdata with nbtlib: {e}")
        logger.exception("[PlayerData] Full traceback:")
        return None


def read_playerdata_file_nbtlib_raw(filepath: Path) -> dict:
    """
    Read a playerdata file using nbtlib (helper for UUID search).
    
    Args:
        filepath: Path to .dat file
        
    Returns:
        NBT data dictionary or None
    """
    try:
        import nbtlib
        nbt_data = nbtlib.load(filepath)
        return dict(nbt_data)
    except Exception:
        return None


def convert_nbtlib_to_api_format(username: str, nbt_data: dict) -> dict:
    """
    Convert nbtlib NBT data to API format.
    
    Args:
        username: Player username
        nbt_data: Raw NBT data from nbtlib
        
    Returns:
        Dictionary in API format
    """
    api_data = {
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
        "source": "nbtlib_file",
        "error": None
    }
    
    try:
        # Extract gamemode
        if "playerGameType" in nbt_data:
            gm_map = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
            api_data["gamemode"] = gm_map.get(nbt_data["playerGameType"], "unknown")
        
        # Extract level
        if "XpLevel" in nbt_data:
            api_data["level"] = nbt_data["XpLevel"]
        
        # Extract health
        if "Health" in nbt_data:
            api_data["health"] = float(nbt_data["Health"])
        
        # Extract food
        if "foodLevel" in nbt_data:
            api_data["food"] = nbt_data["foodLevel"]
        
        # Extract position
        if "Pos" in nbt_data:
            pos = nbt_data["Pos"]
            if isinstance(pos, list) and len(pos) >= 3:
                api_data["position"] = {
                    "x": round(float(pos[0]), 1),
                    "y": round(float(pos[1]), 1),
                    "z": round(float(pos[2]), 1)
                }
        
        # Extract dimension
        if "Dimension" in nbt_data:
            api_data["dimension"] = str(nbt_data["Dimension"])
        
        # Extract inventory (main inventory)
        if "Inventory" in nbt_data:
            inv_items = []
            for item in nbt_data["Inventory"]:
                if isinstance(item, dict):
                    slot = item.get("Slot", 0)
                    item_id = item.get("id", "")
                    if item_id.startswith("minecraft:"):
                        item_id = item_id[10:]
                    count = item.get("Count", 1)
                    
                    item_data = {
                        "slot": slot,
                        "id": item_id,
                        "count": count
                    }
                    
                    # Extract enchantments
                    if "tag" in item and isinstance(item["tag"], dict):
                        tag = item["tag"]
                        
                        # Enchantments
                        if "Enchantments" in tag:
                            enchantments = []
                            for ench in tag["Enchantments"]:
                                if isinstance(ench, dict):
                                    ench_id = ench.get("id", "")
                                    if ench_id.startswith("minecraft:"):
                                        ench_id = ench_id[10:]
                                    lvl = ench.get("lvl", 1)
                                    enchantments.append({"id": ench_id, "lvl": lvl})
                            if enchantments:
                                item_data["enchantments"] = enchantments
                        
                        # Stored enchantments (for enchanted books)
                        if "StoredEnchantments" in tag:
                            stored_ench = []
                            for ench in tag["StoredEnchantments"]:
                                if isinstance(ench, dict):
                                    ench_id = ench.get("id", "")
                                    if ench_id.startswith("minecraft:"):
                                        ench_id = ench_id[10:]
                                    lvl = ench.get("lvl", 1)
                                    stored_ench.append({"id": ench_id, "lvl": lvl})
                            if stored_ench:
                                item_data["stored_enchantments"] = stored_ench
                        
                        # Potion effects
                        if "Potion" in tag:
                            potion_type = tag["Potion"]
                            if potion_type.startswith("minecraft:"):
                                potion_type = potion_type[10:]
                            item_data["potion_type"] = potion_type
                        
                        # Custom potion effects
                        if "CustomPotionEffects" in tag:
                            effects = []
                            for effect in tag["CustomPotionEffects"]:
                                if isinstance(effect, dict):
                                    effect_id = effect.get("id", "")
                                    if effect_id.startswith("minecraft:"):
                                        effect_id = effect_id[10:]
                                    duration = effect.get("Duration", 0)
                                    amplifier = effect.get("Amplifier", 0)
                                    effects.append({
                                        "id": effect_id,
                                        "duration": duration,
                                        "amplifier": amplifier
                                    })
                            if effects:
                                item_data["potion_effects"] = effects
                    
                    if item_id:
                        inv_items.append(item_data)
            
            api_data["inventory"] = inv_items
            logger.debug(f"[PlayerData] Converted {len(inv_items)} inventory items from nbtlib")
        
    except Exception as e:
        logger.error(f"[PlayerData] Failed to convert nbtlib data to API format: {e}")
    
    return api_data


def read_playerdata_file(username: str) -> dict:
    """
    Read player data directly from the Minecraft playerdata file.
    This is the most reliable method as it bypasses RCON size limits.
    
    Args:
        username: Player username
        
    Returns:
        Dictionary with player data or None if file not found
    """
    try:
        import gzip
        import uuid
        
        # Try to find the playerdata file
        # Minecraft stores playerdata as UUID.dat files
        world_path = SERVER_DIR / "world"
        playerdata_path = world_path / "playerdata"
        
        if not playerdata_path.exists():
            logger.debug(f"[PlayerData] Playerdata directory not found: {playerdata_path}")
            return None
        
        # Try to find the player's UUID file
        # First, try to get UUID from online-players.json if available
        uuid_file = world_path / "stats" / f"{username}.json"
        player_uuid = None
        
        # Try common UUID mapping files
        for mapping_file in ["usercache.json", "known_players.json"]:
            mapping_path = world_path / mapping_file
            if mapping_path.exists():
                try:
                    with open(mapping_path, 'r') as f:
                        import json
                        data = json.load(f)
                        for entry in data:
                            if entry.get('name') == username:
                                player_uuid = entry.get('uuid')
                                break
                except Exception:
                    pass
        
        if not player_uuid:
            # Try direct UUID conversion (works for offline mode)
            try:
                from uuid import UUID
                # Offline mode UUID generation
                player_uuid = str(UUID('00000000-0000-0000-0000-' + str(hash(username))[:12]))
            except Exception:
                pass
        
        if player_uuid:
            dat_file = playerdata_path / f"{player_uuid}.dat"
            if dat_file.exists():
                logger.info(f"[PlayerData] Reading playerdata file: {dat_file.name}")
                return SimpleNBTParser.read_playerdat(dat_file)
        
        # Fallback: try to find by searching all files
        for dat_file in playerdata_path.glob("*.dat"):
            try:
                data = SimpleNBTParser.read_playerdat(dat_file)
                if data and data.get('Name') == username:
                    logger.info(f"[PlayerData] Found playerdata file by search: {dat_file.name}")
                    return data
            except Exception:
                continue
        
        logger.debug(f"[PlayerData] No playerdata file found for {username}")
        return None
        
    except Exception as e:
        logger.error(f"[PlayerData] Failed to read playerdata file: {e}")
        return None


def parse_inventory_only(response: str) -> list:
    """
    Parse inventory data from a dedicated inventory query response.
    Handles the format: 'hao333333 has the following entity data: [{...}, {...}]'
    
    Args:
        response: RCON response from 'data get entity <player> Inventory' command
        
    Returns:
        List of inventory items or empty list if parsing fails
    """
    try:
        # Find the inventory list in the response
        # Format: "username has the following entity data: [{...}]"
        list_start = response.find('[')
        if list_start == -1:
            logger.debug("[PlayerData] No inventory list found in response")
            return []
        
        # Find matching end bracket by tracking braces
        brace_depth = 0
        list_end = -1
        in_string = False
        string_char = None
        
        for i in range(list_start, len(response)):
            char = response[i]
            
            if not in_string:
                if char in '"\'':
                    in_string = True
                    string_char = char
                elif char == '[' or char == '{':
                    brace_depth += 1
                elif char == ']' or char == '}':
                    brace_depth -= 1
                    if brace_depth == 0 and char == ']':
                        list_end = i + 1
                        break
            else:
                if char == '\\' and i + 1 < len(response):
                    continue  # Skip escaped character
                elif char == string_char:
                    in_string = False
                    string_char = None
        
        if list_end == -1:
            logger.debug("[PlayerData] Could not find end of inventory list")
            return []
        
        inv_content = response[list_start:list_end]
        logger.debug(f"[PlayerData] Extracted inventory content: {inv_content[:100]}...")
        
        # Use the robust NBT parser
        items = parse_nbt_compound_list(inv_content)
        return items
        
    except Exception as e:
        logger.error(f"[PlayerData] Failed to parse inventory-only response: {e}")
        return []


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
        "source": "live_query",
        "error": None
    }
    
    try:
        nbt_start = log_line.find("{")
        if nbt_start > 0:
            nbt_data = log_line[nbt_start:]
            logger.debug(f"[PlayerData] Parsing NBT data from RCON response")
            
            if "playerGameType" in nbt_data:
                gm_match = re.search(r'playerGameType:\s*(\d+)', nbt_data)
                if gm_match:
                    gm_map = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
                    response_data["gamemode"] = gm_map.get(int(gm_match.group(1)), "unknown")
                    logger.debug(f"[PlayerData] Parsed gamemode: {response_data['gamemode']}")
            
            if "XpLevel" in nbt_data:
                xp_match = re.search(r'XpLevel:\s*(\d+)', nbt_data)
                if xp_match:
                    response_data["level"] = int(xp_match.group(1))
                    logger.debug(f"[PlayerData] Parsed level: {response_data['level']}")
            
            if "Health" in nbt_data:
                health_match = re.search(r'Health:\s*(\d+\.?\d*)', nbt_data)
                if health_match:
                    response_data["health"] = float(health_match.group(1))
                    logger.debug(f"[PlayerData] Parsed health: {response_data['health']}")
            
            if "foodLevel" in nbt_data:
                food_match = re.search(r'foodLevel:\s*(\d+)', nbt_data)
                if food_match:
                    response_data["food"] = int(food_match.group(1))
                    logger.debug(f"[PlayerData] Parsed food: {response_data['food']}")
            
            pos_match = re.search(r'Pos:\s*\[(-?\d+\.?\d*)d,\s*(-?\d+\.?\d*)d,\s*(-?\d+\.?\d*)d\]', nbt_data)
            if pos_match:
                response_data["position"] = {
                    "x": round(float(pos_match.group(1)), 1),
                    "y": round(float(pos_match.group(2)), 1),
                    "z": round(float(pos_match.group(3)), 1)
                }
                logger.debug(f"[PlayerData] Parsed position: {response_data['position']}")
            
            dim_match = re.search(r'Dimension:\s*"([^"]+)"', nbt_data)
            if dim_match:
                response_data["dimension"] = dim_match.group(1)
                logger.debug(f"[PlayerData] Parsed dimension: {response_data['dimension']}")
            
            inv_match = re.search(r'Inventory:\s*\[(.*?)\]', nbt_data, re.DOTALL)
            if inv_match:
                inv_content = inv_match.group(1).strip()
                logger.debug(f"[PlayerData] Found inventory data, content length: {len(inv_content)}")
                
                # Use robust NBT compound parser to extract inventory items
                inv_items = parse_nbt_compound_list(inv_content)
                response_data["inventory"] = inv_items
                
                if inv_items:
                    logger.info(f"[PlayerData] Parsed {username}: online={response_data['online']}, gamemode={response_data['gamemode']}, health={response_data['health']}, inventory={len(inv_items)} items")
                else:
                    logger.info(f"[PlayerData] Parsed {username}: inventory is empty or contains no valid items")
            else:
                logger.info(f"[PlayerData] No inventory data found for {username} (player may have empty inventory)")
        else:
            logger.warning(f"[PlayerData] No NBT start brace found in RCON response for {username}")
            response_data["error"] = "no_nbt_data"
    
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
    
    # Try file-based method first (most reliable, works for both online and offline)
    try:
        # Check if the specific player is online
        is_player_online = False
        if server_status.state == ServerState.ACTIVE and hasattr(server_status, 'players'):
            for player in server_status.players:
                if hasattr(player, 'username') and player.username.lower() == username.lower():
                    is_player_online = True
                    break
        
        # Only trigger save-all flush if the player is actually online
        if is_player_online:
            from .commands import execute_command
            logger.info(f"[PlayerData] Player {username} is online, triggering save-all flush")
            await execute_command("save-all flush")
            time.sleep(0.5)  # Allow OS to complete write operation
        else:
            logger.info(f"[PlayerData] Player {username} is offline, reading saved .dat file directly")
        
        # Read from file (works even if server is offline)
        logger.info(f"[PlayerData] Attempting to read playerdata file for {username}")
        file_data = read_playerdata_file_nbtlib(username)
        if file_data:
            logger.info(f"[PlayerData] Successfully read playerdata file, converting to API format")
            player_data = convert_nbtlib_to_api_format(username, file_data)
            player_data["source"] = "nbtlib_file"
            # Set online status based on actual player presence
            player_data["online"] = is_player_online
            logger.info(f"[PlayerData] Retrieved data via nbtlib for {username}: {len(player_data.get('inventory', []))} items, online={player_data['online']}")
            return player_data
        else:
            logger.warning(f"[PlayerData] Failed to read playerdata file for {username}, falling back to RCON")
    except Exception as e:
        logger.warning(f"[PlayerData] File-based method failed: {e}")
        logger.exception("[PlayerData] Full traceback:")
    
    # Fallback to RCON if file method fails
    if server_status.state == ServerState.ACTIVE:
        try:
            from .commands import get_player_data
            response = get_player_data(username)
            
            if response:
                debug_info["method"] = "rcon_direct"
                debug_info["response_length"] = len(response)
                debug_info["response"] = response[:300] if len(response) > 300 else response
                
                # Check for truncated response (RCON limit ~4096 bytes)
                if len(response) >= 4090:
                    logger.warning(f"[PlayerData] RCON response truncated: {len(response)} bytes, using file fallback")
                    # Try file-based method instead
                    file_data = read_playerdata_file(username)
                    if file_data:
                        player_data = convert_filedata_to_api_format(username, file_data)
                        player_data["source"] = "playerdata_file"
                        player_data["online"] = True  # Assume online if we got here
                        logger.info(f"[PlayerData] Retrieved data from file for {username}: {len(player_data.get('inventory', []))} items")
                        return player_data
                
                logger.debug(f"[PlayerData] Raw RCON response for {username}:\n{response[:500]}{'...' if len(response) > 500 else ''}")
                
                # Parse RCON response for player data
                player_data = parse_live_player_data(username, response)
                if player_data and not player_data.get("error"):
                    player_data["source"] = "rcon_live"
                    
                    # Check if inventory might be truncated (empty inventory but large response suggests truncation)
                    has_inventory_tag = "Inventory:" in response
                    inventory_is_empty = len(player_data.get('inventory', [])) == 0
                    response_is_large = len(response) >= 3000  # Likely truncated if this large but no items
                    
                    if has_inventory_tag and inventory_is_empty and response_is_large:
                        logger.warning(f"[PlayerData] Possible truncated inventory detected, fetching separately")
                        # Fetch inventory separately to avoid RCON size limits
                        from .commands import get_player_inventory
                        inv_response = get_player_inventory(username)
                        if inv_response:
                            inv_items = parse_inventory_only(inv_response)
                            if inv_items:
                                player_data['inventory'] = inv_items
                                logger.info(f"[PlayerData] Fetched {len(inv_items)} items via separate inventory query")
                    
                    logger.info(f"[PlayerData] RCON data retrieved for {username}: {len(player_data.get('inventory', []))} items, online={player_data.get('online')}")
                    return player_data
                else:
                    logger.warning(f"[PlayerData] RCON response received but parsing failed or no valid data for {username}")
                    logger.debug(f"[PlayerData] Parsed data: {player_data}")
            else:
                logger.debug(f"[PlayerData] RCON returned no response for {username}")
        except Exception as e:
            logger.warning(f"[PlayerData] RCON query exception: {e}")
            logger.exception("[PlayerData] Full traceback:")
    
    # Fallback to log parsing method
    is_running = server_status.state == ServerState.ACTIVE and console_controller.is_console_running()
    debug_info["is_running"] = is_running
    
    if is_running and PYWINAUTO_AVAILABLE:
        try:
            escaped_name = username.replace(" ", "_")
            command = f"data get entity {escaped_name}"
            
            time_before = time.time()
            
            from .commands import execute_command
            success = await execute_command(command)
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
