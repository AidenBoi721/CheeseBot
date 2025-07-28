import os
import shutil
import sqlite3
import asyncio
import discord
from discord.ext import tasks
from discord import app_commands
from datetime import datetime, UTC, date
from dotenv import load_dotenv
from typing import Optional

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
DEVELOPER_IDS = set(int(i) for i in os.getenv("DEVELOPER_IDS", "").split(",") if i.strip())

def log(message: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    COLORS = {
        "INFO": "\033[94m",      # Blue
        "WARN": "\033[38;5;208m",  # Orange
        "ERROR": "\033[91m",     # Red
        "COMMAND": "\033[92m",   # Light green
        "DEBUG": "\033[95m",     # Magenta
        "ENDC": "\033[0m"
    }
    color = COLORS.get(level.upper(), "")
    endc = COLORS["ENDC"]
    formatted = f"[{timestamp}] [{level.upper():<7}] {message}"
    print(f"{color}{formatted}{endc}")

def log_command_usage(interaction: discord.Interaction):
    user = interaction.user
    command = interaction.command.name if interaction.command else "Unknown"
    log(f"Command '{command}' invoked by {user.name} ({user.id})", level="COMMAND")

def get_birthday_channel(guild: discord.Guild) -> discord.TextChannel | None:
    cursor.execute("SELECT channel_id FROM birthday_channels WHERE guild_id = ?", (guild.id,))
    result = cursor.fetchone()
    if result:
        return guild.get_channel(result[0])
    return discord.utils.get(guild.text_channels, name="general") 

# Setup Discord bot
intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)



