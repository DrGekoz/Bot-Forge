"""
New Game Modes for LLMCord / Bot-Forge
========================================
20 Questions, Show-and-Tell, Pokemon Battles, MTG Battles
All with holographic memory integration for persistent progression.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("new_game_modes")

# ═══════════════════════════════════════════════════════════════
#  HOLOGRAPHIC MEMORY HELPERS (Bot-Forge compatible)
# ═══════════════════════════════════════════════════════════════

try:
    from holographic_memory.store import MemoryStore as StandaloneStore
    _HOLO_AVAILABLE = True
except ImportError:
    _HOLO_AVAILABLE = False

try:
    from core.memory_store import get_store
    _MEMORY_AVAILABLE = True
except ImportError:
    _MEMORY_AVAILABLE = False

_MEM_DB_PATH = None

def _get_hm_store():
    """Get the best available memory store."""
    global _MEM_DB_PATH
    if _HOLO_AVAILABLE:
        try:
            from holographic_memory.store import MemoryStore as StandaloneStore
            if _MEM_DB_PATH is None:
                _MEM_DB_PATH = str(Path(__file__).parent / "llmcord_memory.db")
                if not os.path.exists(_MEM_DB_PATH):
                    # Try Bot-Forge path
                    alt = str(Path(__file__).parent.parent / "memory_store.db")
                    if os.path.exists(alt):
                        _MEM_DB_PATH = alt
            return StandaloneStore(db_path=_MEM_DB_PATH)
        except Exception:
            pass
    return None

def store_bot_memory(bot_id: int, content: str, category: str = "game", tags: str = ""):
    """Store a fact about a bot in holographic memory."""
    store = _get_hm_store()
    if store:
        try:
            return store.add_fact(content, category=category, tags=tags)
        except Exception as e:
            logger.warning(f"HM store failed: {e}")
    return None

def search_bot_memory(query: str, limit: int = 20) -> list:
    """Search bot memories."""
    store = _get_hm_store()
    if store:
        try:
            return store.search_facts(query, limit=limit)
        except Exception:
            return []
    return []

# ═══════════════════════════════════════════════════════════════
#  STATE MANAGEMENT (shared with voice_chat.py)
# ═══════════════════════════════════════════════════════════════

STATE_DIR = Path("vc_group_state")

def _state_path(guild_id: int) -> str:
    return str(STATE_DIR / f"g{guild_id}.json")

def _read_group_state(guild_id: int) -> dict:
    path = _state_path(guild_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def _write_group_state(guild_id: int, state: dict):
    path = _state_path(guild_id)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)

# ═══════════════════════════════════════════════════════════════
#  20 QUESTIONS
# ═══════════════════════════════════════════════════════════════

TWENTY_QUESTIONS_PICKER_PROMPT = """
─── 20 QUESTIONS — You are the PICKER ───
You have secretly chosen: {secret_thing}

RULES:
1. You KNOW what the thing is — do NOT reveal it
2. Answer each guesser's question with ONLY "Yes", "No", or "Sometimes" / "Maybe"
3. If a guesser makes a GUESS ("Is it X?") that IS the thing, say "Correct! Well done!"
4. If a guesser makes a guess that is WRONG, say "Nope, that's not it" — that counts as a question
5. After 20 total questions/guesses, if nobody guessed correctly, the Referee will announce you as winner
6. Keep answers to ONE WORD when possible
7. The category is: {category} (person, place, or thing)
"""

TWENTY_QUESTIONS_GUESSER_PROMPT = """
─── 20 QUESTIONS — You are a GUESSER ───
Category: {category}

RULES:
1. Ask yes/no questions to narrow down what the Picker chose
2. You can also make a GUESS ("Is it X?") — this counts as a question
3. If you guess correctly, YOU WIN
4. If you guess wrong, the Picker scores a point
5. After 20 total questions, if nobody guessed right, the Picker wins
6. Previous questions & answers: {history}
7. Questions asked so far: {question_count}/20
"""

TWENTY_QUESTIONS_REFEREE_PROMPT = """
─── 20 QUESTIONS — You are the REFEREE ───
Category: {category}

RULES:
1. Track the question count and enforce the 20-question limit
2. Do NOT reveal the Picker's secret
3. When a guesser guesses correctly: announce the winner and reveal the secret
4. After 20 questions with no correct guess: announce the Picker as winner and reveal the secret
5. Questions asked: {question_count}/20
6. Keep announcements punchy and enthusiastic
7. At the end, output in this format:
   [WINNER]
   Winner: <bot_name>
   Secret was: <secret_thing>
   [/WINNER]
