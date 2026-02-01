import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import time
import uuid
import os
import json
from datetime import datetime, timezone, timedelta
import math
import random

# -----------------------------
# CONFIG
# -----------------------------

TOKEN = os.getenv("TOKEN_ID", "")
GUILD_TOKEN = os.getenv("GUILD_ID", "")
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
save_lock = asyncio.Lock()

# -----------------------------
# DATABASES / FILES
# -----------------------------

DATA_DIR = "assets"
PLAYER_FILE = os.path.join(DATA_DIR, "players.json")
HEROES_FILE = os.path.join(DATA_DIR, "heroes.json")
MONSTERS_FILE = os.path.join(DATA_DIR, "monsters.json")
ITEMS_FILE = os.path.join(DATA_DIR, "items.json")
AUCTION_FILE = "assets/auctions.json"

CONTRACT_EMOJI = "📜"

def load_json(path, default_obj=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default_obj if default_obj is not None else {}

async def save_json(path, data):
    async with save_lock:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

players = load_json(PLAYER_FILE, {})
heroes_db = load_json(HEROES_FILE, [])
monsters_db = load_json(MONSTERS_FILE, [])
auctions = load_json(AUCTION_FILE, {})
ITEMS = load_json(ITEMS_FILE, {})
CONSUMABLES = {i["name"].lower(): i for i in ITEMS["consumables"]}
KEYS = {i["name"].lower(): i for i in ITEMS["keys"]}
MATERIALS = {
    elem: {r: [m for m in mats] for r, mats in rarities.items()}
    for elem, rarities in ITEMS["materials"].items()
}

hero_by_id = {h.get("id"): h for h in heroes_db}
monster_by_id = {m.get("id"): m for m in monsters_db}

# -----------------------------
# COLORS / RARITY
# -----------------------------

rarity_colors = {
    "common": discord.Color.light_grey(),
    "uncommon": discord.Color.green(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
    "mythical": discord.Color.red(),
}

rarity_order = {
    "legendary": 5,
    "epic": 4,
    "rare": 3,
    "uncommon": 2,
    "common": 1
}

LEGENDARY_DROPS = {
    "Aurelia": [  # Light legendary
        {"name": "Relic of Radiant Souls", "price": None, "special": "AureliaSummon"}
    ],
    "Umbra": [  # Shadow legendary
        {"name": "Relic of Abyssal Souls", "price": None, "special": "UmbraSummon"}
    ],
    "Ignis": [  # Fire legendary
        {"name": "Relic of Infernal Souls", "price": None, "special": "IgnisSummon"}
    ],
    "Frostbane": [  # Ice legendary
        {"name": "Relic of Frozen Souls", "price": None, "special": "FrostbaneSummon"}
    ],
    "Zephyra": [  # Air legendary
        {"name": "Relic of Skybound Souls", "price": None, "special": "ZephyraSummon"}
    ]
}

BINGO_DURATION = 7200    # 2 hour
BINGO_COOLDOWN = 14400   # 4 hours
HUNT_DURATION = 7200    # 2 hour
HUNT_COOLDOWN = 14400   # 4 hours

# -----------------------------
# STARTUP
# -----------------------------

@bot.event
async def on_ready():
    print(f"✅ Pixel Heroes bot logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

# -----------------------------
# STATS / XP HELPERS
# -----------------------------

def calc_hp(base, iv, ev, level):
    return math.floor(((2 * base + iv + (ev // 4)) * level) / 100) + level + 10

def calc_stat(base, iv, ev, level):
    return math.floor(((2 * base + iv + (ev // 4)) * level) / 100) + 5

def entity_xp_required(level: int) -> int:
    return int(0.015 * (level ** 3) + 10 * (level ** 2))

def xp_required_for_level(level: int) -> int:
    base = 200
    growth = 1.059
    return int(base * (growth ** (level - 1)))

def ensure_full_ivs_evs(entity: dict):
    entity.setdefault("ivs", {})
    entity.setdefault("evs", {})
    for s in ["hp", "attack", "defense", "magic", "speed"]:
        entity["ivs"].setdefault(s, random.randint(0, 31))
        entity["evs"].setdefault(s, 0)

def recalc_stats_from_base(entity: dict, base_stats: dict):
    ivs, evs, level = entity["ivs"], entity["evs"], entity["level"]
    old_max = entity.get("stats", {}).get("hp", 1) or 1
    old_cur = entity.get("current_hp", old_max)
    hp_pct = max(0.0, min(1.0, old_cur / old_max))

    entity["stats"] = {
        "hp": calc_hp(base_stats["hp"], ivs["hp"], evs["hp"], level),
        "attack": calc_stat(base_stats["attack"], ivs["attack"], evs["attack"], level),
        "defense": calc_stat(base_stats["defense"], ivs["defense"], evs["defense"], level),
        "magic": calc_stat(base_stats["magic"], ivs["magic"], evs["magic"], level),
        "speed": calc_stat(base_stats["speed"], ivs["speed"], evs["speed"], level),
    }
    entity["current_hp"] = max(1, int(entity["stats"]["hp"] * hp_pct))

def award_entity_xp(entity: dict, xp_gain: int, log: list, base_stats_provider):
    ensure_full_ivs_evs(entity)
    if entity.get("level", 1) >= 100:
        entity["level"] = 100
        entity["xp"] = 0
        log.append(f"🔒 {entity['name']} is already Lv.100.")
        return

    entity["xp"] = entity.get("xp", 0) + xp_gain
    while entity["xp"] >= entity_xp_required(entity["level"] + 1) and entity["level"] < 100:
        entity["xp"] -= entity_xp_required(entity["level"] + 1)
        entity["level"] += 1
        base = base_stats_provider(entity)
        recalc_stats_from_base(entity, base)
        log.append(f"{entity['name']} leveled up to **Lv.{entity['level']}**")

async def show_xp(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    p = players[user_id]
    level = p.get("level", 1)
    xp = p.get("xp", 0)

    # XP requirement for next level
    next_level = level + 1
    base = 200       # XP to go from L1 → L2
    growth = 1.059   # tuned for ~1,000,000 total XP at L100
    xp_required =  int(base * (growth ** (next_level - 1)))
    xp_remaining = xp_required - xp

    # Rewards logic
    rewards = []
    if next_level < 10:
        rewards.append(f"💰 {next_level * 200} coins")
    elif next_level < 20:
        rewards.append(f"💰 {next_level * 200} coins")
    elif next_level < 50:
        rewards.append(f"💰 {next_level * 200} coins")
    else:
        rewards.append(f"💰 {next_level * 200} coins")
    if next_level in [10, 25, 50, 75, 100]:
        rewards.append("✨ 1 Master Ball")

    # Embed
    embed = discord.Embed(
        title=f"📈 XP Progress — {p['username']}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Level", value=level)
    embed.add_field(name="Current XP", value=xp)
    embed.add_field(name="Next Level", value=f"Lv.{next_level}")
    embed.add_field(name="XP Needed", value=f"{xp_remaining} XP", inline=False)
    embed.add_field(name="Next Rewards", value="\n".join(rewards), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------------
# PC HELPERS
# -----------------------------

class PCView(discord.ui.View):
    def __init__(self, user_id: str, heroes: list):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.h = heroes
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id

    def get_embed(self):
        h = self.h[self.index]
        embed = discord.Embed(
            title=f"📦 Barracks Storage — {h['name']} (Lv.{h['level']})",
            color=discord.Color.blurple())
        moveset = h.get("moveset", [])
        if moveset and isinstance(moveset[0], dict):
            moves_text = ", ".join([m.get("move") or m.get("skill") for m in moveset])
        else:
            moves_text = ", ".join(moveset) if moveset else "None"
        embed.add_field(name="Shiny", value="✨ Yes" if h.get("shiny") else "No")
        short_id = h["unique_id"][:9]
        embed.add_field(name="ID", value=short_id)
        embed.add_field(name="IVs", value=", ".join([f"{k}:{v}" for k,v in h["ivs"].items()]), inline=False)
        embed.add_field(name="Stats", value=", ".join([f"{k}:{v}" for k,v in h["stats"].items()]), inline=False)
        embed.add_field(name="Moves", value=moves_text)
        embed.set_footer(text=f"Hero {self.index+1}/{len(self.h)} | Caught {h.get('date_caught','Unknown')}")

        # If we saved a sprite path when caught, show it
        if "sprite" in h and h["sprite"] and os.path.exists(h["sprite"]):
            file = discord.File(h["sprite"], filename="sprite.png")
            embed.set_thumbnail(url="attachment://sprite.png")
            return embed, [file]
        return embed, []

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.h)
        embed, files = self.get_embed()
        await interaction.response.edit_message(embed=embed, attachments=files)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.h)
        embed, files = self.get_embed()
        await interaction.response.edit_message(embed=embed, attachments=files)

class ConfirmClearView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id

    @discord.ui.button(label="✅ Yes, clear Barracks", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = players[self.user_id]
        team_ids = set(user.get("active_team", []))

        before = len(user.get("pc", []))
        user["pc"] = [h for h in user.get("pc", []) if h["unique_id"] in team_ids]
        after = len(user["pc"])

        await save_json(PLAYER_FILE, players)

        cleared = before - after
        await interaction.response.edit_message(
            content=f"📦 Cleared **{cleared}** heroes from your barracks (active party preserved).",
            embed=None, view=None)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Cancelled Barracks clear.", embed=None, view=None)

class StarterSelectView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=60)
        self.user_id = user_id

    async def add_starter(self, interaction: discord.Interaction, hero_name: str):
        """Give the selected starter hero to a new player."""
        base_data = next((h for h in heroes_db if h["name"].lower() == hero_name.lower()), None)
        if not base_data:
            await interaction.response.send_message(f"❌ Could not find data for {hero_name}.", ephemeral=True)
            return

        unique_id = f"h{uuid.uuid4().hex[:9]}"
        level = 10

        # Generate IVs & EVs
        ivs = {s: random.randint(0, 31) for s in ["hp", "attack", "defense", "magic", "speed"]}
        evs = {s: 0 for s in ["hp", "attack", "defense", "magic", "speed"]}

        # Calculate scaled stats
        stats = {
            "hp": calc_hp(base_data["stats"]["hp"], ivs["hp"], evs["hp"], level),
            "attack": calc_stat(base_data["stats"]["attack"], ivs["attack"], evs["attack"], level),
            "defense": calc_stat(base_data["stats"]["defense"], ivs["defense"], evs["defense"], level),
            "magic": calc_stat(base_data["stats"]["magic"], ivs["magic"], evs["magic"], level),
            "speed": calc_stat(base_data["stats"]["speed"], ivs["speed"], evs["speed"], level),
        }

        # Moveset — first few skills that unlock at or below level 5
        moveset = [
            {
                "move": s["skill"],
                "type": s["type"],
                "power": int(s["power"]) if str(s["power"]).isdigit() else 0,
                "acc": int(s["acc."]) if str(s["acc."]).isdigit() else 100
            }
            for s in base_data.get("skills", []) if int(s["lv."]) <= level
        ][:4] or [{"move": "Strike", "power": 40, "acc": 100}]

        starter = {
            "id": base_data["id"],
            "unique_id": unique_id,
            "name": base_data["name"],
            "class": base_data["class"],
            "element": base_data["element"],
            "rarity": base_data["rarity"],
            "level": level,
            "shiny": random.random() < (1/5000),
            "ivs": ivs,
            "evs": evs,
            "stats": stats,
            "current_hp": stats["hp"],
            "xp": 0,
            "xp_to_next": entity_xp_required(level + 1),
            "moveset": moveset,
            "sprite": base_data.get("sprite", f"assets/heroes/{hero_name.lower()}.png"),
            "date_recruited": datetime.now().isoformat(),
        }

        # Save to player
        p = players[self.user_id]
        p["pc"].append(starter)
        p["active_team"].append(unique_id)
        p["codex"].append(base_data["id"])
        await save_json(PLAYER_FILE, players)

        await interaction.response.edit_message(
            content=f"🎉 You chose **{base_data['name']}** the {base_data['class']} as your starter hero!",
            view=None
        )

    # --- Starter Buttons ---
    @discord.ui.button(label="Damon", style=discord.ButtonStyle.red, emoji="🛡️")
    async def warrior_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.add_starter(interaction, "Damon")

    @discord.ui.button(label="Rilon", style=discord.ButtonStyle.blurple, emoji="🔮")
    async def mage_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.add_starter(interaction, "Rilon")

    @discord.ui.button(label="Ivy", style=discord.ButtonStyle.success, emoji="🗡️")
    async def rogue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.add_starter(interaction, "Ivy")

# -----------------------------
# AUCTION HELPERS
# -----------------------------

def new_auction_id():
    return f"A{uuid.uuid4().hex[:8]}"

def valid_duration(hours: int) -> bool:
    return hours in [12, 24, 48, 72]

# -----------------------------
# ENTITY GENERATION
# -----------------------------

def build_instance_from_template(template, level=None, is_hero=False):
    """Generate a battle-ready instance from a hero or monster template."""
    level = level or template.get("level", 10)
    shiny = is_hero and (random.random() < 1/5000)

    # IVs and EVs (same backend fields as Pokémon for simplicity)
    ivs = {s: random.randint(0, 31) for s in ["hp", "attack", "defense", "magic", "speed"]}
    evs = {s: 0 for s in ["hp", "attack", "defense", "magic", "speed"]}

    stats = template["stats"]

    # Simple scaling formula similar to Pokémon stat growth
    scaled_stats = {
        "hp": calc_hp(stats["hp"], ivs["hp"], evs["hp"], level),
        "attack": calc_stat(stats["attack"], ivs["attack"], evs["attack"], level),
        "defense": calc_stat(stats["defense"], ivs["defense"], evs["defense"], level),
        "magic": calc_stat(stats["magic"], ivs["magic"], evs["magic"], level),
        "speed": calc_stat(stats["speed"], ivs["speed"], evs["speed"], level),
    }

    # Build complete entity dict
    inst = {
        "id": template.get("id"),
        "unique_id": f"e{uuid.uuid4().hex[:9]}",
        "id": template["id"],
        "name": template["name"],
        "element": template.get("element", "Neutral"),
        "class": template.get("class", ""),
        "level": level,
        "rarity": template.get("rarity", "common"),
        "shiny": shiny,
        "ivs": ivs,
        "evs": evs,
        "stats": scaled_stats,
        "current_hp": scaled_stats["hp"],
        "xp": 0,
        "xp_to_next": level * 100,
        "moveset": [
            {
                "move": s["skill"],
                "type": s["type"],
                "power": int(s["power"]) if str(s["power"]).isdigit() else 0,
                "acc": int(s["acc."]) if str(s["acc."]).isdigit() else 100
            }
            for s in template.get("skills", [])[:4]
        ],
        "sprite": template.get("sprite", ""),
    }

    return inst

# -----------------------------
# ENCOUNTER PICKERS
# -----------------------------

import random

def pick_random_hero_template() -> dict:
    """Randomly selects a hero template based on rarity weights and level scaling."""
    if not heroes_db:
        return {
            "id": 1, "name": "Aiden", "class": "Knight", "element": "Earth",
            "stats": {"hp": 80, "attack": 70, "defense": 65, "magic": 30, "speed": 45},
            "skills": [{"skill": "Slash", "power": "50", "acc.": "100"}],
            "rarity": "uncommon",
            "level": 5,
            "shiny": False
        }

    # --- Group heroes by rarity ---
    rarity_groups = {
        "common": [h for h in heroes_db if h.get("rarity") == "common"],
        "uncommon": [h for h in heroes_db if h.get("rarity") == "uncommon"],
        "rare": [h for h in heroes_db if h.get("rarity") == "rare"],
        "epic": [h for h in heroes_db if h.get("rarity") == "epic"],
        "legendary": [h for h in heroes_db if h.get("rarity") == "legendary"],
    }

    # --- Weighted rarity selection ---
    rarity_weights = {
        "common": 50.0,
        "uncommon": 29.9,
        "rare": 15.0,
        "epic": 5.0,
        "legendary": 0.1,
    }

    rarities = list(rarity_weights.keys())
    weights = list(rarity_weights.values())
    chosen_rarity = random.choices(rarities, weights=weights, k=1)[0]

    # fallback if group empty
    if not rarity_groups[chosen_rarity]:
        chosen_rarity = "common"

    template = random.choice(rarity_groups[chosen_rarity])
    shiny = random.random() < (1 / 4096)  # 1 in 4096 chance

    # --- Level scaling ---
    if chosen_rarity == "legendary":
        level = int(random.triangular(50, 70, 55))
    elif chosen_rarity == "epic":
        level = int(random.triangular(30, 55, 45))
    elif chosen_rarity == "rare":
        level = int(random.triangular(15, 40, 25))
    elif chosen_rarity == "uncommon":
        level = int(random.triangular(10, 25, 15))
    else:
        level = int(random.triangular(3, 15, 10))

    # attach rarity and shiny to ensure consistent structure
    template = {**template, "rarity": chosen_rarity, "level": level, "shiny": shiny}
    return template

def pick_random_monster_template() -> dict:
    """Randomly selects a monster template based on rarity weights and level scaling."""
    if not monsters_db:
        return {
            "id": 1, "name": "Slime", "element": "Water",
            "stats": {"hp": 50, "attack": 20, "defense": 15, "magic": 10, "speed": 20},
            "skills": [{"skill": "Splash", "power": "30", "acc.": "100"}],
            "rarity": "common",
            "level": 5,
            "shiny": False
        }

    # --- Group monsters by rarity ---
    rarity_groups = {
        "common": [m for m in monsters_db if m.get("rarity") == "common"],
        "uncommon": [m for m in monsters_db if m.get("rarity") == "uncommon"],
        "rare": [m for m in monsters_db if m.get("rarity") == "rare"],
        "epic": [m for m in monsters_db if m.get("rarity") == "epic"],
        "legendary": [m for m in monsters_db if m.get("rarity") == "legendary"],
    }

    rarity_weights = {
        "common": 60.0,
        "uncommon": 25.0,
        "rare": 10.0,
        "epic": 4.0,
        "legendary": 1.0,
    }

    rarities = list(rarity_weights.keys())
    weights = list(rarity_weights.values())
    chosen_rarity = random.choices(rarities, weights=weights, k=1)[0]

    if not rarity_groups[chosen_rarity]:
        chosen_rarity = "common"

    template = random.choice(rarity_groups[chosen_rarity])
    shiny = random.random() < (1 / 8192)  # 1 in 8192 chance

    # --- Level scaling ---
    if chosen_rarity == "legendary":
        level = int(random.triangular(60, 80, 70))
    elif chosen_rarity == "epic":
        level = int(random.triangular(40, 60, 50))
    elif chosen_rarity == "rare":
        level = int(random.triangular(20, 45, 30))
    elif chosen_rarity == "uncommon":
        level = int(random.triangular(10, 30, 20))
    else:
        level = int(random.triangular(3, 20, 10))

    template = {**template, "rarity": chosen_rarity, "level": level, "shiny": shiny}
    return template

def pick_random_hero_template_by_rarity(target_rarity: str) -> dict:
    """Selects a hero specifically of the given rarity (epic, legendary, etc.)."""
    if not heroes_db:
        return pick_random_hero_template()  # fallback

    rarity_groups = {
        "common": [h for h in heroes_db if h.get("rarity") == "common"],
        "uncommon": [h for h in heroes_db if h.get("rarity") == "uncommon"],
        "rare": [h for h in heroes_db if h.get("rarity") == "rare"],
        "epic": [h for h in heroes_db if h.get("rarity") == "epic"],
        "legendary": [h for h in heroes_db if h.get("rarity") == "legendary"],
    }

    group = rarity_groups.get(target_rarity.lower(), [])
    if not group:
        return pick_random_hero_template()

    template = random.choice(group)
    shiny = random.random() < (1 / 4096)

    # level scaling tied to rarity
    if target_rarity == "legendary":
        level = int(random.triangular(70, 85, 80))
    elif target_rarity == "epic":
        level = int(random.triangular(45, 60, 50))
    else:
        level = int(random.triangular(15, 30, 20))

    return {**template, "rarity": target_rarity, "level": level, "shiny": shiny}

# -----------------------------
# EXPLORATION COMMAND
# -----------------------------

@bot.tree.command(name="explore", description="Explore and encounter heroes or monsters!")
async def explore(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return

    roll = random.random()

    if roll < 0.01:
        key_roll = random.randint(1, 100)
        if key_roll <= 50:
            key_type = "Silver Key"
            sprite = "assets/chests/silverchest.png"
            difficulty = "silver"
        elif key_roll <= 80:
            key_type = "Golden Key"
            sprite = "assets/chests/goldenchest.png"
            difficulty = "gold"
        else:
            key_type = "Ancient Key"
            sprite = "assets/chests/ancientchest.png"
            difficulty = "ancient"

        embed = discord.Embed(
            title=f"{key_type} Encounter!",
            description=f"You’ve discovered a mysterious treasure!",
            color=0xf1c40f if "Gold" in key_type else 0xc0c0c0 if "Silver" in key_type else 0x9b59b6,
        )
        file = discord.File(sprite, filename="chest.png")
        embed.set_image(url="attachment://chest.png")
        embed.set_footer(text="Will you attempt to unlock it?")
        view = KeyEncounterView(interaction, user_id, key_type, difficulty)
        await interaction.response.send_message(embed=embed, view=view, file=file)
        return

    elif roll < 0.99:
        # ===== HERO ENCOUNTER =====
        template = pick_random_hero_template()
        inst = build_instance_from_template(template)
        rarity = inst.get("rarity", "common").lower()
        shiny = inst.get("shiny", False)
        ephemeral_setting = rarity != "legendary" or shiny != True

        # ensure required keys
        inst["name"] = inst.get("name", "Unknown Hero")
        inst["current_hp"] = inst["stats"]["hp"]

        # store the full encounter persistently
        players[user_id]["encounter"] = inst
        await save_json(PLAYER_FILE, players)

        shiny_icon = "✨ " if inst["shiny"] else ""
        rarity_color = rarity_colors.get(rarity, discord.Color.light_grey())
        embed = discord.Embed(
            title=f"👤 {shiny_icon}{inst['name']} Appears!",
            description=(
                f"{inst['name']} the {template.get('class', 'wanderer')} stands before you!\n"
                f"**{rarity.capitalize()}** — Lv.{inst['level']}"),
            color=rarity_color)
        view = RecruitView(interaction, user_id, inst)
        files = view._get_sprites(embed)
        await interaction.response.send_message(embed=embed, view=view, files=files, ephemeral=ephemeral_setting)

    else:
        # ===== MONSTER ENCOUNTER =====
        template = pick_random_monster_template()
        inst = build_instance_from_template(template)
        rarity = inst.get("rarity", "common").lower()
        ephemeral_setting = rarity != "legendary"

        inst["name"] = inst.get("name", "Unknown Monster")
        inst["current_hp"] = inst["stats"]["hp"]
        inst["shiny"] = False  # monsters never shiny (for now)

        players[user_id]["name"] = inst
        rarity_color = rarity_colors.get(rarity, discord.Color.light_grey())
        await save_json(PLAYER_FILE, players)

        embed = discord.Embed(
            title=f"A wild {inst['name']} appeared!",
            description=f"**{rarity.capitalize()}** — Lv.{inst['level']}\nPrepare for battle!",
            color=rarity_color
        )
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral_setting)
        message = await interaction.original_response()
        await start_battle(interaction, user_id, inst, message)

@bot.tree.command(name="use", description="Use a special relic to awaken a legendary hero")
@app_commands.describe(item="The relic name to use (e.g., Relic of Radiant Souls)")
async def use(interaction: discord.Interaction, item: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    p = players[user_id]
    bag = p.get("bag", {})
    item = item.strip()

    found_relic = None
    for hero_name, drops in LEGENDARY_DROPS.items():
        for d in drops:
            if d["name"].lower() == item.lower():
                found_relic = (hero_name, d)
                break
        if found_relic:
            break

    if not found_relic:
        await interaction.response.send_message("❌ That item cannot be used or isn’t a valid relic.", ephemeral=True)
        return

    hero_name, drop = found_relic

    if bag.get(item, 0) <= 0:
        await interaction.response.send_message(f"❌ You don’t have any `{item}`.", ephemeral=True)
        return

    # Confirmation view
    class ConfirmUse(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=20)

        @discord.ui.button(label="✅ Yes", style=discord.ButtonStyle.success)
        async def confirm(self, interaction_: discord.Interaction, button: discord.ui.Button):
            p["relic_charge"] = {
                "item": item,
                "hero": hero_name,
                "kills": 0,
                "goal": 100
            }
            await save_json(PLAYER_FILE, players)
            await interaction_.response.edit_message(
                content=f"🔮 You activated the **{item}**!\nDefeat **100 monsters** to fill it with souls...",
                view=None
            )

        @discord.ui.button(label="❌ No", style=discord.ButtonStyle.danger)
        async def cancel(self, interaction_: discord.Interaction, button: discord.ui.Button):
            await interaction_.response.edit_message(content="❎ Cancelled.", view=None)

    embed = discord.Embed(
        title="Use Special Relic?",
        description=(
            f"Are you sure you want to use **{item}**?\n"
            f"This will begin charging it for **{hero_name}**’s awakening (100 monster defeats required)."
        ),
        color=discord.Color.yellow()
    )
    await interaction.response.send_message(embed=embed, view=ConfirmUse(), ephemeral=True)

@bot.tree.command(name="summon", description="Summon a legendary hero using a filled relic")
@app_commands.describe(item="The filled relic name (e.g., Filled Relic of Radiant Souls)")
async def summon(interaction: discord.Interaction, item: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return

    p = players[user_id]
    bag = p.get("bag", {})
    item = item.strip().lower()

    # Check if player owns it
    if bag.get(item, 0) <= 0:
        await interaction.response.send_message(f"❌ You don’t have `{item}`.", ephemeral=True)
        return

    # Match relic → legendary hero
    hero_name = None
    for k, v in LEGENDARY_DROPS.items():
        for drop in v:
            if f"filled {drop['name']}".lower() == item:
                hero_name = k
                break
        if hero_name:
            break

    if not hero_name:
        await interaction.response.send_message("❌ That relic doesn’t summon anyone.", ephemeral=True)
        return

    # Consume relic
    bag[item] -= 1
    if bag[item] <= 0:
        del bag[item]
    await save_json(PLAYER_FILE, players)

    # Find hero data
    hero_data = next((h for h in heroes_db if h["name"].lower() == hero_name.lower()), None)
    if not hero_data:
        await interaction.response.send_message(f"⚠️ Hero data for {hero_name} not found.", ephemeral=True)
        return

    # Build encounter
    inst = build_instance_from_template(hero_data)
    inst["level"] = random.randint(50, 70)
    inst["rarity"] = "legendary"
    inst["shiny"] = True  # All summoned heroes are shiny
    inst["current_hp"] = inst["stats"]["hp"]

    embed = discord.Embed(
        title=f"⚡ {inst['name']} has been summoned!",
        description="A legendary hero answers your call...",
        color=discord.Color.gold()
    )

    sprite_path = inst.get("sprite")
    files = []
    if sprite_path and os.path.exists(sprite_path):
        file = discord.File(sprite_path, filename="sprite.png")
        embed.set_thumbnail(url="attachment://sprite.png")
        files = [file]

    view = RecruitView(interaction, user_id, inst)
    await interaction.response.send_message(embed=embed, files=files, view=view)

# -----------------------------
# PERSON ENCONTER
# -----------------------------

class RecruitView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, player_id: str, target_hero: dict):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.player_id = player_id
        self.target = target_hero

    # -------------------------
    # Sprite Handling
    # -------------------------
    def _get_sprites(self, embed: discord.Embed):
        """Attach both player + enemy sprites."""
        files = []
        # Enemy sprite
        enemy_sprite = self.target.get("sprite")
        if enemy_sprite and os.path.exists(enemy_sprite):
            f = discord.File(enemy_sprite, filename="enemy.png")
            embed.set_thumbnail(url="attachment://enemy.png")
            files.append(f)
        return files
    
    async def attempt_recruit(self, interaction: discord.Interaction, contract_name: str, base_chance: float):
        p = players.get(self.player_id)
        if not p:
            await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
            return

        bag = p.setdefault("bag", {})
        if bag.get(contract_name, 0) <= 0:
            await interaction.response.send_message(f"❌ You don’t have any **{contract_name}s** left!", ephemeral=True)
            return

        # Consume one contract
        bag[contract_name] -= 1
        if bag[contract_name] <= 0:
            del bag[contract_name]

        # Calculate success
        rarity_modifiers = {
            "common": 1.0,
            "uncommon": 0.9,
            "rare": 0.75,
            "epic": 0.5,
            "legendary": 0.3,
        }
        rarity = self.target.get("rarity", "common").lower()
        success_chance = base_chance * rarity_modifiers.get(rarity, 1.0)

        # Always use the *same encounter instance* stored in players
        encounter = p.get("encounter", self.target)

        # Attempt recruitment
        if random.random() <= success_chance:
            hero = encounter.copy()
            hero["unique_id"] = f"h{uuid.uuid4().hex[:9]}"
            hero["date_recruited"] = datetime.now().isoformat()
            hero["current_hp"] = hero["stats"]["hp"]
            hero["moveset"] = encounter.get("moveset", encounter.get("skills", []))

            # XP tracking (these fields are stable from encounter)
            hero.setdefault("xp", 0)
            hero.setdefault("xp_to_next", entity_xp_required(hero["level"] + 1))

            p["pc"].append(hero)
            if len(p["active_team"]) < 6:
                p["active_team"].append(hero["unique_id"])
            if hero["id"] not in p["codex"]:
                p["codex"].append(hero["id"])
                codex_msg = f" \n{hero["name"]} added to your Codex 📖"
            else:
                codex_msg = ""


            # Remove encounter data
            if "encounter" in p:
                del p["encounter"]

            await save_json(PLAYER_FILE, players)

            shiny_icon = "✨ " if hero.get("shiny") else ""
            embed = discord.Embed(
                title="🎉 Recruitment Successful!",
                description=f"📜 You used a **{contract_name}**!\n{shiny_icon}**{hero['name']}** joined your party!" + codex_msg,
                color=discord.Color.green(),
            )
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, attachments=[], view=None)
            else:
                await interaction.edit_original_response(embed=embed, attachments=[], view=None)

        else:
            embed = discord.Embed(
                title="❌ Recruitment Failed",
                description=f"Your **{contract_name}** failed to persuade **{encounter['name']}**!",
                color=discord.Color.red(),
            )

            # Determine outcome after failure
            if random.random() < 0.25:  # 25% chance of battle
                embed.set_footer(text="They've lost patience and attack!")
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, view=None)
                else:
                    await interaction.edit_original_response(embed=embed, view=None)

                message = await interaction.original_response()
                await start_battle(interaction, self.player_id, encounter, message)

            else:  # 75% chance to allow another attempt
                embed.set_footer(text="They hesitate... maybe you can try again.")
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, attachments=[], view=self)
                else:
                    await interaction.edit_original_response(embed=embed, attachments=[], view=self)

    # ----------------------------
    # BUTTONS
    # ----------------------------
    @discord.ui.button(label="Contract", style=discord.ButtonStyle.gray, emoji="📜")
    async def contract(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attempt_recruit(interaction, "Contract", base_chance=0.5)

    @discord.ui.button(label="Great Contract", style=discord.ButtonStyle.blurple, emoji="📘")
    async def great_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attempt_recruit(interaction, "Great Contract", base_chance=0.7)

    @discord.ui.button(label="Ancient Contract", style=discord.ButtonStyle.success, emoji="📕")
    async def ultimate_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attempt_recruit(interaction, "Ancient Contract", base_chance=0.9)

    @discord.ui.button(label="Battle", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def battle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip recruitment and begin battle immediately."""
        p = players.get(self.player_id)
        if not p:
            await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
            return

        # Ensure the encounter still exists
        encounter = p.get("encounter", self.target)
        if not encounter:
            await interaction.response.send_message("❌ No encounter available!", ephemeral=True)
            return

        # Edit the existing recruit message to show transition into battle
        embed = discord.Embed(
            title=f"⚔️ Battle — {encounter['name']} challenges you!",
            description=f"Prepare for battle against **{encounter['name']}**!",
            color=discord.Color.red()
        )
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.edit_original_response(embed=embed, view=None)

        # Start the actual battle using the same message
        message = await interaction.original_response()
        await start_battle(interaction, self.player_id, encounter, message)

# -----------------------------
# KEY ENCOUNTER
# -----------------------------

class KeyEncounterView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, player_id: str, key_type: str, difficulty: str):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.player_id = player_id
        self.key_type = key_type
        self.difficulty = difficulty

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.green, emoji="🗝️")
    async def unlock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        p = players.get(self.player_id)
        if not p:
            await interaction.followup.send("⚠️ Create a profile first with `/profile`.", ephemeral=True)
            return

        bag = p.setdefault("bag", {})
        
        key_name = f"{self.key_type}"
        if bag.get(key_name, 0) <= 0:
            await interaction.followup.send(f"❌ You don’t have a **{key_name}** to unlock this!", ephemeral=True)
            return

        # Consume one key
        bag[key_name] -= 1
        if bag[key_name] <= 0:
            del bag[key_name]
        
        # 🗑️ Delete the original encounter message (removes button)
        try:
            await interaction.message.delete()
        except Exception:
            pass

        # --------------------------
        # SILVER / GOLD — Treasure Chest
        # --------------------------
        if self.difficulty in ("silver", "gold"):
            reward_coins = random.randint(5000, 15000) if self.difficulty == "silver" else random.randint(20000, 40000)
            contracts = random.randint(4, 8)
            potions = random.randint(6, 12) if self.difficulty == "silver" else random.randint(8, 18)
            hero_spawn = random.random() < 0.20  # 20% chance for epic hero

            p["coins"] = p.get("coins", 0) + reward_coins
            if self.difficulty == "silver":
                contracttext = "Great Contracts"
                bag["Great Contract"] = bag.get("Great Contract", 0)
            else:
                contracttext = "Ancient Contracts"
                bag["Ancient Contract"] = bag.get("Ancient Contract", 0) + contracts
            bag["Potion"] = bag.get("Potion", 0) + potions
            await save_json(PLAYER_FILE, players)

            msg = f"You unlocked the {self.key_type} chest!\n\n"
            msg += f"**Rewards**\n- {reward_coins} coins\n- {potions} Potions\n- {contracts} {contracttext}"

            if hero_spawn:
                msg += f"\n🌟 An **Epic Hero** has emerged from the treasure!"
                embed = discord.Embed(title="🎁 Chest Opened!", description=msg, color=discord.Color.gold())
                await interaction.followup.send(embed=embed)
                hero_template = pick_random_hero_template_by_rarity("epic")
                hero_inst = build_instance_from_template(hero_template, is_hero=True)
                hero_inst["shiny"] = (random.randint(1, 20) == 1)
                players[self.player_id]["encounter"] = hero_inst
                await save_json(PLAYER_FILE, players)

                shiny_icon = "✨ " if hero_inst.get("shiny") else ""
                rarity_color = rarity_colors.get("epic", discord.Color.purple())
                embed = discord.Embed(
                    title=f"👤 {shiny_icon}{hero_inst['name']} Appears!",
                    description=f"{hero_inst['name']} the {hero_template.get('class', 'Hero')} stands before you!",
                    color=rarity_color
                )
                view = RecruitView(interaction, self.player_id, hero_inst)
                await interaction.followup.send(embed=embed, view=view)
                return

            embed = discord.Embed(title="🎁 Chest Opened!", description=msg, color=discord.Color.gold())
            await interaction.followup.send(embed=embed)
            return

        # --------------------------
        # ANCIENT — Dungeon Boss
        # --------------------------
        elif self.difficulty == "ancient":
            embed = discord.Embed(
                title="🏰 The Ancient Gate Opens!",
                description="A **Legendary Hero Lv.85** emerges from the ruins!",
                color=discord.Color.dark_red()
            )
            embed.set_image(url="https://yourcdn.com/ancient_gate.png")

            await interaction.followup.send(embed=embed)

            boss_template = pick_random_hero_template_by_rarity("legendary")
            boss_inst = build_instance_from_template(boss_template, level=85, is_hero=True)
            players[self.player_id]["encounter"] = boss_inst
            await save_json(PLAYER_FILE, players)

            message = await interaction.original_response()
            await start_battle(interaction, self.player_id, boss_inst, message)

# -----------------------------
# BATTLE ENCOUNTER
# -----------------------------

async def start_battle(interaction: discord.Interaction, player_id: str, enemy_inst: dict, message: discord.Message):
    p = players[player_id]
    if not p["active_team"]:
        await interaction.response.send_message("⚠️ You need at least one hero in your party to battle.", ephemeral=True)
        return

    lead_id = p["active_team"][0]
    lead = next((m for m in p["pc"] if m["unique_id"] == lead_id), None)
    if not lead:
        await interaction.response.send_message("❌ Could not find your lead hero.", ephemeral=True)
        return

    # Correct HP display
    cur_hp = lead.get("current_hp", lead["stats"]["hp"])
    max_hp = lead["stats"]["hp"]
    enemy_cur = enemy_inst.get("current_hp", enemy_inst["stats"]["hp"])
    enemy_max = enemy_inst["stats"]["hp"]

    embed = discord.Embed(
        title="⚔️ Battle Started",
        description=f"{lead['name']} vs {enemy_inst['name']}!",
        color=discord.Color.red())
    embed.add_field(name=f"{lead['name']} HP", value=f"{cur_hp}/{max_hp}")
    embed.add_field(name=f"{enemy_inst['name']} HP", value=f"{enemy_cur}/{enemy_max}")

    view = BattleView(interaction, lead, enemy_inst, p["active_team"])
    players[player_id]["encounter"] = enemy_inst
    files = view._get_sprites(embed)
    await save_json(PLAYER_FILE, players)

    msg = await message.edit(embed=embed, attachments=files, view=view)
    view.message = msg

class BattleView(discord.ui.View):
    def __init__(
        self,
        interaction: discord.Interaction,
        player_hero: dict,
        enemy: dict,
        team: list,
        *,
        carry: dict | None = None,
        start_of_battle: bool = False,
    ):
        super().__init__(timeout=120)
        self.user_id = str(interaction.user.id)
        self.player_hero = player_hero
        self.enemy = enemy
        self.team = team

        # ===== Accumulated rewards across the whole battle =====
        if carry:
            self.reward_log = carry.get("reward_log", [])
            self.total_coins = carry.get("total_coins", 0)
            self.total_trainer_xp = carry.get("total_trainer_xp", 0)
            self.total_hero_xp = carry.get("total_hero_xp", 0)
            self.drop_summary = carry.get("drop_summary", {})
        else:
            self.reward_log = []
            self.total_coins = 0
            self.total_trainer_xp = 0
            self.total_hero_xp = 0
            self.drop_summary = {}

        if start_of_battle:
            for hero_id in players[self.user_id]["active_team"]:
                hero = next((h for h in players[self.user_id]["pc"] if h["unique_id"] == hero_id), None)
                if hero and "current_hp" not in hero:
                    hero["current_hp"] = hero["stats"]["hp"]

        # Ensure current HP exists
        self.player_hero.setdefault("current_hp", self.player_hero["stats"]["hp"])
        self.enemy.setdefault("current_hp", self.enemy["stats"]["hp"])

        moves = []

        # Prefer moveset first (usually copied from encounter)
        if "moveset" in self.player_hero:
            for move in self.player_hero["moveset"]:
                if isinstance(move, dict):
                    label = move.get("move") or move.get("skill")
                else:
                    label = str(move)
                if label:
                    moves.append(label)

        # Add buttons for each move
        for move_name in moves:
            btn = discord.ui.Button(label=move_name, style=discord.ButtonStyle.primary)
            btn.callback = self.make_move_callback(move_name)
            self.add_item(btn)

        # Run button
        run_btn = discord.ui.Button(label="🏃 Retreat", style=discord.ButtonStyle.danger)
        run_btn.callback = self.run_callback
        self.add_item(run_btn)

        # Swap button
        swap_btn = discord.ui.Button(label="🔄 Swap", style=discord.ButtonStyle.secondary)
        swap_btn.callback = self.swap_callback
        self.add_item(swap_btn)

    def _carry_state(self) -> dict:
        return {
            "reward_log": self.reward_log,
            "total_coins": self.total_coins,
            "total_trainer_xp": self.total_trainer_xp,
            "total_hero_xp": self.total_hero_xp,
            "drop_summary": self.drop_summary,
        }

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id

    def make_move_callback(self, move_name: str):
        async def callback(interaction: discord.Interaction):
            await self.process_turn(interaction, move_name)
        return callback

    # -------------------------
    # Sprite Handling
    # -------------------------
    def _get_sprites(self, embed: discord.Embed):
        """Attach both player + enemy sprites."""
        files = []
        # Enemy sprite
        enemy_sprite = self.enemy.get("sprite")
        if enemy_sprite and os.path.exists(enemy_sprite):
            f = discord.File(enemy_sprite, filename="enemy.png")
            embed.set_thumbnail(url="attachment://enemy.png")
            files.append(f)
        # Player sprite
        hero_sprite = self.player_hero.get("sprite")
        if hero_sprite and os.path.exists(hero_sprite):
            f = discord.File(hero_sprite, filename="hero.png")
            embed.set_image(url="attachment://hero.png")
            files.append(f)
        return files

    # -------------------------
    # Run
    # -------------------------
    async def run_callback(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🏃 You retreated from battle!", color=discord.Color.red())
        files = self._get_sprites(embed)
        await interaction.response.edit_message(embed=embed, attachments=files, view=None)

    # -------------------------
    # Swap
    # -------------------------
    async def swap_callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        player_data = players.get(user_id)

        if not player_data or not player_data["active_team"]:
            await interaction.response.send_message("⚠️ You have no heroes to swap with.", ephemeral=True)
            return

        team_ids = player_data["active_team"]
        team_heroes = [h for h in player_data["pc"] if h["unique_id"] in team_ids]
        valid_swaps = [h for h in team_heroes if h["unique_id"] != self.player_hero["unique_id"] and h.get("current_hp", h["stats"]["hp"]) > 0]

        if not valid_swaps:
            await interaction.response.send_message("❌ No healthy heroes available to swap!", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔄 Choose a Hero to Swap",
            description="Pick one of your healthy party members:",
            color=discord.Color.blurple()
        )
        for hero in valid_swaps:
            cur_hp = hero.get("current_hp", hero["stats"]["hp"])
            max_hp = hero["stats"]["hp"]
            embed.add_field(
                name=f"{hero['name']} (Lv.{hero['level']})",
                value=f"HP: {cur_hp}/{max_hp} | ID: {hero['unique_id'][:7]}",
                inline=False
            )

        class SwapSelect(discord.ui.View):
            def __init__(self, parent, heroes):
                super().__init__(timeout=30)
                self.parent = parent
                self.heroes = {h["unique_id"]: h for h in heroes}

                select = discord.ui.Select(
                    placeholder="Select a hero...",
                    min_values=1,
                    max_values=1,
                    options=[
                        discord.SelectOption(
                            label=f"{h['name']} (Lv.{h['level']})",
                            description=f"HP: {h.get('current_hp', h['stats']['hp'])}/{h['stats']['hp']}",
                            value=h["unique_id"]
                        )
                        for h in heroes
                    ]
                )
                select.callback = self.select_callback
                self.add_item(select)

            async def select_callback(self, interaction: discord.Interaction):
                hero_id = interaction.data["values"][0]
                chosen = self.heroes[hero_id]
                chosen.setdefault("current_hp", chosen["stats"]["hp"])
                self.parent.player_hero = chosen

                embed = discord.Embed(
                    title=f"⚔️ Battle — {chosen['name']} vs {self.parent.enemy['name']}",
                    description=f"You swapped to {chosen['name']} (Lv.{chosen['level']})!",
                    color=discord.Color.orange()
                )
                embed.add_field(
                    name=f"{chosen['name']} HP",
                    value=f"{chosen['current_hp']}/{chosen['stats']['hp']}"
                )
                embed.add_field(
                    name=f"{self.parent.enemy['name']} HP",
                    value=f"{self.parent.enemy['current_hp']}/{self.parent.enemy['stats']['hp']}"
                )

                new_view = BattleView(
                    interaction,
                    chosen,
                    self.parent.enemy,
                    self.parent.team,
                    carry=self.parent._carry_state(),
                    start_of_battle=False,
                )
                files = self.parent._get_sprites(embed)
                await interaction.response.edit_message(embed=embed, attachments=files, view=new_view)
                self.stop()

        await interaction.response.edit_message(embed=embed, attachments=[], view=SwapSelect(self, valid_swaps))

    # -------------------------
    # Process Turn
    # -------------------------
    async def process_turn(self, interaction: discord.Interaction, move_name: str):
        log = []

        # --- helpers ---
        def _label(m):
            """Return a string label from a move entry that might be a dict or string."""
            if isinstance(m, dict):
                return m.get("skill") or m.get("move") or str(m)
            return str(m)

        def resolve_move(attacker: dict, defender: dict, chosen_move):
            """Handles accuracy, damage, and log line for one move."""
            mv_name = _label(chosen_move)

            # find the move data (attacker moveset entries may be dicts or strings)
            mvdata = None
            for entry in attacker.get("moveset", []):
                if isinstance(entry, dict):
                    if (_label(entry)).lower() == mv_name.lower():
                        mvdata = entry
                        break
                else:
                    if _label(entry).lower() == mv_name.lower():
                        mvdata = {"move": _label(entry), "power": 40, "acc": 100}
                        break

            if not mvdata:
                return f"{attacker['name']} tried **{mv_name}**, but it failed!"

            # accuracy
            acc_raw = mvdata.get("acc") or mvdata.get("acc.") or 100
            try:
                acc_val = int(str(acc_raw).replace("%", "").strip() or 100)
            except Exception:
                acc_val = 100
            if random.randint(1, 100) > acc_val:
                return f"{attacker['name']} used **{mv_name}**, but it missed!"

            # power / damage
            pwr_raw = mvdata.get("power", 40)
            try:
                power = int(pwr_raw)
            except Exception:
                # treat as support / non-damaging
                return f"{attacker['name']} used **{mv_name}** — it's a support move!"
            type = mvdata.get("type", "Physical")
            if type == "Magical":
                dmg = max(1, (power + attacker["stats"]["magic"] // 2) - (defender["stats"]["defense"] // 3))
            else:
                dmg = max(1, (power + attacker["stats"]["attack"] // 2) - (defender["stats"]["defense"] // 3))
            defender["current_hp"] = max(0, defender.get("current_hp", defender["stats"]["hp"]) - dmg)
            return f"{attacker['name']} used **{mv_name}** and dealt {dmg} {type} damage!"

        # --- turn order ---
        player_speed = self.player_hero["stats"]["speed"]
        enemy_speed = self.enemy["stats"]["speed"]

        enemy_mv_choice = random.choice(self.enemy.get("moveset", ["Strike"]))
        enemy_mv_label = _label(enemy_mv_choice)

        if player_speed > enemy_speed:
            order = [("player", move_name), ("enemy", enemy_mv_label)]
        elif enemy_speed > player_speed:
            order = [("enemy", enemy_mv_label), ("player", move_name)]
        else:
            order = random.choice([
                [("player", move_name), ("enemy", enemy_mv_label)],
                [("enemy", enemy_mv_label), ("player", move_name)],
            ])

        # --- execute ---
        for side, mv in order:
            if side == "player" and self.player_hero["current_hp"] > 0 and self.enemy["current_hp"] > 0:
                log.append(resolve_move(self.player_hero, self.enemy, mv))
            elif side == "enemy" and self.enemy["current_hp"] > 0 and self.player_hero["current_hp"] > 0:
                log.append(resolve_move(self.enemy, self.player_hero, mv))

            # enemy defeated => handle rewards (that function edits the message and returns here)
            if self.enemy["current_hp"] <= 0:
                log.append(f"{self.enemy['name']} was defeated 💀\n")
                await self._handle_rewards(interaction, log)
                return

            # player defeated => offer swap (edit once and return)
            if self.player_hero["current_hp"] <= 0:
                log.append(f"{self.player_hero['name']} has fallen in battle 💀")

                p = players.get(self.user_id)
                if not p:
                    embed = discord.Embed(title="❌ Defeat", description="\n".join(log), color=discord.Color.red())
                    await interaction.response.edit_message(embed=embed, view=None)
                    return

                team_ids = p.get("active_team", [])
                team_heroes = [h for h in p["pc"] if h["unique_id"] in team_ids]
                healthy = [h for h in team_heroes if h.get("current_hp", h["stats"]["hp"]) > 0]

                if not healthy:
                    embed = discord.Embed(
                        title="❌ All Heroes Have Fallen!",
                        description="\n".join(log) + "\nYour party has been defeated...",
                        color=discord.Color.red(),
                    )
                    await interaction.response.edit_message(embed=embed, view=None)
                    return

                embed = discord.Embed(
                    title="🔄 Choose a Hero to Continue",
                    description="\n".join(log) + "\nPick your next available hero:",
                    color=discord.Color.blurple(),
                )
                for h in healthy:
                    embed.add_field(
                        name=f"{h['name']} (Lv.{h['level']})",
                        value=f"HP: {h.get('current_hp', h['stats']['hp'])}/{h['stats']['hp']} | ID: {h['unique_id'][:7]}",
                        inline=False,
                    )

                class AutoSwapView(discord.ui.View):
                    def __init__(self, parent, heroes):
                        super().__init__(timeout=30)
                        self.parent = parent
                        self.heroes = {x["unique_id"]: x for x in heroes}

                        select = discord.ui.Select(
                            placeholder="Select a hero...",
                            min_values=1,
                            max_values=1,
                            options=[
                                discord.SelectOption(
                                    label=f"{x['name']} (Lv.{x['level']})",
                                    description=f"HP: {x.get('current_hp', x['stats']['hp'])}/{x['stats']['hp']}",
                                    value=x["unique_id"],
                                )
                                for x in heroes
                            ],
                        )
                        select.callback = self.select_callback
                        self.add_item(select)

                    async def select_callback(self, interaction: discord.Interaction):
                        hero_id = interaction.data["values"][0]
                        chosen = self.heroes[hero_id]
                        chosen.setdefault("current_hp", chosen["stats"]["hp"])
                        self.parent.player_hero = chosen

                        embed = discord.Embed(
                            title=f"⚔️ Battle — {chosen['name']} vs {self.parent.enemy['name']}",
                            description=f"You sent out {chosen['name']} (Lv.{chosen['level']}) to continue the fight!",
                            color=discord.Color.orange(),
                        )
                        embed.add_field(name=f"{chosen['name']} HP",
                                        value=f"{chosen['current_hp']}/{chosen['stats']['hp']}")
                        embed.add_field(name=f"{self.parent.enemy['name']} HP",
                                        value=f"{self.parent.enemy['current_hp']}/{self.parent.enemy['stats']['hp']}")

                        new_view = BattleView(
                            interaction,
                            chosen,
                            self.parent.enemy,
                            self.parent.team,
                            carry=self.parent._carry_state(),
                            start_of_battle=False,
                        )
                        files = self.parent._get_sprites(embed)
                        await interaction.response.edit_message(embed=embed, attachments=files, view=new_view)
                        self.stop()

                await interaction.response.edit_message(embed=embed, view=AutoSwapView(self, healthy))
                return

        # --- ongoing state (single edit) ---
        embed = discord.Embed(
            title=f"⚔️ Battle — {self.player_hero['name']} vs {self.enemy['name']}",
            description="\n".join(log),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name=f"{self.player_hero['name']} HP",
            value=f"{self.player_hero['current_hp']}/{self.player_hero['stats']['hp']}",
        )
        embed.add_field(
            name=f"{self.enemy['name']} HP",
            value=f"{self.enemy['current_hp']}/{self.enemy['stats']['hp']}",
        )
        files = self._get_sprites(embed)
        await interaction.response.edit_message(embed=embed, attachments=files, view=self)

    # -------------------------
    # Handle rewards
    # -------------------------
    async def _handle_rewards(self, interaction, log):
        """Handles enemy defeat, XP, coins, drops, level ups, and boss logic (full version)."""
        if self.enemy["current_hp"] > 0:
            # Still alive, just update embed mid-battle
            embed = discord.Embed(
                title=f"⚔️ Battle — {self.player_hero['name']} vs {self.enemy['name']}",
                description="\n".join(log),
                color=discord.Color.orange()
            )
            embed.add_field(
                name=f"{self.player_hero['name']} HP",
                value=f"{self.player_hero['current_hp']}/{self.player_hero['stats']['hp']}")
            embed.add_field(
                name=f"{self.enemy['name']} HP",
                value=f"{self.enemy['current_hp']}/{self.enemy['stats']['hp']}")
            files = self._get_sprites(embed)
            await interaction.response.edit_message(embed=embed, attachments=files, view=None)
            return

        next_enemy = None
        boss_msg = None
        boss_coins = 0

        # ---------------------------------
        # Boss domain (multi-phase battles)
        # ---------------------------------
        bdata = players.get(self.user_id, {}).get("boss_battle")
        if bdata:
            btype = bdata["type"]
            bdata["index"] += 1

            if bdata["index"] >= len(bdata["team"]):
                # Domain cleared!
                badge_already = players[self.user_id]["badges"].get(btype, False)
                if badge_already:
                    boss_coins = int(random.triangular(5000, 10000, 8000))
                    players[self.user_id]["coins"] += boss_coins
                    boss_msg = (
                        f"🏆 You defeated **{BOSS_LEADERS[btype]['name']}**, "
                        f"the ruler of the **{btype.capitalize()} Domain!** 👑"
                    )
                else:
                    boss_coins = int(random.triangular(10000, 25000, 10000))
                    players[self.user_id]["coins"] += boss_coins
                    players[self.user_id]["badges"][btype] = True
                    boss_msg = (
                        f"🏆 You conquered the **{btype.capitalize()} Domain** "
                        f"and earned the **{btype.capitalize()} Badge** 🎖️"
                    )
                log.append(boss_msg)

                del players[self.user_id]["boss_battle"]
                players[self.user_id]["boss_victory"] = ""
            else:
                next_enemy = bdata["team"][bdata["index"]]

        # ---------------------------------
        # Rewards (Coins / XP / Materials)
        # ---------------------------------
        p = players[self.user_id]
        p.setdefault("bag", {})

        rarity = self.enemy.get("rarity", "common").lower()

        # Coin rewards
        rarity_rewards = {
            "common": (50, 100),
            "uncommon": (150, 250),
            "rare": (250, 500),
            "epic": (500, 1000),
            "legendary": (1000, 5000),
        }
        low, high = rarity_rewards.get(rarity, (100, 200))
        coin_reward = random.randint(low, high)
        level_bonus = self.enemy.get("level", 1) // 2
        ko_coins = coin_reward + level_bonus

        p["coins"] = p.get("coins", 0) + ko_coins
        self.total_coins += ko_coins
        self.reward_log.append(f"💰 +{ko_coins} coins from {self.enemy['name']}")

        # Hero XP
        rarity_xp = {
            "common": (200, 1500),
            "uncommon": (1500, 3000),
            "rare": (3000, 6000),
            "epic": (6000, 12000),
            "legendary": (15000, 30000),
        }
        xp_low, xp_high = rarity_xp.get(rarity, (5, 10))
        hero_xp_gain = random.randint(xp_low, xp_high) + (self.enemy.get("level", 1) // 2)
        hero = self.player_hero
        if hero:
            if hero["level"] >= 100:
                self.reward_log.append(f"{hero['name']} is maxed at Lv.100 and won’t gain XP 🔒")
            else:
                award_entity_xp(hero, hero_xp_gain, log, lambda e: hero_by_id.get(e["id"], {}).get("stats", {}))
                self.total_hero_xp += hero_xp_gain
                self.reward_log.append(f"⭐ {hero['name']} +{hero_xp_gain} XP")

        # Adventurer XP
        xp_rewards = {
            "common": (20, 40),
            "uncommon": (40, 80),
            "rare": (80, 160),
            "epic": (160, 300),
            "legendary": (1000, 2000),
        }
        trainer_xp_gain = random.randint(*xp_rewards.get(rarity, (5, 10)))
        p["xp"] = p.get("xp", 0) + trainer_xp_gain
        self.total_trainer_xp += trainer_xp_gain

        # Handle adventurer level ups
        level_ups = []
        while p["xp"] >= xp_required_for_level(p["level"]):
            p["xp"] -= xp_required_for_level(p["level"])
            p["level"] += 1
            new_level = p["level"]
            coin_bonus = new_level * 200
            p["coins"] += coin_bonus
            msg = f"\n🎉 Congrats {p['username']}! You leveled up to Lv.{new_level}!\n💰 Reward: {coin_bonus} coins"

            if new_level < 10:
                amt = random.randint(2, 5)
                p["bag"]["Contract"] = p["bag"].get("Contract", 0) + amt
                msg += f" + {amt} Contracts"
            elif new_level < 20:
                amt = random.randint(2, 5)
                p["bag"]["Great Contract"] = p["bag"].get("Great Contract", 0) + amt
                msg += f" + {amt} Great Contracts"
            elif new_level < 50:
                amt = random.randint(2, 5)
                p["bag"]["Ancient Contract"] = p["bag"].get("Ancient Contract", 0) + amt
                msg += f" + {amt} Ancient Contracts"
            if new_level in (10, 25, 50, 75, 100):
                p["bag"]["Soulbound Contract"] = p["bag"].get("Soulbound Contract", 0) + 1
                msg += " + 1 Soulbound Contract"
            level_ups.append(msg)

        # ---------- Material drops (rarity-aware like Pokémon bot) ----------
        drop_summary = {}

        # Safe MATERIALS lookup
        element_tables = {}
        try:
            if MATERIALS:
                element_tables = {str(k).lower(): v for k, v in MATERIALS.items()}
        except NameError:
            element_tables = {}

        # Pick element (normalize case)
        fallback_ele = next(iter(element_tables.keys()), None)
        enemy_ele = (self.enemy.get("element") or fallback_ele or "").lower()
        element_drops = element_tables.get(enemy_ele, {})

        # Rarity relationships
        rarity_order = ["common", "uncommon", "rare", "epic", "legendary"]
        enemy_rarity = self.enemy.get("rarity", "common").lower()
        if enemy_rarity not in rarity_order:
            enemy_rarity = "common"

        # Weighted roll around enemy rarity
        def get_adjacent_rarity():
            idx = rarity_order.index(enemy_rarity)
            roll = random.random()
            if roll < 0.70:
                # same rarity
                return rarity_order[idx]
            elif roll < 0.85:
                # one tier higher (if possible)
                return rarity_order[min(idx + 1, len(rarity_order) - 1)]
            else:
                # one tier lower (if possible)
                return rarity_order[max(idx - 1, 0)]

        # Number of items to drop
        num_drops = random.randint(1, 3)

        for _ in range(num_drops):
            chosen_rarity = get_adjacent_rarity()
            pool = element_drops.get(chosen_rarity, [])

            # Fallback if chosen rarity empty
            if not pool:
                all_items = []
                for r in rarity_order:
                    all_items.extend(element_drops.get(r, []))
                pool = all_items

            if not pool:
                continue

            drop = random.choice(pool)
            item_name = (
                drop.get("name")
                or drop.get("item")
                or (str(drop.get("id")) if isinstance(drop.get("id"), (str, int)) else None)
                or str(drop)
            )

            # Add to bag
            p.setdefault("bag", {})
            p["bag"][item_name] = p["bag"].get(item_name, 0) + 1
            drop_summary[item_name] = drop_summary.get(item_name, 0) + 1

        # Log
        for item, count in drop_summary.items():
            self.drop_summary[item] = self.drop_summary.get(item, 0) + count
            self.reward_log.append(f"🎁 {count}x {item}")

        await save_json(PLAYER_FILE, players)

        # ---------------------------------
        # Multi-phase boss transition
        # ---------------------------------
        if next_enemy:
            carry = self._carry_state()
            new_view = BattleView(
                interaction,
                self.player_hero,
                next_enemy,
                self.team,
                carry=carry,
                start_of_battle=False,
            )
            desc_parts = [*log]
            if boss_msg:
                desc_parts.append(boss_msg)
            desc_parts.append("🔥 Another foe appears!")
            embed = discord.Embed(
                title=f"⚔️ Boss Battle — {self.player_hero['name']} vs {next_enemy['name']}",
                description="\n".join(desc_parts),
                color=discord.Color.orange()
            )
            files = new_view._get_sprites(embed)
            await interaction.response.edit_message(embed=embed, attachments=files, view=None)
            return

        # ---------------------------------
        # Final victory summary
        # ---------------------------------
        summary_lines = [
            f"💰 Coins: **{self.total_coins + boss_coins}**",
            f"⭐ Adventurer XP: **{self.total_trainer_xp}**",
            f"⭐ Hero XP: **{self.total_hero_xp}**",
        ]
        for item, count in self.drop_summary.items():
            summary_lines.append(f"🎁 {count}x {item}")

        if boss_msg:
            self.reward_log.append(boss_msg)

        respawn_orb = p.get("respawn_orb")
        if respawn_orb:
            respawn_orb["kills"] += 1
            kills = respawn_orb["kills"]
            goal = respawn_orb["goal"]
            if kills >= goal:
                filled_name = f"Filled {respawn_orb['item']}"
                base_name = respawn_orb["item"].lower()
                if base_name in p["bag"]:
                    p["bag"][base_name] -= 1
                    if p["bag"][base_name] <= 0:
                        del p["bag"][base_name]
                p["bag"][filled_name.lower()] = p["bag"].get(filled_name.lower(), 0) + 1
                del p["respawn_orb"]
                summary_lines.append(f"✨ Your {filled_name} is complete! You can now summon {respawn_orb['name']}\n")
            else:
                summary_lines.append(f"⚡ {kills}/{goal} souls absorbed into {respawn_orb['item']}...\n")
            await save_json(PLAYER_FILE, players)

        embed = discord.Embed(
            title="🎉 Victory",
            description="\n".join(log),
            color=discord.Color.green()
        )
        embed.add_field(name="Rewards", value="\n".join(summary_lines), inline=False)

        if level_ups:
            embed.add_field(name="Level Ups!", value="".join(level_ups), inline=False)

        files = []
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, attachments=files, view=None)
        else:
            await interaction.response.edit_message(embed=embed, attachments=files, view=None)

# -----------------------------
# PROFILE
# -----------------------------

@bot.tree.command(name="profile", description="View your adventurer profile or someone else's")
@app_commands.describe(user="(Optional) Mention another adventurer to view their profile")
async def profile(interaction: discord.Interaction, user: discord.User | None = None):
    target_user = user or interaction.user
    user_id = str(target_user.id)
    p = players.get(user_id)

    # Initialize new player if not exists (only if viewing self)
    if not p:
        if target_user == interaction.user:
            players[user_id] = {
                "username": target_user.name,
                "coins": 20000,
                "xp": 0,
                "level": 1,
                "badges": {k: False for k in BOSS_LEADERS.keys()},
                "bag": {"Potion": 10, "Contract": 10, "Great Contract": 2},
                "pc": [],
                "active_team": [],
                "codex": []
            }
            await save_json(PLAYER_FILE, players)

            view = StarterSelectView(user_id)
            await interaction.response.send_message(
                "🎉 Welcome, Adventurer! Please choose your starter Hero:",
                view=view,
                ephemeral=True
            )
            return
        else:
            await interaction.response.send_message(
                f"⚠️ {target_user.mention} doesn’t have a profile yet.",
                ephemeral=True
            )
            return

    # ---------------------------
    # Profile Embed
    # ---------------------------
    embed = discord.Embed(
        title=f"👤 Adventurer {p['username']}",
        color=discord.Color.gold()
    )

    embed.add_field(name="🏆 Level", value=p["level"])
    embed.add_field(name="💰 Coins", value=p["coins"])

    # Codex progress
    codex_count = len(p.get("codex", []))
    embed.add_field(name="📖 Codex", value=f"{codex_count}/100")

    # Badges
    badge_emojis = {
        "earth": "🌍", "fire": "🔥", "water": "💧", "lightning": "⚡",
        "shadow": "🌑", "holy": "🌟", "ice": "❄️", "nature": "🌿"
    }
    earned = [
        f"{badge_emojis.get(k, '🏅')} {k.title()} Badge"
        for k, v in p.get("badges", {}).items() if v
    ]
    embed.add_field(
        name="🏅 Boss Badges",
        value="\n".join(earned) if earned else "None earned yet.",
        inline=False
    )

    # Bag preview
    bag = p.get("bag", {})
    if bag:
        shown = ", ".join([f"{k} ×{v}" for k, v in list(bag.items())[:5]])
        if len(bag) > 5:
            shown += f", + {len(bag) - 5} more"
    else:
        shown = "Empty"
    embed.add_field(name="🎒 Bag", value=shown, inline=False)

    # Active Party
    if p["active_team"]:
        heroes = []
        for i, hid in enumerate(p["active_team"], 1):
            h = next((x for x in p["pc"] if x["unique_id"] == hid), None)
            if h:
                rarity = h['rarity'].capitalize()
                shiny_star = "✨ " if h.get("shiny") else ""
                lock_icon = "🔒 " if h.get("locked") else ""
                heroes.append(
                    f"**Slot {i}:** {lock_icon}{shiny_star}**{h['name']}** — {rarity} Lv.{h['level']} — `{h['unique_id'][:6]}`"
                )
        embed.add_field(name="👥 Party", value="\n".join(heroes), inline=False)
    else:
        embed.add_field(name="👥 Party", value="No Heroes recruited yet", inline=False)

    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.set_footer(text="Use /explore to get started")

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show the top coin holders")
async def leaderboard(interaction: discord.Interaction):
    if not players:
        await interaction.response.send_message("Nobody has any coins yet.")
        return

    # Sort by player coins
    top10 = sorted(players.items(), key=lambda x: x[1]["coins"], reverse=True)[:10]

    lines = []
    for i, (uid, pdata) in enumerate(top10, start=1):
        bal = pdata["coins"]
        user = interaction.client.get_user(int(uid)) or await interaction.client.fetch_user(int(uid))
        name = user.display_name if user else f"User {uid}"
        lines.append(f"**{i}.** {name} — 💰 {bal}")

    embed = discord.Embed(
        title="🏆 Coin Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="bag", description="View your inventory and materials")
async def bag(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return

    p = players[user_id]
    bag = p.get("bag", {})
    if not bag:
        await interaction.response.send_message("🎒 Your bag is empty!", ephemeral=True)
        return

    # Load items.json dynamically
    with open(ITEMS_FILE, "r", encoding="utf-8") as f:
        items_data = json.load(f)

    CONSUMABLES = {i["name"]: i for i in items_data["consumables"]}
    KEYS = {i["name"]: i for i in items_data["keys"]}
    MATERIALS = items_data["materials"]

    embed = discord.Embed(
        title=f"🎒 {p['username']}'s Inventory",
        description="Your collected items and materials",
        color=discord.Color.blurple()
    )

    # --- Keys ---
    key_lines = [
        f"**{name}** ×{bag[name]} — *{KEYS[name]['effect']}*" for name in bag.keys() if name in KEYS
    ]
    if key_lines:
        embed.add_field(name="🗝️ Keys", value="\n".join(key_lines), inline=False)

    # --- Consumables ---
    cons_lines = [
        f"**{name}** ×{bag[name]} — *{CONSUMABLES[name]['effect']}*"
        for name in bag.keys() if name in CONSUMABLES
    ]
    if cons_lines:
        embed.add_field(name="🧴 Consumables", value="\n".join(cons_lines), inline=False)

    # --- Materials grouped by element ---
    element_icons = {
        "fire": "🔥", "water": "💧", "earth": "🌍", "air": "🌬️",
        "ice": "❄️", "shadow": "🌑", "holy": "🌟", "lightning": "⚡",
        "nature": "🌿", "arcane": "🔮"
    }

    for element, rarities in MATERIALS.items():
        found = []
        for rarity, mats in rarities.items():
            for m in mats:
                qty = bag.get(m["name"], 0)
                if qty > 0:
                    found.append(f"• {m['name']} ×{qty} ({rarity.title()})")
        if found:
            icon = element_icons.get(element, "✨")
            embed.add_field(
                name=f"{icon} {element.title()} Materials",
                value="\n".join(found),
                inline=False
            )

    # --- Misc Items ---
    known_items = set(CONSUMABLES.keys()) | set(KEYS.keys())
    for element, rarities in MATERIALS.items():
        for rarity, mats in rarities.items():
            known_items.update([m["name"] for m in mats])

    misc_items = [
        f"**{k}** ×{v}"
        for k, v in bag.items()
        if k not in known_items
    ]
    if misc_items:
        embed.add_field(name="📦 Other Items", value="\n".join(misc_items), inline=False)

    embed.set_footer(text=f"💰 Coins: {p.get('coins', 0)} | Use /sell or /sellall")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="xp", description="Check your XP progress and next level rewards")
async def xp(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return
    await show_xp(interaction)

@bot.tree.command(name="level", description="Check your XP progress and next level rewards")
async def level(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return
    await show_xp(interaction)

# -----------------------------
# PARTY / BARRACKS SYSTEM
# -----------------------------

@bot.tree.command(name="party", description="View your current hero party")
async def party(interaction: discord.Interaction):
    user = interaction.user
    user_id = str(interaction.user.id)

    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    
    p = players.get(user_id)
    

    if not p or not p.get("active_team"):
        await interaction.response.send_message(
            "👥 Your party is empty. Recruit heroes with `/explore`!",
            ephemeral=True
        )
        return

    heroes = []
    for i, hid in enumerate(p["active_team"], 1):
        h = next((x for x in p["pc"] if x["unique_id"] == hid), None)
        if h:
            shiny = "✨" if h.get("shiny") else ""
            locked = "🔒" if h.get("locked") else ""
            heroes.append(
                f"**Slot {i}:** {shiny}{locked} **{h['name']}** "
                f"(Lv.{h['level']}) — ❤️ {h['current_hp']}/{h['stats']['hp']} "
            )

    embed = discord.Embed(
        title=f"👥 {p['username']}'s Party",
        description="\n".join(heroes) if heroes else "No active heroes.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="team", description="View your current hero party")
async def team(interaction: discord.Interaction):
    user = interaction.user
    user_id = str(interaction.user.id)

    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    
    p = players.get(user_id)

    if not p or not p.get("active_team"):
        await interaction.response.send_message(
            "👥 Your party is empty. Recruit heroes with `/explore`!",
            ephemeral=True
        )
        return

    heroes = []
    for i, hid in enumerate(p["active_team"], 1):
        h = next((x for x in p["pc"] if x["unique_id"] == hid), None)
        if h:
            shiny = "✨" if h.get("shiny") else ""
            locked = "🔒" if h.get("locked") else ""
            heroes.append(
                f"**Slot {i}:** {shiny}{locked} **{h['name']}** "
                f"(Lv.{h['level']}) — ❤️ {h['current_hp']}/{h['stats']['hp']} "
            )

    embed = discord.Embed(
        title=f"👥 {p['username']}'s Party",
        description="\n".join(heroes) if heroes else "No active heroes.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="barracks", description="View all stored heroes (excluding your active party)")
async def barracks(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    
    p = players.get(user_id)
    if not p or not p.get("pc"):
        await interaction.response.send_message(
            "📦 Your barracks are empty. Recruit heroes with `/explore`!",
            ephemeral=True
        )
        return

    active_ids = set(p.get("active_team", []))
    stored_heroes = [h for h in p["pc"] if h["unique_id"] not in active_ids]

    if not stored_heroes:
        await interaction.response.send_message(
            "📦 Your barracks are empty (your active party doesn’t appear here).",
            ephemeral=True
        )
        return

    view = PCView(user_id, stored_heroes)
    embed, files = view.get_embed()
    await interaction.response.send_message(embed=embed, files=files, view=view, ephemeral=True)

@bot.tree.command(name="barracksclear", description="Clear all stored heroes (except active party)")
async def barracksclear(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ You don't have a profile yet. Use `/profile` first.", ephemeral=True)
        return

    view = ConfirmClearView(user_id)
    await interaction.response.send_message(
        "⚠️ Are you sure you want to clear your barracks? Active party will remain safe.",
        view=view, ephemeral=True
    )

@bot.tree.command(name="hero", description="View details for one of your heroes")
@app_commands.describe(id="Hero ID (e.g. hf7048f) or team slot number (1-6)")
async def hero(interaction: discord.Interaction, id: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    
    p = players.get(user_id)
    if not p or not p.get("pc"):
        await interaction.response.send_message("❌ You don't have any heroes.", ephemeral=True)
        return

    # Allow lookup by team slot or by hero ID
    hero = None
    if id.isdigit():
        slot = int(id)
        if 1 <= slot <= len(p.get("active_team", [])):
            hero_id = p["active_team"][slot - 1]
            hero = next((h for h in p["pc"] if h["unique_id"] == hero_id), None)
    else:
        hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(id.lower())), None)

    if not hero:
        await interaction.response.send_message(f"❌ No hero found matching `{id}`.", ephemeral=True)
        return

    # Prepare IVs and EVs
    ivs = ", ".join([f"{k}:{v}" for k, v in hero.get("ivs", {}).items()])
    evs = ", ".join([f"{k}:{v}" for k, v in hero.get("evs", {}).items()])

    # Format moveset
    moveset = hero.get("moveset", [])
    if moveset and isinstance(moveset[0], dict):
        move_lines = [
            f"**{m.get('move') or m.get('skill')}** — {m.get('type', '?')} | Power: {m.get('power', '-')} | Acc: {m.get('acc', '-')}"
            for m in moveset
        ]
        moves_text = "\n".join(move_lines)
    else:
        moves_text = ", ".join(moveset) if moveset else "None"

    rarity = hero['rarity'].capitalize()
    # Build the embed
    embed = discord.Embed(
        title=f"{hero['name']} — Lv.{hero['level']} {hero['class']}",
        color=discord.Color.blurple()
    )
    embed.add_field(name="❤️ HP", value=f"{hero.get('current_hp', hero['stats']['hp'])}/{hero['stats']['hp']}")
    embed.add_field(name="⚔️ Attack", value=hero['stats']['attack'])
    embed.add_field(name="🛡️ Defense", value=hero['stats']['defense'])
    embed.add_field(name="🎓 XP", value=f"{hero.get('xp', 0)} / {entity_xp_required(hero['level']+1)}")
    embed.add_field(name="✨ Magic", value=hero['stats']['magic'])
    embed.add_field(name="⚡ Speed", value=hero['stats']['speed'])
    embed.add_field(name="🎭 Traits", value=ivs or "None", inline=False)
    embed.add_field(name="🏋️ Training", value=evs or "None", inline=False)
    embed.add_field(name="🎯 Moves", value=moves_text, inline=False)
    embed.set_footer(text=f"ID: {hero['unique_id']} | {''.join(hero.get('element', []))} | {rarity}")

    # Include sprite if available
    if hero.get("sprite") and os.path.exists(hero["sprite"]):
        file = discord.File(hero["sprite"], filename="sprite.png")
        embed.set_thumbnail(url="attachment://sprite.png")
        await interaction.response.send_message(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="elixir", description="Use Elixirs to level up a hero")
@app_commands.describe(
    identifier="Hero ID (e.g. ha1b2) or slot number (1-6)",
    amount="Number of Elixirs to use (optional)"
)
async def elixir(interaction: discord.Interaction, identifier: str, amount: int = 1):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)
    if not p or "bag" not in p:
        await interaction.response.send_message("❌ You don’t have any items yet.", ephemeral=True)
        return

    # Case-insensitive bag lookup
    bag = {k.lower(): v for k, v in p["bag"].items()}
    elixirs = bag.get("elixir", 0)
    if elixirs <= 0:
        await interaction.response.send_message("❌ You have no Elixirs! Buy some from the shop.", ephemeral=True)
        return

    # Try to resolve the hero by ID or slot number
    hero = None
    if identifier.isdigit():
        slot = int(identifier)
        if 1 <= slot <= len(p.get("active_team", [])):
            hero = next((h for h in p["pc"] if h["unique_id"] == p["active_team"][slot - 1]), None)
    else:
        hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(identifier.lower())), None)

    if not hero:
        await interaction.response.send_message(f"❌ No hero found matching `{identifier}`.", ephemeral=True)
        return

    current_level = hero["level"]
    if current_level >= 100:
        await interaction.response.send_message("🔒 That hero is already Lv.100.", ephemeral=True)
        return

    # Clamp usable amount
    max_usable = min(amount or 1, elixirs, 100 - current_level)
    if max_usable <= 0:
        await interaction.response.send_message("⚠️ You can’t use that many Elixirs!", ephemeral=True)
        return

    # Deduct Elixirs (case-insensitive)
    for key in list(p["bag"].keys()):
        if key.lower() == "elixir":
            p["bag"][key] -= max_usable
            if p["bag"][key] <= 0:
                del p["bag"][key]
            break

    # Level up hero
    hero["level"] += max_usable
    if "_base_stats" not in hero:
        hero["_base_stats"] = hero.get("stats", {}).copy()
    recalc_stats_from_base(hero, hero["_base_stats"])

    await save_json(PLAYER_FILE, players)

    embed = discord.Embed(
        title="🧪 Elixir Used!",
        description=(
            f"✨ **{hero['name']}** grew from **Lv.{current_level} → Lv.{hero['level']}!**\n"
            f"📈 Used **{max_usable}× Elixir{'s' if max_usable > 1 else ''}**."
        ),
        color=discord.Color.green()
    )
    embed.add_field(
        name="New Stats",
        value=", ".join([f"{k}: {v}" for k, v in hero["stats"].items()])
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="heal", description="Fully heal your active party using potions.")
async def heal(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)

    if not p:
        await interaction.response.send_message("❌ You don’t have a profile yet. Use `/explore` first!", ephemeral=True)
        return

    # Ensure they have an active team
    if not p.get("active_team"):
        await interaction.response.send_message("⚠️ You have no active heroes to heal!", ephemeral=True)
        return

    # Case-insensitive potion check
    bag = {k.lower(): v for k, v in p.get("bag", {}).items()}
    potions = bag.get("potion", 0)
    if potions <= 0:
        await interaction.response.send_message("❌ You have no Potions! Buy them from the shop.", ephemeral=True)
        return

    # Heal all active team members
    team_ids = p["active_team"]
    healed = []
    for hero in p["pc"]:
        if hero["unique_id"] in team_ids:
            cur_hp = hero.get("current_hp", hero["stats"]["hp"])
            max_hp = hero["stats"]["hp"]
            if cur_hp < max_hp:
                hero["current_hp"] = max_hp
                healed.append(hero["name"])

    if not healed:
        await interaction.response.send_message("💤 All your heroes are already at full HP!", ephemeral=True)
        return

    # Consume one potion (you can make it cost more later)
    for key in list(p["bag"].keys()):
        if key.lower() == "potion":
            p["bag"][key] -= 1
            if p["bag"][key] <= 0:
                del p["bag"][key]
            break

    await save_json(PLAYER_FILE, players)

    embed = discord.Embed(
        title="💖 Party Healed!",
        description=f"🧴 Used **1 Potion** to fully heal your active team!",
        color=discord.Color.pink()
    )
    embed.add_field(
        name="Healed Heroes",
        value="\n".join([f"✨ {n}" for n in healed]),
        inline=False
    )
    embed.set_footer(text=f"Remaining Potions: {p.get('bag', {}).get('Potion', 0)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------------
# PARTY MANAGEMENT COMMANDS
# -----------------------------

@bot.tree.command(name="move", description="Swap two Heroes in your active party slots")
@app_commands.describe(
    from_slot="The slot number of the Hero you want to move",
    to_slot="The slot number to swap with")
async def move(interaction: discord.Interaction, from_slot: int, to_slot: int):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return

    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    p = players[user_id]
    team = p.get("active_team", [])

    # Validate team presence
    if not team:
        await interaction.response.send_message("❌ You don’t have an active team set.", ephemeral=True)
        return

    max_slots = len(team)
    if from_slot < 1 or from_slot > max_slots or to_slot < 1 or to_slot > max_slots:
        await interaction.response.send_message(
            f"❌ Invalid slot numbers. You currently have {max_slots} Heroes in your team.",
            ephemeral=True)
        return

    # Ensure both slots are actually filled
    if not team[from_slot - 1] or not team[to_slot - 1]:
        await interaction.response.send_message(
            "❌ One of the selected slots is empty — cannot swap.",
            ephemeral=True)
        return

    # Swap (0-indexed)
    hero_a_id = team[from_slot - 1]
    hero_b_id = team[to_slot - 1]
    hero_a = next((h for h in p["pc"] if h["unique_id"] == hero_a_id), None)
    hero_b = next((h for h in p["pc"] if h["unique_id"] == hero_b_id), None)

    if (hero_a and hero_a.get("locked")) or (hero_b and hero_b.get("locked")):
        await interaction.response.send_message(
            "🔒 One or both of the selected heroes are locked and cannot be moved.",
            ephemeral=True)
        return

    team[from_slot - 1], team[to_slot - 1] = team[to_slot - 1], team[from_slot - 1]

    await save_json(PLAYER_FILE, players)

    await interaction.response.send_message(
        f"✅ Swapped slot {from_slot} with slot {to_slot}.",
        ephemeral=True)

@bot.tree.command(name="partyadd", description="Add a hero from your barracks to your active party")
@app_commands.describe(id="Hero ID to add to your party (e.g. h6d21)")
async def partyadd(interaction: discord.Interaction, id: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)

    if not p or not p.get("pc"):
        await interaction.response.send_message("❌ You don't have any heroes in your barracks.", ephemeral=True)
        return

    hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(id.lower())), None)
    if not hero:
        await interaction.response.send_message(f"❌ No hero found with ID `{id}`.", ephemeral=True)
        return

    if hero["unique_id"] in p["active_team"]:
        await interaction.response.send_message("⚠️ That hero is already in your party.", ephemeral=True)
        return

    if len(p["active_team"]) >= 6:
        await interaction.response.send_message("⚠️ Your party is full (max 6 heroes).", ephemeral=True)
        return

    p["active_team"].append(hero["unique_id"])
    await save_json(PLAYER_FILE, players)
    await interaction.response.send_message(
        f"✅ Added **{hero['name']}** (Lv.{hero['level']}) to your party! (`{hero['unique_id']}`)",
        ephemeral=True
    )

@bot.tree.command(name="partyremove", description="Remove a hero from your active party by ID or slot number")
@app_commands.describe(identifier="Hero ID (e.g. hfaf4) or slot number (1-6)")
async def partyremove(interaction: discord.Interaction, identifier: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)
    
    if not p or not p.get("active_team"):
        await interaction.response.send_message("👥 You have no active heroes to remove.", ephemeral=True)
        return

    team = p["active_team"]
    hero = None

    # Check if input is a slot number
    if identifier.isdigit():
        slot = int(identifier)
        if 1 <= slot <= len(team):
            hero = next((h for h in p["pc"] if h["unique_id"] == team[slot - 1]), None)
    else:
        # Otherwise, treat as hero ID
        hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(identifier.lower()) and h["unique_id"] in team), None)

    if not hero:
        await interaction.response.send_message(f"❌ No hero found matching `{identifier}` in your party.", ephemeral=True)
        return

    if hero.get("locked"):
        await interaction.response.send_message(
            f"🔒 **{hero['name']}** is locked and cannot be removed from your party.",
            ephemeral=True)
        return
    
    p["active_team"].remove(hero["unique_id"])
    await save_json(PLAYER_FILE, players)
    await interaction.response.send_message(
        f"🗑️ Removed **{hero['name']}** (Lv.{hero['level']}) from your party. (`{hero['unique_id']}`)",
        ephemeral=True
    )

@bot.tree.command(name="teamadd", description="Add a hero from your barracks to your active party")
@app_commands.describe(id="Hero ID to add to your party (e.g. h6d21)")
async def teamadd(interaction: discord.Interaction, id: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)

    if not p or not p.get("pc"):
        await interaction.response.send_message("❌ You don't have any heroes in your barracks.", ephemeral=True)
        return

    hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(id.lower())), None)
    if not hero:
        await interaction.response.send_message(f"❌ No hero found with ID `{id}`.", ephemeral=True)
        return

    if hero["unique_id"] in p["active_team"]:
        await interaction.response.send_message("⚠️ That hero is already in your party.", ephemeral=True)
        return

    if len(p["active_team"]) >= 6:
        await interaction.response.send_message("⚠️ Your party is full (max 6 heroes).", ephemeral=True)
        return

    p["active_team"].append(hero["unique_id"])
    await save_json(PLAYER_FILE, players)
    await interaction.response.send_message(
        f"✅ Added **{hero['name']}** (Lv.{hero['level']}) to your party! (`{hero['unique_id']}`)",
        ephemeral=True
    )

@bot.tree.command(name="teamremove", description="Remove a hero from your active party by ID or slot number")
@app_commands.describe(identifier="Hero ID (e.g. hfaf4) or slot number (1-6)")
async def teamremove(interaction: discord.Interaction, identifier: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)
    
    if not p or not p.get("active_team"):
        await interaction.response.send_message("👥 You have no active heroes to remove.", ephemeral=True)
        return

    team = p["active_team"]
    hero = None

    # Check if input is a slot number
    if identifier.isdigit():
        slot = int(identifier)
        if 1 <= slot <= len(team):
            hero = next((h for h in p["pc"] if h["unique_id"] == team[slot - 1]), None)
    else:
        # Otherwise, treat as hero ID
        hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(identifier.lower()) and h["unique_id"] in team), None)

    if not hero:
        await interaction.response.send_message(f"❌ No hero found matching `{identifier}` in your party.", ephemeral=True)
        return

    if hero.get("locked"):
        await interaction.response.send_message(
            f"🔒 **{hero['name']}** is locked and cannot be removed from your party.",
            ephemeral=True)
        return
    
    p["active_team"].remove(hero["unique_id"])
    await save_json(PLAYER_FILE, players)
    await interaction.response.send_message(
        f"🗑️ Removed **{hero['name']}** (Lv.{hero['level']}) from your party. (`{hero['unique_id']}`)",
        ephemeral=True
    )

@bot.tree.command(name="lock", description="Lock a hero so they cannot be cleared or sold")
@app_commands.describe(identifier="Hero ID (e.g. hf04f) or slot number (1-6)")
async def lock(interaction: discord.Interaction, identifier: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)
    if not p or not p.get("pc"):
        await interaction.response.send_message("❌ You have no heroes.", ephemeral=True)
        return

    hero = None
    if identifier.isdigit():
        slot = int(identifier)
        if 1 <= slot <= len(p["active_team"]):
            hero = next((h for h in p["pc"] if h["unique_id"] == p["active_team"][slot - 1]), None)
    else:
        hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(identifier.lower())), None)

    if not hero:
        await interaction.response.send_message(f"❌ No hero found matching `{identifier}`.", ephemeral=True)
        return

    hero["locked"] = True
    await save_json(PLAYER_FILE, players)
    await interaction.response.send_message(
        f"🔒 **{hero['name']}** (`{hero['unique_id']}`) is now locked and safe from deletion.",
        ephemeral=True)

@bot.tree.command(name="unlock", description="Unlock a hero so they can be cleared or released")
@app_commands.describe(identifier="Hero ID (e.g. hf71f) or slot number (1-6)")
async def unlock(interaction: discord.Interaction, identifier: str):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)
    if not p or not p.get("pc"):
        await interaction.response.send_message("❌ You have no heroes.", ephemeral=True)
        return

    hero = None
    if identifier.isdigit():
        slot = int(identifier)
        if 1 <= slot <= len(p["active_team"]):
            hero = next((h for h in p["pc"] if h["unique_id"] == p["active_team"][slot - 1]), None)
    else:
        hero = next((h for h in p["pc"] if h["unique_id"].lower().startswith(identifier.lower())), None)

    if not hero:
        await interaction.response.send_message(f"❌ No hero found matching `{identifier}`.", ephemeral=True)
        return

    hero["locked"] = False
    await save_json(PLAYER_FILE, players)
    await interaction.response.send_message(
        f"🔓 **{hero['name']}** (`{hero['unique_id']}`) is now unlocked.",
        ephemeral=True)

@bot.tree.command(name="locked", description="View your locked heroes")
async def locked(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    p = players.get(user_id)
    if not p or not p.get("pc"):
        await interaction.response.send_message("📦 You don't have any heroes.", ephemeral=True)
        return

    locked_heroes = [h for h in p["pc"] if h.get("locked")]
    if not locked_heroes:
        await interaction.response.send_message("🔓 You have no locked heroes.", ephemeral=True)
        return

    embed = discord.Embed(title="🔒 Locked Heroes", color=discord.Color.gold())
    for h in locked_heroes:
        embed.add_field(
            name=f"{h['name']} (Lv.{h['level']})",
            value=f"HP {h['current_hp']}/{h['stats']['hp']} | ID {h['unique_id'][:7]}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------------
# BOSSES
# -----------------------------

BOSS_LEADERS = {
    "undead":     {"emoji": "💀", "name": "Necrolord Vorath"},
    "fire":       {"emoji": "🔥", "name": "Ignar the Flame Titan"},
    "ice":        {"emoji": "❄️", "name": "Frost Queen Lyria"},
    "earth":      {"emoji": "🌋", "name": "Thoran the Mountain King"},
    "holy":       {"emoji": "🌟", "name": "Seraphine the Radiant"},
    "shadow":     {"emoji": "🌑", "name": "Umbra, Mistress of Night"},
    "lightning":  {"emoji": "⚡", "name": "Tempest the Sky Tyrant"},
    "air":        {"emoji": "🌪️", "name": "Zephyra, Storm Sovereign"},
    "water":      {"emoji": "💧", "name": "Tidebreaker Kael’nar"},
    "nature":     {"emoji": "🌿", "name": "Eldros, Guardian of the Wilds"},
    "arcane":     {"emoji": "🔮", "name": "Magus Eternis"},
}

BOSS_COOLDOWN_HOURS = 12

@bot.tree.command(name="bosses", description="View all available Bosses")
async def bosses(interaction: discord.Interaction):
    lines = []
    for btype, data in BOSS_LEADERS.items():
        lines.append(f"{data['emoji']} **{data['name']}** — {btype.capitalize()} Domain")
    embed = discord.Embed(
        title="🏰 Boss Domains",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Use /boss name:<type> to challenge one!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="boss", description="Challenge a powerful Boss")
@app_commands.describe(name="The boss type (fire, ice, earth, etc.)")
async def boss(interaction: discord.Interaction, name: str):
    btype = name.lower()
    if btype not in BOSS_LEADERS:
        await interaction.response.send_message(
            f"❌ Invalid boss name. Use `/bosses` to see the list.",
            ephemeral=True
        )
        return

    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return
    
    p = players.setdefault(user_id, {})
    p.setdefault("boss_cooldowns", {})

    # Cooldown
    now = datetime.now(timezone.utc)
    last = p["boss_cooldowns"].get(btype)
    if last:
        last = datetime.fromisoformat(last)
        remaining = (last + timedelta(hours=BOSS_COOLDOWN_HOURS)) - now
        if remaining.total_seconds() > 0:
            hrs, mins = divmod(int(remaining.total_seconds()) // 60, 60)
            await interaction.response.send_message(
                f"⏳ You must wait **{hrs}h {mins}m** before fighting {btype.capitalize()} again.",
                ephemeral=True
            )
            return

    leader = BOSS_LEADERS[btype]

    # Pull monsters of this element
    valid = [m for m in monsters_db if btype.capitalize() in m.get("types", []) or btype.lower() in [t.lower() for t in m.get("types", [])]]
    if len(valid) < 3:
        await interaction.response.send_message(f"⚠️ Not enough monsters for `{btype}` boss.", ephemeral=True)
        return

    # Generate 3-6 monsters for this boss battle
    chosen = random.sample(valid, random.randint(3, 6))
    boss_team = []
    for template in chosen:
        lvl = int(random.triangular(35, 60, 45))
        inst = build_instance_from_template(template, level=lvl)
        boss_team.append(inst)

    players[user_id]["boss_battle"] = {
        "type": btype,
        "team": boss_team,
        "index": 0
    }
    await save_json(PLAYER_FILE, players)

    if not p.get("active_team"):
        await interaction.response.send_message("⚠️ You need at least one hero in your team to fight!", ephemeral=True)
        return

    lead_id = p["active_team"][0]
    lead = next((h for h in p["pc"] if h["unique_id"] == lead_id), None)
    if not lead:
        await interaction.response.send_message("❌ Could not find your lead hero.", ephemeral=True)
        return

    enemy = boss_team[0]
    embed = discord.Embed(
        title=f"🏰 {leader['name']} — {btype.capitalize()} Domain",
        description=f"Your {lead['name']} faces {leader['name']}'s {enemy['name']}!",
        color=discord.Color.orange()
    )
    embed.add_field(name=f"{lead['name']} HP", value=lead["stats"]["hp"])
    embed.add_field(name=f"{enemy['name']} HP", value=enemy["stats"]["hp"])

    view = BattleView(interaction, lead, enemy, p["active_team"], start_of_battle=True)
    files = view._get_sprites(embed)
    await interaction.response.send_message(embed=embed, files=files, view=view)

# -----------------------------
# AH COMMANDS
# -----------------------------

async def show_auctionhouse(interaction: discord.Interaction, search: str = None):
    if not auctions:
        await interaction.response.send_message("🏪 The Auction House is empty.", ephemeral=True)
        return
    
    embed = discord.Embed(title="🏪 Auction House", color=discord.Color.gold())

    # Apply search filter
    items = list(auctions.items())
    if search:
        search = search.lower()
        items = [
            (auc_id, auc)
            for auc_id, auc in items
            if search in auc["item"].lower()
        ]

    if not items:
        await interaction.response.send_message(
            f"❌ No auctions found matching **{search}**.", ephemeral=True
        )
        return

    # Limit display to 10 results
    for auc_id, auc in items[:10]:
        seller_name = players.get(auc["seller"], {}).get("username", "Unknown")
        try:
            end_ts = int(datetime.fromisoformat(auc["end_time"]).timestamp())
            end_str = f"⏳ Ends <t:{end_ts}:R>"
        except Exception:
            end_str = "⏳ End time unknown"

        embed.add_field(
            name=f"ID: {auc_id} — {auc['item']} ×{auc['amount']}",
            value=f"💰 {auc['price']} each | Seller: {seller_name}\n{end_str}",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="auctionhouse", description="View the Player Market")
@app_commands.describe(search="Optional search term for item name")
async def auctionhouse(interaction: discord.Interaction, search: str = None):
    await show_auctionhouse(interaction, search)

@bot.tree.command(name="ah", description="View the Player Market (alias)")
@app_commands.describe(search="Optional search term for item name")
async def ah(interaction: discord.Interaction, search: str = None):
    await show_auctionhouse(interaction, search)

# -----------------------------
# SHOP COMMANDS
# -----------------------------

@bot.tree.command(name="shop", description="View the Adventurer Shop")
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛒 Adventurer Shop",
        description="Buy consumables and keys with `/purchase name:<item> amount:<x>`",
        color=discord.Color.gold()
    )
    for item in list(CONSUMABLES.values()) + list(KEYS.values()):
        embed.add_field(
            name=f"🧾 {item['name']}",
            value=f"{item['effect']} — 💰 {item['price']} coins",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="purchase", description="Buy an item from the shop")
@app_commands.describe(name="Item name", amount="Quantity to buy")
async def purchase(interaction: discord.Interaction, name: str, amount: int):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return

    name_l = name.lower()
    item = CONSUMABLES.get(name_l) or KEYS.get(name_l)
    if not item:
        await interaction.response.send_message("❌ That item isn’t available for purchase.", ephemeral=True)
        return

    cost = item["price"] * amount
    p = players[user_id]
    if p["coins"] < cost:
        await interaction.response.send_message(
            f"❌ You need 💰 {cost}, but only have 💰 {p['coins']}.",
            ephemeral=True
        )
        return

    p["coins"] -= cost
    p["bag"][item["name"]] = p["bag"].get(item["name"], 0) + amount
    await save_json(PLAYER_FILE, players)

    await interaction.response.send_message(
        f"✅ Purchased {amount}x **{item['name']}** for 💰{cost} coins.\n💰 New Balance: {p['coins']}",
        ephemeral=True
    )

@bot.tree.command(name="sell", description="Sell items from your bag for coins")
@app_commands.describe(item="Item name", amount="How many to sell")
async def sell(interaction: discord.Interaction, item: str, amount: int):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    p = players[user_id]
    bag = p.get("bag", {})
    query = item.lower()

    # ✅ Collect all sellable items from the ITEMS.json structure
    all_items = []

    # Materials
    for elem_data in MATERIALS.values():
        for rarity_list in elem_data.values():
            all_items.extend(rarity_list)

    # Find matching item
    matches = [d for d in all_items if query in d["name"].lower()]
    if not matches:
        await interaction.response.send_message(f"❌ No sellable item found matching '{item}'.", ephemeral=True)
        return

    if len(matches) > 1:
        names = ", ".join([m["name"] for m in matches])
        await interaction.response.send_message(f"⚠️ Multiple matches found: {names}", ephemeral=True)
        return

    target = matches[0]
    name = target["name"]
    price = target.get("price")

    if price is None:
        await interaction.response.send_message(f"❌ {name} cannot be sold!", ephemeral=True)
        return

    actual_key = next((k for k in bag.keys() if k.lower() == name.lower()), None)
    if not actual_key or bag[actual_key] < amount:
        await interaction.response.send_message(f"❌ You don’t have {amount}x {name}.", ephemeral=True)
        return

    # Process sale
    earned = price * amount
    bag[actual_key] -= amount
    if bag[actual_key] <= 0:
        del bag[actual_key]
    p["coins"] += earned
    await save_json(PLAYER_FILE, players)

    await interaction.response.send_message(
        f"✅ Sold {amount}x **{name}** for 💰 {earned} coins!\n💰 New Balance: {p['coins']}",
        ephemeral=True
    )

@bot.tree.command(name="sellall", description="Sell all sellable items in your bag")
async def sellall(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    p = players[user_id]
    bag = p.get("bag", {})
    if not bag:
        await interaction.response.send_message("🎒 Your bag is empty!", ephemeral=True)
        return

    # ✅ Build full sellable item list
    all_items = []
    for elem_data in MATERIALS.values():
        for rarity_list in elem_data.values():
            all_items.extend(rarity_list)

    total_earned = 0
    sold = []
    skipped = []

    for key, qty in list(bag.items()):
        match = next((d for d in all_items if d["name"].lower() == key.lower()), None)
        if not match:
            skipped.append(key)
            continue

        price = match.get("price")
        if price is None:
            skipped.append(match["name"])
            continue

        earned = qty * price
        total_earned += earned
        sold.append(f"{qty}x {match['name']} ({earned}c)")
        del bag[key]

    if total_earned > 0:
        p["coins"] += total_earned
        await save_json(PLAYER_FILE, players)

    lines = []
    if sold:
        lines.append("✅ Sold:\n" + "\n".join(sold))
        lines.append(f"\n💰 Total Earned: {total_earned} coins\n💰 New Balance: {p['coins']}")
    else:
        lines.append("❌ No sellable items found in your bag.")

    if skipped:
        lines.append("\n⏩ Skipped unsellable items: " + ", ".join(skipped))

    embed = discord.Embed(title="👜 Sell All", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------------
# ECONOMY
# -----------------------------

last_work_time = {}
coinflips = {}  # {id: {"creator": user_id, "amount": int, "time": float}}
EXPIRY_SECONDS = 1800

@bot.tree.command(name="work", description="Work to earn coins!")
async def work(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return
    now = time.time()
    cooldown = 1800  # 30 minutes

    # Check cooldown
    if user_id in last_work_time and now - last_work_time[user_id] < cooldown:
        remaining = int(cooldown - (now - last_work_time[user_id]))
        mins, secs = divmod(remaining, 60)
        await interaction.response.send_message(
            f"⏳ {interaction.user.mention}, you need to wait {mins}m {secs}s before working again.",
            ephemeral=True
        )
        return  

    # Update last work time
    last_work_time[user_id] = now

    # Random reward
    reward = random.randint(2000, 8000)
    players[user_id]["coins"] += reward
    await save_json(PLAYER_FILE, players)

    next_available = int(now + cooldown)
    await interaction.response.send_message(
        f"🛠️ {interaction.user.mention}, you worked hard and earned **{reward}** coins!\n"
        f"💰 New Balance: {players[user_id]['coins']}\n"
        f"⏳ You can work again <t:{next_available}:R>",
        ephemeral=True)

@bot.tree.command(name="ahsell", description="List an item in the Auction House")
@app_commands.describe(
    id="Item name in your bag",
    amount="Amount to list",
    price="Price for 1 item",
    time="Duration in hours (12, 24, 48, 72)")
async def ahsell(interaction: discord.Interaction, id: str, amount: int, price: int, time: int):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return
    
    if not valid_duration(time):
        await interaction.response.send_message("❌ Auction duration must be 12, 24, 48, or 72 hours.", ephemeral=True)
        return
    
    p = players[user_id]
    bag = p.get("bag", {})

    # Case-insensitive bag key lookup
    matched_key = next((k for k in bag if k.lower() == id.lower()), None)
    if not matched_key:
        await interaction.response.send_message(f"❌ You don’t have any **{id}** in your bag.", ephemeral=True)
        return

    if bag[matched_key] < amount:
        await interaction.response.send_message(f"❌ You don’t have {amount}x {matched_key} in your bag.", ephemeral=True)
        return

    # Deduct from bag
    bag[matched_key] -= amount
    if bag[matched_key] <= 0:
        del bag[matched_key]

    # Create auction
    auc_id = new_auction_id()
    end_time = datetime.now(timezone.utc) + timedelta(hours=time)
    auctions[auc_id] = {
        "seller": user_id,
        "item": matched_key,  # Keep original case
        "amount": amount,
        "price": price,
        "end_time": end_time.isoformat()
    }
    await save_json(AUCTION_FILE, auctions)
    await save_json(PLAYER_FILE, players)

    await interaction.response.send_message(
        f"✅ Listed {amount}x **{matched_key}** for 💰{price} each\n"
        f"Auction ends <t:{int(end_time.timestamp())}:R>\n"
        f"Auction ID: `{auc_id}`",
        ephemeral=True)

@bot.tree.command(name="ahbuy", description="Buy from the Auction House")
@app_commands.describe(id="Auction ID", amount="Amount to buy (optional if only 1 item)")
async def ahbuy(interaction: discord.Interaction, id: str, amount: int = 1):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return
    
    auction = auctions.get(id)
    if not auction:
        await interaction.response.send_message("❌ Auction not found.", ephemeral=True)
        return
    
    if amount > auction["amount"]:
        await interaction.response.send_message(f"❌ Only {auction['amount']} items available in this auction.", ephemeral=True)
        return
    
    total_cost = auction["price"] * amount
    buyer = players[user_id]
    
    if buyer["coins"] < total_cost:
        await interaction.response.send_message(f"❌ You don’t have enough coins. Need 💰{total_cost}.", ephemeral=True)
        return
    
    # Deduct coins
    buyer["coins"] -= total_cost
    
    # Pay seller
    seller_id = auction["seller"]
    if seller_id in players:
        players[seller_id]["coins"] = players[seller_id].get("coins", 0) + total_cost
    
    # Give item(s)
    buyer["bag"][auction["item"]] = buyer["bag"].get(auction["item"], 0) + amount
    
    # Reduce auction amount
    auction["amount"] -= amount
    if auction["amount"] <= 0:
        del auctions[id]
    
    await save_json(AUCTION_FILE, auctions)
    await save_json(PLAYER_FILE, players)
    
    await interaction.response.send_message(
        f"✅ Bought {amount}x **{auction['item']}** for 💰{total_cost}",
        ephemeral=True)

@bot.tree.command(name="ahcancel", description="Cancel one of your active auctions")
@app_commands.describe(id="The auction ID to cancel")
async def ahcancel(interaction: discord.Interaction, id: str):
    user_id = str(interaction.user.id)

    # Check if auction exists
    auc = auctions.get(id)
    if not auc:
        await interaction.response.send_message(f"❌ No auction found with ID `{id}`.", ephemeral=True)
        return

    # Ensure user is the seller
    if auc["seller"] != user_id:
        await interaction.response.send_message("🚫 You can only cancel your own auctions.", ephemeral=True)
        return

    p = players.get(user_id)
    if not p:
        await interaction.response.send_message("⚠️ Create a profile first with `/profile`.", ephemeral=True)
        return

    bag = p.setdefault("bag", {})

    # Return items to seller’s bag
    item_name = auc["item"]
    amount = auc["amount"]
    bag[item_name] = bag.get(item_name, 0) + amount

    # Remove auction
    del auctions[id]

    await save_json(AUCTION_FILE, auctions)
    await save_json(PLAYER_FILE, players)

    await interaction.response.send_message(
        f"✅ Auction `{id}` canceled.\nReturned **{amount}x {item_name}** to your bag.",
        ephemeral=True
    )

@bot.tree.command(name="slots", description="Gamble your coins in a slot machine!")
@app_commands.describe(amount="Amount of coins to bet")
async def slots(interaction: discord.Interaction, amount: int):
    user_id = str(interaction.user.id)
    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    if players[user_id]["coins"] < amount:
        await interaction.response.send_message("❌ You don’t have enough coins to bet that amount.", ephemeral=True)
        return

    players[user_id]["coins"] -= amount
    await save_json(PLAYER_FILE, players)

    symbols = (
        ["🍇"] * 8 +
        ["🍒"] * 8 +
        ["🍋"] * 6 +
        ["⭐"] * 4 +
        ["🔔"] * 6 +
        ["💎"] * 2 + 
        ["🎁"] * 1)
    free_spins = 0
    total_winnings = 0
    spin_number = 1
    bonus_mode = False

    await interaction.response.send_message("Spinning...")
    msg = await interaction.original_response()

    while True:
        delays = [0.05, 0.075, 0.1, 0.115, 0.135, 0.15, 0.2, 0.25, 0.35, 0.5, 0.75, 1.7]  
        for delay in delays:
            rows = [[random.choice(symbols) for _ in range(3)] for _ in range(3)]
            display = ""
            for i, row in enumerate(rows):
                line = " | ".join(row)
                if i == 1:
                    display += f"   {line} ⬅️\n"
                else:
                    display += f"   {line}\n"
            embed = discord.Embed(
                title="🎰 Spinning...",
                description=f"```\n{display}```",
                color=discord.Color.purple()
            )
            await msg.edit(embed=embed, content=None)
            await asyncio.sleep(delay)

        middle = rows[1]
        display = ""
        for i, row in enumerate(rows):
            line = " | ".join(row)
            if i == 1:
                display += f"   {line} ⬅️\n"
            else:
                display += f"   {line}\n"

        payout = 0
        if len(set(middle)) == 1:
            symbol = middle[0]
            if symbol in ["🍇", "🍒", "🍋"]:
                payout = amount * 4
            elif symbol == "⭐":
                payout = amount * 15
            elif symbol == "🔔":
                payout = amount * 10
            elif symbol == "💎":
                payout = amount * 100

        elif len(set(middle)) == 2:
            for sym in middle:
                if middle.count(sym) == 2:
                    symbol = sym
                    break
            if symbol in ["🍇", "🍒", "🍋"]:
                payout = int(amount * 0.5)
            elif symbol == "⭐":
                payout = int(amount * 3.5)
            elif symbol == "🔔":
                payout = int(amount * 2.5)
            elif symbol == "💎":
                payout = amount * 15

        players[user_id]["coins"] += payout
        total_winnings += payout
        await save_json(PLAYER_FILE, players)

        if bonus_mode:
            total_winnings += payout

        result_msg = ""
        if payout >= amount * 10:
            result_msg = f"💰 BIG WIN! You won {payout} coins!"
        elif payout > 0:
            if payout == int(amount * 0.5):
                result_msg = f"➗ You got half back: {payout} coins."
            else:
                result_msg = f"✅ You won {payout} coins!"
        else:
            result_msg = "❌ You lost!"

        if "🎁" in middle:
            free_spins += 3
            bonus_mode = True
            result_msg += f"\n🎁 BONUS! You got 3 free spins! (Remaining: {free_spins})"

        title = "Slot Machine Result"
        if free_spins > 0:
            title = f"Bonus Spin Result ({spin_number})"

        embed = discord.Embed(
            title=title,
            description=f"```\n{display}```",
            color=discord.Color.gold())
        embed.add_field(name="Result", value=result_msg, inline=False)
        embed.add_field(name="Balance", value=f"💰 {players[user_id]['coins']} coins", inline=False)

        await msg.edit(embed=embed, content=None)

        # Continue free spins if active
        if free_spins > 0:
            free_spins -= 1
            spin_number += 1
            await asyncio.sleep(2)
            continue
        else:
            break

    # 🔹 Final bonus summary
    if bonus_mode and total_winnings > 0:
        embed = discord.Embed(
            title="Bonus Spins Finished 🎁 ",
            description=f"You earned **{total_winnings}** coins total from your bonus spins!",
            color=discord.Color.green())
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="cfcreate", description="Create a coinflip bet")
@app_commands.describe(amount="How many coins to bet")
async def cfcreate(interaction: discord.Interaction, amount: int):
    user_id = str(interaction.user.id)

    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return
    
    p = players[user_id]

    if amount <= 0:
        await interaction.response.send_message("❌ Bet must be greater than 0.", ephemeral=True)
        return

    if p["coins"] < amount:
        await interaction.response.send_message("❌ You don’t have enough coins!", ephemeral=True)
        return

    # Deduct coins upfront
    p["coins"] -= amount
    await save_json(PLAYER_FILE, players)

    # Generate unique ID
    cf_id = str(int(time.time() * 1000))[-6:]
    coinflips[cf_id] = {
        "creator": user_id,
        "amount": amount,
        "time": time.time()
    }

    await interaction.response.send_message(
        f"🎲 Coinflip created!\nBet: 💰 {amount} coins\n"
        f"Use `/cftake id:{cf_id}` to challenge\n"
        f"Expires in 30 minutes")

@bot.tree.command(name="cf", description="View active coinflips")
async def cf(interaction: discord.Interaction):
    if not coinflips:
        await interaction.response.send_message("⚠️ No active coinflips right now.", ephemeral=True)
        return

    lines = []
    now = time.time()
    for cf_id, data in coinflips.items():
        creator = players.get(data["creator"], {}).get("username", "Unknown")
        uptime = timedelta(seconds=int(now - data["time"]))
        remaining = EXPIRY_SECONDS - int(now - data["time"])
        mins, secs = divmod(max(0, remaining), 60)
        lines.append(f"🆔 `{cf_id}` | 💰 {data['amount']} coins | 👤 {creator} | ⏱️ {uptime} | ⌛ {mins:02}:{secs:02} left")

    embed = discord.Embed(
        title="🎲 Active Coinflips",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="cftake", description="Take a coinflip")
@app_commands.describe(id="The coinflip ID to challenge")
async def cftake(interaction: discord.Interaction, id: str):
    user_id = str(interaction.user.id)

    if user_id not in players:
        await interaction.response.send_message("⚠️ Create a profile with `/profile` first.", ephemeral=True)
        return

    if id not in coinflips:
        await interaction.response.send_message("❌ No active coinflip found with that ID (maybe expired).", ephemeral=True)
        return

    flip = coinflips[id]
    creator_id = flip["creator"]
    amount = flip["amount"]

    if user_id == creator_id:
        await interaction.response.send_message("❌ You can’t take your own coinflip.", ephemeral=True)
        return

    challenger = players[user_id]
    if challenger["coins"] < amount:
        await interaction.response.send_message("❌ You don’t have enough coins to match this bet.", ephemeral=True)
        return

    # Deduct challenger coins
    challenger["coins"] -= amount

    # Flip coin
    winner = random.choice([creator_id, user_id])
    loser = creator_id if winner == user_id else user_id

    prize = amount * 2
    players[winner]["coins"] += prize

    # Remove flip
    del coinflips[id]
    await save_json(PLAYER_FILE, players)

    winner_name = players[winner]["username"]
    loser_name = players[loser]["username"]

    await interaction.response.send_message(
        f"🎲 Coinflip `{id}` — {amount} vs {amount}\n"
        f"🪙 Flipping coin...\n\n"
        f"🏆 {winner_name} wins **{prize}** coins\n"
        f"💀 {loser_name} lost their bet")

# -----------------------------
# HELP COMMAND
# -----------------------------

@bot.tree.command(name="help", description="View all available Pixel Heroes commands")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📘 Pixel Heroes — Command Guide",
        description="Welcome, Adventurer! Here’s a list of available commands by category.\nUse `/profile` first to begin your journey!",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="🧙 Profile",
        value=(
            "`/profile name:<user>` — View an adventurer's profile\n"
            "`/bag` — See your inventory and materials\n"
            "`/party` — View your current hero team\n"
            "`/xp` or `/level` — Check XP and level progress\n"
            "`/leaderboard` — View top coin holders"
        ),
        inline=False
    )

    embed.add_field(
        name="⚔️ Adventure",
        value=(
            "`/explore` — Explore to encounter monsters or heroes\n"
            "`/bosses` — View all powerful Boss Domains\n"
            "`/boss name:<type>` — Challenge a Domain Boss"
        ),
        inline=False
    )

    embed.add_field(
        name="🧭 Hero Management",
        value=(
            "`/move from:<slot> to:<slot>` — Swap team order\n"
            "`/barracks` — View all stored heroes\n"
            "`/barracksclear` — Clear non-active heroes\n"
            "`/hero id:<hID>` — View detailed hero stats\n"
            "`/partyadd id:<hID>` — Add hero to your team\n"
            "`/partyremove id:<hID>` — Remove from team\n"
            "`/lock` or `/unlock` — Protect or release heroes\n"
            "`/elixir name:<hero>` — Instantly level up a hero\n"
            "`/heal` — Heal your active party"
        ),
        inline=False
    )

    embed.add_field(
        name="💰 Economy",
        value=(
            "`/shop` — View shop items\n"
            "`/purchase name:<item> amount:<x>` — Buy from shop\n"
            "`/sell item:<name> amount:<x>` — Sell a material\n"
            "`/sellall` — Sell all sellable items\n"
            "`/slots amount:<x>` — Gamble your coins\n"
            "`/cfcreate amount:<x>` — Create a coinflip bet\n"
            "`/cf` — View active coinflips\n"
            "`/cftake id:<id>` — Join a coinflip\n"
            "`/work` — Earn coins every 30 minutes"
        ),
        inline=False
    )

    embed.add_field(
        name="🏪 Player Market",
        value=(
            "`/auctionhouse [search:<term>]` — Browse active listings\n"
            "`/ahsell id:<item> amount:<x> price:<p> time:<hrs>` — List an item\n"
            "`/ahbuy id:<auction_id>` — Purchase from an auction\n"
            "`/ahcancel id:<auction_id>` — Cancel an auction"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Utility",
        value=(
            "`/help` — View this command guide"
        ),
        inline=False
    )

    embed.set_footer(text="✨ Tip: Most commands have optional autocomplete or parameter hints")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------------
# TASKS
# -----------------------------

@tasks.loop(minutes=1)
async def auction_cleanup():
    now = datetime.now(timezone.utc)
    expired = []
    for auc_id, auc in list(auctions.items()):
        end_time = datetime.fromisoformat(auc["end_time"])
        if now >= end_time:
            seller_id = auc["seller"]
            if auc["amount"] > 0 and seller_id in players:
                players[seller_id]["bag"][auc["item"]] = players[seller_id]["bag"].get(auc["item"], 0) + auc["amount"]
            expired.append(auc_id)
    
    for auc_id in expired:
        del auctions[auc_id]
    
    if expired:
        await save_json(AUCTION_FILE, auctions)
        await save_json(PLAYER_FILE, players)
        print(f"🛒 Auction cleanup: expired {len(expired)} auctions.")

@tasks.loop(minutes=1)
async def coinflip_cleanup():
    """Automatically clean up expired coinflips and refund creators."""
    now = time.time()
    expired = []

    for cf_id, data in list(coinflips.items()):
        # Check expiry
        if now - data["time"] > EXPIRY_SECONDS:
            creator_id = data["creator"]
            amount = data["amount"]

            # Refund coins if creator still exists
            if creator_id in players:
                players[creator_id]["coins"] = players[creator_id].get("coins", 0) + amount
                print(f"💸 Refunded {amount} coins to {players[creator_id]['username']} for expired coinflip {cf_id}")

            expired.append(cf_id)

    # Remove expired coinflips
    for cf_id in expired:
        del coinflips[cf_id]

    # Persist refunds
    if expired:
        await save_json(PLAYER_FILE, players)
        print(f"🎲 Coinflip cleanup: expired {len(expired)} flips.")

# -----------------------------
# BOT START
# -----------------------------

def main():
    if not TOKEN:
        print("❌ Set DISCORD_TOKEN environment variable.")
        return
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
