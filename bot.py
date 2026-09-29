"""Anti-raid bot: detects join floods and message spam, and locks down the server."""
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("antiraid")

DATA_FILE = Path("data/config.json")
DEFAULTS = {
    "log_channel_id": None,
    "join_threshold": 5,        # joins ...
    "join_window": 10,          # ... within this many seconds trigger lockdown
    "min_account_age_days": 3,  # accounts younger than this are flagged during lockdown
    "lockdown_action": "kick",  # kick | timeout | none
    "lockdown_minutes": 10,     # auto-unlock after this long (0 = manual only)
    "spam_messages": 6,         # messages ...
    "spam_window": 5,           # ... within this many seconds => timeout
    "max_mentions": 6,          # mentions in one message => timeout
    "timeout_minutes": 10,
    "whitelist_role_ids": [],   # roles exempt from spam checks
}


class Store:
    def __init__(self):
        self.data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}

    def get(self, guild_id: int) -> dict:
        return {**DEFAULTS, **self.data.get(str(guild_id), {})}

    def set(self, guild_id: int, key: str, value):
        self.data.setdefault(str(guild_id), {})[key] = value
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(self.data, indent=2))


class AntiRaidBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # privileged: enable in the developer portal
        intents.message_content = False  # not needed; we only count messages
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store = Store()
        self.joins = defaultdict(deque)      # guild_id -> join timestamps
        self.msgs = defaultdict(deque)       # (guild_id, user_id) -> timestamps
        self.lockdown_until = {}             # guild_id -> epoch (inf = manual)

    async def setup_hook(self):
        await self.tree.sync()

    async def alert(self, guild: discord.Guild, text: str):
        log.info("[%s] %s", guild.name, text)
        cid = self.store.get(guild.id)["log_channel_id"]
        channel = guild.get_channel(cid) if cid else None
        if channel:
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    def in_lockdown(self, guild_id: int) -> bool:
        until = self.lockdown_until.get(guild_id)
        if until is None:
            return False
        if time.time() >= until:
            del self.lockdown_until[guild_id]
            return False
        return True

    async def start_lockdown(self, guild: discord.Guild, reason: str):
        cfg = self.store.get(guild.id)
        mins = cfg["lockdown_minutes"]
        self.lockdown_until[guild.id] = time.time() + mins * 60 if mins else float("inf")
        await self.alert(guild, f"🚨 **Lockdown enabled**: {reason}. New joiners will be handled "
                                f"(action: `{cfg['lockdown_action']}`).")

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = self.store.get(guild.id)
        now = time.time()

        q = self.joins[guild.id]
        q.append(now)
        while q and now - q[0] > cfg["join_window"]:
            q.popleft()
        if len(q) >= cfg["join_threshold"] and not self.in_lockdown(guild.id):
            await self.start_lockdown(guild, f"{len(q)} joins in {cfg['join_window']}s")

        if not self.in_lockdown(guild.id) or member.bot:
            return
        age = discord.utils.utcnow() - member.created_at
        young = age < timedelta(days=cfg["min_account_age_days"])
        # During lockdown every joiner is handled; young accounts are noted in the log.
        action = cfg["lockdown_action"]
        try:
            if action == "kick":
                await member.kick(reason="Anti-raid lockdown")
            elif action == "timeout":
                await member.timeout(timedelta(minutes=cfg["lockdown_minutes"] or 60),
                                     reason="Anti-raid lockdown")
        except discord.HTTPException as e:
            await self.alert(guild, f"⚠️ Could not {action} {member} ({e}).")
            return
        if action != "none":
            await self.alert(guild, f"{action.title()}ed {member} (`{member.id}`), "
                                    f"account age {age.days}d{' ⚠️ new' if young else ''}.")

    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        cfg = self.store.get(message.guild.id)
        if member.guild_permissions.manage_messages or any(
                r.id in cfg["whitelist_role_ids"] for r in member.roles):
            return

        now = time.time()
        q = self.msgs[(message.guild.id, member.id)]
        q.append(now)
        while q and now - q[0] > cfg["spam_window"]:
            q.popleft()

        reason = None
        if len(q) >= cfg["spam_messages"]:
            reason = f"{len(q)} messages in {cfg['spam_window']}s"
        elif len(message.mentions) + len(message.role_mentions) >= cfg["max_mentions"]:
            reason = "mass mentions"
        if reason:
            q.clear()
            try:
                await member.timeout(timedelta(minutes=cfg["timeout_minutes"]),
                                     reason=f"Anti-raid: {reason}")
                await message.channel.purge(limit=50, check=lambda m: m.author == member,
                                            after=discord.utils.utcnow() - timedelta(minutes=1))
            except discord.HTTPException as e:
                await self.alert(message.guild, f"⚠️ Failed to act on {member}: {e}")
                return
            await self.alert(message.guild, f"🔇 Timed out {member} (`{member.id}`): {reason}.")