"""

def _init_20questions_state(guild_id: int, turn_order: list[int], picker_id: int, referee_id: int, category: str = "thing") -> dict:
    """Create initial 20 Questions state."""
    choices = {
        "person": ["Albert Einstein", "Cleopatra", "Shakespeare", "Elon Musk", "Napoleon",
                    "Marie Curie", "Gandhi", "Leonardo da Vinci", "Abraham Lincoln", "Marilyn Monroe",
                    "Einstein", "Tesla", "Julius Caesar", "Queen Elizabeth", "Mozart",
                    "Sun Tzu", "Edison", "Freud", "Van Gogh", "Newton"],
        "place": ["Atlantis", "The Moon", "A volcano", "The Great Wall of China", "The Mariana Trench",
                   "The Bermuda Triangle", "Area 51", "Easter Island", "Antarctica", "The Sistine Chapel",
                   "The Amazon", "The Sahara Desert", "Stonehenge", "Machu Picchu", "The North Pole",
                   "A black hole", "The Colosseum", "The Louvre", "Mount Everest", "Walmart"],
        "thing": ["A pencil", "A rubber duck", "A microwave", "A toothbrush", "A mirror",
                   "A calculator", "A lightbulb", "A lock", "A zipper", "A paperclip",
                   "A coin", "A book", "A clock", "A wheel", "A key",
                   "A balloon", "A sponge", "A magnet", "A candle", "A feather"],
    }
    secret_thing = random.choice(choices.get(category, choices["thing"]))
    guesser_ids = [bid for bid in turn_order if bid != picker_id and bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "20questions",
        "turn_order": turn_order, "current_index": 0,
        "picker_id": picker_id, "referee_id": referee_id,
        "guesser_ids": guesser_ids,
        "secret_thing": secret_thing, "category": category,
        "question_count": 0, "max_questions": 20,
        "history": [], "winner": None,
        "phase": "picker_chooses",
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_20questions_picker(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "20questions" and state.get("picker_id") == bot_id

def _is_20questions_referee(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "20questions" and state.get("referee_id") == bot_id

def _is_20questions_guesser(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "20questions" and bot_id in state.get("guesser_ids", [])

def _check_20questions_guess(state: dict, guesser_id: int, reply: str) -> bool:
    """Check if a guesser's reply contains a correct guess. Returns True if game should end."""
    secret = state.get("secret_thing", "").lower()
    reply_lower = reply.lower()
    
    # Check for direct guess patterns
    correct_phrases = [
        f"is it {secret}",
        f"it's {secret}",
        f"it is {secret}",
        secret.rstrip('.'),
    ]
    
    for phrase in correct_phrases:
        if phrase in reply_lower:
            return True
    return False

def _check_20questions_picker_confirm(state: dict, reply: str) -> bool:
    """Check if picker confirmed a correct guess."""
    reply_lower = reply.lower()
    confirm_words = ["correct", "right", "yes!", "that's it", "you got it", "well done", "nailed it"]
    for word in confirm_words:
        if word in reply_lower:
            return True
    return False

# ═══════════════════════════════════════════════════════════════
#  SHOW AND TELL (with HM inventory)
# ═══════════════════════════════════════════════════════════════

SHOW_TELL_PRESENTER_PROMPT = """
─── SHOW & TELL — You are PRESENTING ───
Your items: {inventory}

RULES:
1. Pick 1-3 items from your inventory to present
2. Pitch them HARD — why are they amazing? What do they do?
3. Be creative, dramatic, persuasive
4. After your pitch, the other bots will roast them — defend your items!
5. The Referee scores you on: pitch quality (1-10), creativity (1-10)
"""

SHOW_TELL_ROASTER_PROMPT = """
─── SHOW & TELL — You are ROASTING ───
{presenter} presented: {inventory}

RULES:
1. ROAST those items — why are they garbage?
2. Then pitch YOUR items instead
3. Be funny, savage, creative
4. The Referee scores you on: roast quality (1-10), your pitch quality (1-10)
5. Your inventory: {my_inventory}
"""

SHOW_TELL_REFEREE_PROMPT = """
─── SHOW & TELL — You are the REFEREE ───
{presenter} presented: {inventory}

RULES:
1. Score each roaster's roast and pitch out of 10
2. Score the presenter's pitch and defense out of 10
3. After everyone has gone, announce the winner
4. Output format:
   [SCORE]
   Presenter: <bot_name> — Pitch: X/10, Defense: X/10 = Total X/20
   Roaster: <bot_name> — Roast: X/10, Pitch: X/10 = Total X/20
   Winner: <bot_name>
   [/SCORE]
"""