# Setup birthday database
conn = sqlite3.connect("birthdays.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS birthdays (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    birth_month INTEGER NOT NULL,
    birth_day INTEGER NOT NULL,
    notified_today INTEGER DEFAULT 0
)
""")
conn.commit()

# Setup channel database
cursor.execute("""
CREATE TABLE IF NOT EXISTS birthday_channels (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL
)
""")
conn.commit()

@bot.event
async def on_ready():
    await bot.change_presence(
    status=discord.Status.online,
    activity=discord.Game(name="with unicorns")
    )
    
    log(f"Logged in as {bot.user}", level="INFO")

    # Sync once only — add commands *before* syncing
    tree.add_command(debug_group, guild=discord.Object(id=GUILD_ID))
    tree.add_command(birthday_group, guild=discord.Object(id=GUILD_ID))
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    birthday_check.start()


class DebugCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="debug", description="Developer-only debug tools")

debug_group = DebugCommands()

class BirthdayCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="birthday", description="Birthday-related commands")

birthday_group = BirthdayCommands()

@debug_group.command(name="wipe_database", description="(Dev only) Wipe all birthday records")
async def wipe_database(interaction: discord.Interaction):
    if interaction.user.id not in DEVELOPER_IDS:
        await interaction.response.send_message("❌ You are not authorized to use this command.", ephemeral=True)
        log(f"Unauthorized DB wipe attempt by {interaction.user.id}", level="WARN")
        return

    try:
        cursor.execute("DELETE FROM birthdays")
        conn.commit()
        await interaction.response.send_message("🧨 All birthday records have been wiped!", ephemeral=True)
        log(f"Database wiped by {interaction.user.name} ({interaction.user.id})", level="ERROR")
    except Exception as e:
        log(f"Database wipe failed: {e}", level="ERROR")
        await interaction.response.send_message("❌ Failed to wipe database.", ephemeral=True)

    log_command_usage(interaction)

@birthday_group.command(name="clear", description="Clear a stored birthday")
@app_commands.describe(user="The user whose birthday should be removed (admin only)")
async def clear_birthday(interaction: discord.Interaction, user: Optional[discord.User] = None):
    is_admin = interaction.user.guild_permissions.administrator
    target_user = user or interaction.user

    if user and not is_admin:
        # Trying to clear someone else's birthday without admin
        msg = "❌ You must be a server administrator to clear another user's birthday."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        log(f"Unauthorized birthday clear attempt on {user.name} by {interaction.user.name}", "WARN")
        return

    try:
        cursor.execute("SELECT 1 FROM birthdays WHERE user_id = ?", (target_user.id,))
        if not cursor.fetchone():
            msg = f"⚠️ No birthday found for {target_user.mention}."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            log(f"No birthday to clear for {target_user.name} ({target_user.id})", "INFO")
            return

        cursor.execute("DELETE FROM birthdays WHERE user_id = ?", (target_user.id,))
        conn.commit()

        msg = f"🗑️ Cleared birthday entry for {target_user.mention}."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

        log(f"Cleared birthday for {target_user.name} ({target_user.id}) by {interaction.user.name}", "INFO")

    except Exception as e:
        log(f"Error clearing birthday for {target_user.name}: {e}", "ERROR")
        try:
            msg = "❌ An error occurred while clearing the birthday."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception as inner:
            log(f"Followup failed during clear: {inner}", "ERROR")

    log_command_usage(interaction)


@birthday_group.command(name="channel", description="Set the channel for birthday messages")
@app_commands.describe(channel="The channel to send birthday messages in")
async def set_birthday_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Only administrators can set the birthday channel.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Only administrators can set the birthday channel.", ephemeral=True)
        log(f"Unauthorized channel set attempt by {interaction.user}", "WARN")
        return

    try:
        settings_conn = sqlite3.connect("settings.db")
        settings_cursor = settings_conn.cursor()
        settings_cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        settings_cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ("birthday_channel", str(channel.id)))
        settings_conn.commit()
        settings_conn.close()

        await interaction.response.send_message(f"🎉 Birthday messages will now be sent in {channel.mention}.", ephemeral=True)
        log(f"Set birthday channel to {channel.name} ({channel.id})", "INFO")

    except Exception as e:
        log(f"Failed to set birthday channel: {e}", "ERROR")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Failed to set birthday channel.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to set birthday channel.", ephemeral=True)

    log_command_usage(interaction)


@birthday_group.command(name="set", description="Set the birthday for a user")
@app_commands.describe(
    month="Month (1–12)",
    day="Day (1–31)",
    user="User whose birthday you're setting"
)
async def set_birthday(interaction: discord.Interaction, month: int, day: int, user: discord.User):
    invoking_user = interaction.user
    target_user = user or invoking_user

    # Restrict non-admins from setting birthdays for other users
    if target_user.id != invoking_user.id and not invoking_user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You can only set your own birthday unless you're a server admin.",
            ephemeral=True
        )
        log(f"Unauthorized birthday set attempt for {target_user.name} by {invoking_user.name}", level="WARN")
        return

    try:
        # Validate date
        try:
            date(2000, month, day)
        except ValueError:
            msg = "❌ Invalid month or day. Please ensure it's a real date."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                try:
                    await interaction.followup.send(msg, ephemeral=True)
                except Exception as e:
                    log(f"Follow-up failed after invalid date: {e}", level="ERROR")
            log(f"Invalid date given: {month:02}-{day:02} by {invoking_user}", level="WARN")
            return

        user_id = target_user.id
        username = target_user.name

        # Replace or insert birthday record
        cursor.execute(
            "REPLACE INTO birthdays (user_id, username, birth_month, birth_day, notified_today) VALUES (?, ?, ?, ?, 0)",
            (user_id, username, month, day)
        )
        conn.commit()

        await interaction.response.send_message(
            f"✅ Birthday set for {target_user.mention} on {month:02}-{day:02}.", ephemeral=True
        )
        log(f"Set birthday for {username} ({user_id}): {month:02}-{day:02}", level="INFO")

    except Exception as e:
        log(f"Unexpected error in set_birthday: {e}", level="ERROR")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An unexpected error occurred.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)
        except Exception as inner:
            log(f"Failed to send error message: {inner}", level="ERROR")

    log_command_usage(interaction)


@birthday_group.command(name="prune", description="Remove birthdays for users not in the server")
async def prune_birthdays(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await interaction.followup.send("❌ Guild not found.", ephemeral=True)
        log("Prune failed: guild not found.", level="ERROR")
        return

    cursor.execute("SELECT user_id FROM birthdays")
    all_ids = [row[0] for row in cursor.fetchall()]
    removed = 0

    for user_id in all_ids:
        if guild.get_member(user_id) is None:
            cursor.execute("DELETE FROM birthdays WHERE user_id = ?", (user_id,))
            removed += 1

    conn.commit()
    await interaction.followup.send(f"🧹 Pruned {removed} stale birthday record(s).", ephemeral=True)
    log(f"Pruned {removed} birthday entries.", level="INFO")

    log_command_usage(interaction)

@debug_group.command(name="birthday_check", description="Simulate birthday check")
@app_commands.describe(month="Month to simulate", day="Day to simulate")
async def birthday_check_debug(interaction: discord.Interaction, month: int, day: int):
    if interaction.user.id not in DEVELOPER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        log(f"Unauthorized debug attempt by {interaction.user.id}", level="WARN")
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        log("Interaction already acknowledged. Skipping defer().", "DEBUG")
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await interaction.followup.send("❌ Guild not found.", ephemeral=True)
        return

    channel = discord.utils.get(guild.text_channels, name="general")
    if not channel:
        await interaction.followup.send("❌ Target channel not found.", ephemeral=True)
        return

    cursor.execute("SELECT user_id, username FROM birthdays WHERE birth_month = ? AND birth_day = ?", (month, day))
    rows = cursor.fetchall()
    if not rows:
        await interaction.followup.send(f"No birthdays found for {month:02}-{day:02}.", ephemeral=True)
        return

    count = 0
    for user_id, username in rows:
        member = guild.get_member(user_id)
        name = member.mention if member else username or f"User ID {user_id}"
        await channel.send(f"🎉 [DEBUG] Happy Birthday, {name}! 🎂")
        count += 1

    await interaction.followup.send(f"✅ Sent {count} simulated birthday messages.")
    log_command_usage(interaction)

@debug_group.command(name="reset_flags", description="Reset all notified_today flags")
async def reset_flags(interaction: discord.Interaction):
    if interaction.user.id not in DEVELOPER_IDS:
        try:
            await interaction.response.send_message("✅ All birthday flags reset.", ephemeral=True)
        except discord.errors.NotFound:
            await interaction.followup.send("✅ All birthday flags reset.", ephemeral=True)
    return

    try:
        cursor.execute("UPDATE birthdays SET notified_today = 0")
        conn.commit()
        await interaction.response.send_message("✅ All birthday flags reset.", ephemeral=True)
        log("All birthday flags reset manually.", "WARN")
    except Exception as e:
        log(f"Failed to reset flags: {e}", level="ERROR")
        try:
            await interaction.followup.send("❌ Failed to reset flags.", ephemeral=True)
        except Exception as final:
          log(f"Followup also failed: {final}", "ERROR")

    log_command_usage(interaction)

@tasks.loop(hours=24)
async def birthday_check():
    await bot.wait_until_ready()
    now = datetime.now(UTC)
    month, day = now.month, now.day

    cursor.execute("""
        SELECT user_id, username FROM birthdays
        WHERE birth_month = ? AND birth_day = ? AND notified_today = 0
    """, (month, day))
    rows = cursor.fetchall()

    if not rows:
        log("No birthdays today.", level="INFO")
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        log("Birthday check aborted: guild not found.", level="ERROR")
        return

    channel = get_birthday_channel(guild)
    if not channel:
        log("Birthday check aborted: target channel not found.", level="ERROR")
        return

    for user_id, username in rows:
        try:
            member = guild.get_member(user_id)
            name = member.mention if member else username or f"User ID {user_id}"
            await channel.send(f"🎉 Happy Birthday, {name}! Hope you have an amazing day! 🎂")
            log(f"Sent birthday message for {name}.", level="INFO")
            cursor.execute("UPDATE birthdays SET notified_today = 1 WHERE user_id = ?", (user_id,))
        except Exception as e:
            log(f"Failed to send birthday message to {user_id}: {e}", level="ERROR")

        conn.commit()

@tasks.loop(minutes=5)
async def heartbeat():
    log("💓 Bot heartbeat OK.", "DEBUG")

bot.run(TOKEN)