bot = AntiRaidBot()
admin = app_commands.default_permissions(administrator=True)


@bot.tree.command(description="Show anti-raid status and settings")
@admin
@app_commands.guild_only()
async def status(interaction: discord.Interaction):
    cfg = bot.store.get(interaction.guild_id)
    state = "🔒 ON" if bot.in_lockdown(interaction.guild_id) else "🔓 off"
    lines = "\n".join(f"`{k}`: {v}" for k, v in cfg.items())
    await interaction.response.send_message(f"Lockdown: {state}\n{lines}", ephemeral=True)


@bot.tree.command(description="Manually enable lockdown")
@admin
@app_commands.guild_only()
async def lockdown(interaction: discord.Interaction):
    await bot.start_lockdown(interaction.guild, f"manual, by {interaction.user}")
    await interaction.response.send_message("Lockdown enabled.", ephemeral=True)


@bot.tree.command(description="Disable lockdown")
@admin
@app_commands.guild_only()
async def unlock(interaction: discord.Interaction):
    bot.lockdown_until.pop(interaction.guild_id, None)
    await bot.alert(interaction.guild, f"🔓 Lockdown disabled by {interaction.user}.")
    await interaction.response.send_message("Lockdown disabled.", ephemeral=True)


@bot.tree.command(description="Set a setting (e.g. join_threshold 8)")
@admin
@app_commands.guild_only()
@app_commands.choices(key=[app_commands.Choice(name=k, value=k) for k, v in DEFAULTS.items()
                           if isinstance(v, int) and k != "log_channel_id"]
                      + [app_commands.Choice(name="lockdown_action", value="lockdown_action")])
async def config(interaction: discord.Interaction, key: app_commands.Choice[str], value: str):
    k = key.value
    if k == "lockdown_action":
        if value not in ("kick", "timeout", "none"):
            return await interaction.response.send_message("Use kick, timeout or none.", ephemeral=True)
        parsed = value
    else:
        if not value.isdigit():
            return await interaction.response.send_message("Value must be a whole number.", ephemeral=True)
        parsed = int(value)
    bot.store.set(interaction.guild_id, k, parsed)
    await interaction.response.send_message(f"`{k}` = `{parsed}`", ephemeral=True)


@bot.tree.command(description="Set the channel for anti-raid alerts")
@admin
@app_commands.guild_only()
async def logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.store.set(interaction.guild_id, "log_channel_id", channel.id)
    await interaction.response.send_message(f"Alerts will go to {channel.mention}.", ephemeral=True)


@bot.tree.command(description="Exempt a role from spam checks")
@admin
@app_commands.guild_only()
async def whitelist(interaction: discord.Interaction, role: discord.Role):
    ids = set(bot.store.get(interaction.guild_id)["whitelist_role_ids"]) | {role.id}
    bot.store.set(interaction.guild_id, "whitelist_role_ids", sorted(ids))
    await interaction.response.send_message(f"{role.mention} is exempt from spam checks.", ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    bot.run(token)