def _init_showtell_state(guild_id: int, turn_order: list[int], referee_id: int) -> dict:
    presenters = [bid for bid in turn_order if bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "showtell",
        "turn_order": turn_order, "current_index": 0,
        "referee_id": referee_id, "presenter_order": presenters,
        "current_presenter_idx": 0, "round_phase": "presenting",
        "scores": {}, "winner": None,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_showtell_referee(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "showtell" and state.get("referee_id") == bot_id

def load_bot_inventory(bot_id: int) -> list[dict]:
    """Load a bot's inventory from holographic memory."""
    items = []
    memories = search_bot_memory(f"bot_inventory:{bot_id}", limit=50)
    for mem in memories:
        content = mem.get("content", "") if isinstance(mem, dict) else str(mem)
        tags = mem.get("tags", "") if isinstance(mem, dict) else ""
        if f"bot_inventory:{bot_id}" in tags or "inventory" in tags:
            items.append({"content": content, "id": mem.get("fact_id", 0) if isinstance(mem, dict) else 0})
    if not items:
        # Fallback: generate some fake items for flavor
        fallback_items = [
            "A slightly used time machine (only goes forward)",
            "The world's smallest violin",
            "A jar of 'premium' elbow grease",
            "A map to the lost city of Atlantis (hand-drawn)",
            "One sock from every pair you've ever owned",
        ]
        items = [{"content": item, "id": 0} for item in random.sample(fallback_items, min(3, len(fallback_items)))]
    return items

def add_item_to_inventory(bot_id: int, item_name: str, item_desc: str = "", price: int = 0):
    """Store a purchased item in holographic memory under bot ID."""
    content = f"{item_name}" + (f" - {item_desc}" if item_desc else "")
    tags = f"bot_inventory:{bot_id},inventory,item"
    if price:
        content += f" (purchased for ${price})"
    store_bot_memory(bot_id, content=content, category="game", tags=tags)

# ═══════════════════════════════════════════════════════════════
#  POKEMON / MTG BATTLES (unified engine)
# ═══════════════════════════════════════════════════════════════

POKEMON_MOVES = [
    ("Tackle", 40, 95, "normal"), ("Ember", 40, 100, "fire"),
    ("Water Gun", 40, 100, "water"), ("Vine Whip", 45, 95, "grass"),
    ("Thunder Shock", 40, 100, "electric"), ("Psychic", 90, 100, "psychic"),
    ("Shadow Ball", 80, 100, "ghost"), ("Flamethrower", 90, 100, "fire"),
    ("Hydro Pump", 110, 80, "water"), ("Solar Beam", 120, 100, "grass"),
    ("Thunderbolt", 90, 100, "electric"), ("Ice Beam", 90, 100, "ice"),
    ("Earthquake", 100, 100, "ground"), ("Dragon Claw", 80, 100, "dragon"),
    ("Hyper Beam", 150, 90, "normal"), ("Quick Attack", 40, 100, "normal"),
    ("Giga Drain", 75, 100, "grass"), ("Surf", 90, 100, "water"),
    ("Thunder Punch", 75, 100, "electric"), ("Fire Punch", 75, 100, "fire"),
    ("Rock Slide", 75, 95, "rock"), ("Dark Pulse", 80, 100, "dark"),
    ("Moonblast", 95, 100, "fairy"), ("Iron Tail", 100, 75, "steel"),
]

MTG_SPELLS = [
    ("Lightning Bolt", 3, 100, "Instant"), ("Counterspell", 0, 0, "Instant"),
    ("Fireball", 5, 95, "Sorcery"), ("Healing Touch", 0, 100, "Instant"),
    ("Ancestral Recall", 0, 0, "Instant"), ("Dark Ritual", 0, 100, "Sorcery"),
    ("Swords to Plowshares", 0, 75, "Instant"), ("Wrath of God", 0, 50, "Sorcery"),
    ("Time Walk", 0, 0, "Sorcery"), ("Shock", 2, 100, "Instant"),
    ("Divine Verdict", 0, 60, "Instant"), ("Cancel", 0, 80, "Instant"),
    ("Lava Axe", 5, 100, "Sorcery"), ("Naturalize", 0, 80, "Instant"),
    ("Fog", 0, 100, "Instant"), ("Unsummon", 0, 100, "Instant"),
]

POKEMON_NAMES = [
    "Pikachu", "Charizard", "Blastoise", "Venusaur", "Gengar", "Dragonite",
    "Mewtwo", "Lucario", "Greninja", "Gardevoir", "Garchomp", "Tyranitar",
    "Sylveon", "Umbreon", "Lugia", "Rayquaza", "Arceus", "Mimikyu",
]

def _poke_type_effectiveness(attack_type: str, defender_type: str) -> float:
    """Simple type effectiveness."""
    chart = {
        "fire": {"grass": 2.0, "water": 0.5, "fire": 0.5, "ice": 2.0, "steel": 2.0},
        "water": {"fire": 2.0, "grass": 0.5, "water": 0.5, "ground": 2.0, "rock": 2.0},
        "grass": {"water": 2.0, "fire": 0.5, "grass": 0.5, "ground": 2.0, "rock": 2.0},
        "electric": {"water": 2.0, "electric": 0.5, "ground": 0.0, "flying": 2.0},
        "psychic": {"poison": 2.0, "psychic": 0.5, "dark": 0.0, "fighting": 2.0},
        "ghost": {"psychic": 2.0, "ghost": 2.0, "normal": 0.0, "dark": 0.5},
        "ice": {"grass": 2.0, "fire": 0.5, "ice": 0.5, "water": 0.5, "dragon": 2.0, "flying": 2.0},
        "dragon": {"dragon": 2.0, "steel": 0.5, "fairy": 0.0},
        "dark": {"psychic": 2.0, "ghost": 2.0, "dark": 0.5, "fairy": 0.5},
        "fairy": {"dragon": 2.0, "dark": 2.0, "fire": 0.5, "poison": 0.5, "steel": 0.5},
        "fighting": {"normal": 2.0, "ice": 2.0, "rock": 2.0, "dark": 2.0, "psychic": 0.5, "flying": 0.5, "poison": 0.5, "ghost": 0.0, "fairy": 0.5},
        "flying": {"grass": 2.0, "fighting": 2.0, "bug": 2.0, "electric": 0.5, "rock": 0.5, "steel": 0.5},
        "poison": {"grass": 2.0, "poison": 0.5, "ground": 0.5, "rock": 0.5, "ghost": 0.5, "steel": 0.0, "fairy": 2.0},
        "ground": {"fire": 2.0, "electric": 2.0, "grass": 0.5, "poison": 2.0, "rock": 0.5, "flying": 0.0},
        "rock": {"fire": 2.0, "ice": 2.0, "flying": 2.0, "bug": 2.0, "fighting": 0.5, "ground": 0.5, "steel": 0.5},
        "steel": {"ice": 2.0, "rock": 2.0, "fairy": 2.0, "fire": 0.5, "water": 0.5, "electric": 0.5, "steel": 0.5},
        "normal": {"ghost": 0.0, "rock": 0.5, "steel": 0.5},
    }
    return chart.get(attack_type, {}).get(defender_type, 1.0)

def _roll_stats(level: int = 1) -> dict:
    """Generate battle stats scaled by level."""
    base = 50 + (level - 1) * 10
    return {
        "hp": base + random.randint(0, 20),
        "attack": base//2 + random.randint(0, 10) + (level-1)*3,
        "defense": base//2 + random.randint(0, 10) + (level-1)*2,
        "agility": base//2 + random.randint(0, 10) + (level-1)*2,
        "magic": base//2 + random.randint(0, 10) + (level-1)*2,
    }

def _load_bot_battle_stats(bot_id: int, prefix: str = "pokemon") -> dict:
    """Load or create persistent battle stats from HM."""
    memories = search_bot_memory(f"battlestats:{prefix}:{bot_id}", limit=5)
    if memories:
        for mem in memories:
            if isinstance(mem, dict):
                mcontent = mem.get("content", "")
                if mcontent.startswith("{"):
                    try:
                        return json.loads(mcontent)
                    except json.JSONDecodeError:
                        pass
    # Default stats
    stats = {
        "level": 1, "xp": 0, "wins": 0, "losses": 0,
        "style_points": 0, "total_battles": 0,
        "pokemon": random.choice(POKEMON_NAMES),
        "poke_type": random.choice(["fire", "water", "grass", "electric", "psychic", "ghost", "dragon", "dark", "fairy", "fighting", "normal", "ice"]),
    }
    stats.update(_roll_stats(stats["level"]))
    stats["max_hp"] = stats["hp"]
    return stats

def _save_bot_battle_stats(bot_id: int, stats: dict, prefix: str = "pokemon"):
    """Save battle stats to HM."""
    content = json.dumps(stats)
    store_bot_memory(bot_id, content=content, category="game", tags=f"battlestats:{prefix}:{bot_id},battlestats,{prefix}")

def _init_battle_state(guild_id: int, turn_order: list[int], referee_id: int, mode: str = "pokemon") -> dict:
    """Create initial battle state for Pokemon or MTG."""
    fighters = [bid for bid in turn_order if bid != referee_id]
    fighter_stats = {}
    for bid in fighters:
        stats = _load_bot_battle_stats(bid, prefix=mode)
        fighter_stats[str(bid)] = stats
    
    # Determine turn order by agility
    sorted_fighters = sorted(fighters, key=lambda bid: fighter_stats.get(str(bid), {}).get("agility", 50), reverse=True)
    
    battle_round = 1
    state = {
        "active": True, "guild_id": guild_id, "mode": mode,
        "turn_order": turn_order,
        "referee_id": referee_id,
        "fighters": fighters,
        "fighter_stats": fighter_stats,
        "sorted_fighters": sorted_fighters,
        "current_attacker_idx": 0,
        "battle_round": battle_round,
        "max_rounds": 20,
        "consecutive_defends": {str(bid): 0 for bid in fighters},
        "hp": {str(bid): fighter_stats.get(str(bid), {}).get("max_hp", 100) for bid in fighters},
        "effects": {}, "winner": None,
        "phase": "battle_intro",
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_battle_referee(bot_id: int, state: dict) -> bool:
    return state.get("referee_id") == bot_id

def _is_battle_fighter(bot_id: int, state: dict) -> bool:
    return bot_id in state.get("fighters", [])

def _advance_battle_turn(guild_id: int) -> dict:
    """Advance the battle turn and return updated state."""
    state = _read_group_state(guild_id)
    fighters = state.get("fighters", [])
    if not fighters:
        return state
    
    mode = state.get("mode", "pokemon")
    idx = state.get("current_attacker_idx", 0)
    next_idx = (idx + 1) % len(fighters)
    
    # Check if we completed a full round
    if next_idx == 0:
        state["battle_round"] = state.get("battle_round", 1) + 1
    
    state["current_attacker_idx"] = next_idx
    state["version"] = state.get("version", 0) + 1
    
    # Check for win condition
    hp = state.get("hp", {})
    alive = [fid for fid in fighters if hp.get(str(fid), 0) > 0]
    
    if len(alive) <= 1:
        # Battle over
        winner_id = alive[0] if alive else fighters[0]
        state["winner"] = winner_id
        state["active"] = False
        state["phase"] = "victory"
        
        # Save stats to HM
        for bid in fighters:
            stats = state.get("fighter_stats", {}).get(str(bid), {})
            if stats:
                b_id = bid if isinstance(bid, int) else int(bid)
                was_fainted = hp.get(str(bid), 0) <= 0
                if bid == winner_id:
                    stats["wins"] = stats.get("wins", 0) + 1
                    stats["xp"] = stats.get("xp", 0) + 50
                else:
                    stats["losses"] = stats.get("losses", 0) + 1
                    stats["xp"] = stats.get("xp", 0) + 10
                stats["total_battles"] = stats.get("total_battles", 0) + 1
                # Level up every 100 XP
                if stats["xp"] >= stats.get("level", 1) * 100:
                    stats["level"] = stats.get("level", 1) + 1
                    stats["xp"] = 0
                    new_stats = _roll_stats(stats["level"])
                    stats.update(new_stats)
                    stats["max_hp"] = stats["hp"]
                _save_bot_battle_stats(b_id, stats, prefix=mode)
    
    _write_group_state(guild_id, state)
    return state

BATTLE_REFEREE_PROMPT = """
─── {mode} BATTLE — Round {battle_round} ───
Fighters: {fighter_status}
Current turn: {attacker} (HP: {attacker_hp})
Target: {defender} (HP: {defender_hp})

RULES:
1. Announce whose turn it is and their HP
2. Score each attack on STYLE (1-10) and EFFECTIVENESS (1-10)
3. Apply damage formula: damage = base_power * type_effectiveness * (attack/defense) * random(0.85-1.0)
4. Style points BONUS for: creative descriptions, references, one-liners
5. When a fighter's HP hits 0, declare them fainted
6. Last bot standing WINS
7. After all, output:
   [ROUND_SCORE]
   Attacker: <name> — Style: X/10, Effect: X/10
   Style Points Bonus: +X
   [/ROUND_SCORE]
"""

if __name__ == "__main__":
    print("New Game Modes module loaded. Import this from voice_chat.py")
    print(f"Holographic memory available: {_HOLO_AVAILABLE}")
