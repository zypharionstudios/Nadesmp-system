import asyncio
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode

import aiosqlite
import discord
from aiohttp import ClientSession, web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("nadesmp-bot")

TOKEN = os.getenv("DISCORD_TOKEN", "")
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("REDIRECT_URI", "")
VERIFY_GUILD_ID = int(os.getenv("VERIFY_GUILD_ID", "1539199898430476328"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/bot.sqlite3")
API = "https://discord.com/api/v10"

TICKET_TYPES = {
    "team": ("Team Application", "team-application"),
    "developer": ("Developer Application", "developer-application"),
    "beta": ("Beta Tester Application", "beta-tester"),
    "report": ("Report", "report"),
    "questions": ("Questions", "questions"),
}

class Database:
    def __init__(self, path):
        self.path = path

    async def init(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id TEXT PRIMARY KEY,
                team_role_id TEXT, ticket_category_id TEXT, ticket_log_id TEXT,
                verify_role_id TEXT, extra_role_id TEXT, remove_role_id TEXT,
                welcome_channel_id TEXT, welcome_message TEXT, welcome_required_role_id TEXT
            );
            CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
            """)
            await db.commit()

    async def settings(self, guild_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM settings WHERE guild_id=?", (str(guild_id),))
            row = await cur.fetchone()
            return dict(row) if row else {}

    async def update(self, guild_id, **values):
        allowed = {"team_role_id","ticket_category_id","ticket_log_id","verify_role_id","extra_role_id","remove_role_id","welcome_channel_id","welcome_message","welcome_required_role_id"}
        values = {k: (str(v) if v is not None else None) for k,v in values.items() if k in allowed}
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (str(guild_id),))
            for key, value in values.items():
                await db.execute(f"UPDATE settings SET {key}=? WHERE guild_id=?", (value, str(guild_id)))
            await db.commit()

    async def create_state(self, state):
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 600,))
            await db.execute("INSERT INTO oauth_states(state,created_at) VALUES(?,?)", (state, now))
            await db.commit()

    async def consume_state(self, state):
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM oauth_states WHERE state=? AND created_at>=?", (state, now - 600))
            row = await cur.fetchone()
            await db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
            await db.commit()
            return row is not None

class TicketCloseView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="nadesmp:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("This action is only available inside a ticket.", ephemeral=True)
        cfg = await self.bot.db.settings(interaction.guild.id)
        role_id = cfg.get("team_role_id")
        is_team = role_id and any(r.id == int(role_id) for r in getattr(interaction.user, "roles", []))
        if not (interaction.user.guild_permissions.administrator or is_team):
            return await interaction.response.send_message("You are not allowed to close this ticket.", ephemeral=True)
        await interaction.response.send_message("This ticket is being closed.")
        log_id = cfg.get("ticket_log_id")
        if log_id:
            log_channel = interaction.guild.get_channel(int(log_id))
            if log_channel:
                await log_channel.send(f"Ticket **{interaction.channel.name}** was closed by {interaction.user.mention}.")
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")

class TicketButton(discord.ui.Button):
    def __init__(self, bot, key, label):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"nadesmp:ticket:create:{key}")
        self.bot, self.key = bot, key

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this panel inside the server.", ephemeral=True)
        cfg = await self.bot.db.settings(guild.id)
        category_id, team_id = cfg.get("ticket_category_id"), cfg.get("team_role_id")
        category = guild.get_channel(int(category_id)) if category_id else None
        team_role = guild.get_role(int(team_id)) if team_id else None
        slug = TICKET_TYPES[self.key][1]
        username = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace(" ", "-"))[:18] or "member"
        name = f"{slug}-{username}"
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing:
            return await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        if team_role:
            overwrites[team_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await guild.create_text_channel(name, category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites)
        title = TICKET_TYPES[self.key][0]
        embed = discord.Embed(
            title=title,
            description=f"Welcome to your **{title}** ticket, {interaction.user.mention}.\n\nYour request has been received. The team has been notified and will assist you as soon as possible.\n\nPlease explain your request clearly and include any relevant details.",
            color=discord.Color.blurple()
        )
        notice = f"{team_role.mention} — a new ticket has been opened." if team_role else "A new ticket has been opened."
        await channel.send(content=notice, embed=embed, view=TicketCloseView(self.bot))
        await interaction.response.send_message(f"Your ticket has been created: {channel.mention}", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        for key, (label, _) in TICKET_TYPES.items():
            self.add_item(TicketButton(bot, key, label))

class VerifyPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Verify Account", style=discord.ButtonStyle.success, custom_id="nadesmp:verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or interaction.guild.id != VERIFY_GUILD_ID:
            return await interaction.response.send_message("Verification is only available in the configured server.", ephemeral=True)
        cfg = await self.bot.db.settings(interaction.guild.id)
        if not cfg.get("verify_role_id"):
            return await interaction.response.send_message("Verification has not been configured. Contact an administrator.", ephemeral=True)
        if not all((CLIENT_ID, REDIRECT_URI)):
            return await interaction.response.send_message("OAuth2 is not configured yet. Contact an administrator.", ephemeral=True)
        state = secrets.token_urlsafe(32)
        await self.bot.db.create_state(state)
        query = urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
            "prompt": "consent",
        })
        link = f"https://discord.com/oauth2/authorize?{query}"
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Continue with Discord", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message(
            "Authorize Discord so we can check whether your account is already a member of this server. No invitation or server join is performed.",
            view=view, ephemeral=True
        )

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(DATABASE_PATH)

    async def setup_hook(self):
        await self.db.init()
        self.add_view(TicketPanelView(self))
        self.add_view(TicketCloseView(self))
        self.add_view(VerifyPanelView(self))
        await self.tree.sync()
        log.info("Application commands synced.")

    async def on_ready(self):
        log.info("Ready as %s (%s)", self.user, self.user.id)

bot = Bot()

def admin_only():
    return app_commands.default_permissions(administrator=True)

@bot.tree.command(name="setup", description="Configure ticket and verification settings.")
@admin_only()
@app_commands.describe(team_role="Team role pinged in tickets and allowed to close them", ticket_category="Category for private tickets", ticket_log="Ticket log channel", verify_role="Role granted after a successful member check", extra_role="Optional additional role granted", remove_role="Optional role removed after verification")
async def setup_cmd(interaction: discord.Interaction, team_role: discord.Role = None, ticket_category: discord.CategoryChannel = None, ticket_log: discord.TextChannel = None, verify_role: discord.Role = None, extra_role: discord.Role = None, remove_role: discord.Role = None):
    if not interaction.guild:
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    values = {}
    for key, value in (("team_role_id", team_role), ("ticket_category_id", ticket_category), ("ticket_log_id", ticket_log), ("verify_role_id", verify_role), ("extra_role_id", extra_role), ("remove_role_id", remove_role)):
        if value is not None:
            values[key] = value.id
    if not values:
        return await interaction.response.send_message("Provide at least one setting to update.", ephemeral=True)
    await bot.db.update(interaction.guild.id, **values)
    await interaction.response.send_message("Settings updated successfully.", ephemeral=True)

@bot.tree.command(name="panel-send", description="Send the support ticket panel.")
@admin_only()
async def panel_send(interaction: discord.Interaction, channel: discord.TextChannel):
    embed = discord.Embed(title="Support Center", description="Welcome to the Support Center.\nChoose the category that best matches your request. A private ticket will be created for you.", color=discord.Color.blurple())
    embed.set_footer(text="Please do not open multiple tickets for the same request.")
    await channel.send(embed=embed, view=TicketPanelView(bot))
    await interaction.response.send_message(f"Support panel sent to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="verify-panel-send", description="Send the verification panel.")
@admin_only()
async def verify_panel_send(interaction: discord.Interaction, channel: discord.TextChannel):
    embed = discord.Embed(title="Server Verification", description="Connect your Discord account to confirm that it is already a member of this server.", color=discord.Color.green())
    await channel.send(embed=embed, view=VerifyPanelView(bot))
    await interaction.response.send_message(f"Verification panel sent to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="welcome-system", description="Configure the welcome message.")
@admin_only()
@app_commands.describe(channel="Welcome channel", message="Message placeholders: {user}, {username}, {server}", required_role="Optional role required to receive the welcome message")
async def welcome_system(interaction: discord.Interaction, channel: discord.TextChannel, message: str, required_role: discord.Role = None):
    if not interaction.guild:
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    await bot.db.update(interaction.guild.id, welcome_channel_id=channel.id, welcome_message=message, welcome_required_role_id=required_role.id if required_role else None)
    await interaction.response.send_message("Welcome system configured successfully.", ephemeral=True)

@bot.event
async def on_member_join(member: discord.Member):
    cfg = await bot.db.settings(member.guild.id)
    channel_id, message = cfg.get("welcome_channel_id"), cfg.get("welcome_message")
    if not channel_id or not message:
        return
    required = cfg.get("welcome_required_role_id")
    if required and not any(role.id == int(required) for role in member.roles):
        return
    channel = member.guild.get_channel(int(channel_id))
    if channel:
        text = message.replace("{user}", member.mention).replace("{username}", discord.utils.escape_markdown(member.name)).replace("{server}", discord.utils.escape_markdown(member.guild.name))
        await channel.send(text)

def html_page(title, body):
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title></head><body style='font-family:Arial,sans-serif;max-width:640px;margin:12vh auto;padding:24px;color:#20242b'><h1>{title}</h1><p>{body}</p></body></html>"

async def oauth_callback(request):
    code, state = request.query.get("code"), request.query.get("state")
    if request.query.get("error"):
        return web.Response(text=html_page("Verification cancelled", "You did not authorize the membership check."), content_type="text/html", status=400)
    if not code or not state:
        return web.Response(text=html_page("Verification failed", "Required OAuth2 information is missing."), content_type="text/html", status=400)
    if not all((CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)):
        return web.Response(text=html_page("Configuration error", "OAuth2 environment variables are incomplete."), content_type="text/html", status=500)
    if not await bot.db.consume_state(state):
        return web.Response(text=html_page("Verification expired", "Return to Discord and start verification again."), content_type="text/html", status=400)

    payload = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    async with ClientSession() as session:
        async with session.post("https://discord.com/api/oauth2/token", data=payload) as response:
            if response.status != 200:
                return web.Response(text=html_page("Verification failed", "Discord authorization could not be completed."), content_type="text/html", status=400)
            token = (await response.json()).get("access_token")
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(f"{API}/users/@me", headers=headers) as response:
            if response.status != 200:
                return web.Response(text=html_page("Verification failed", "Could not identify your Discord account."), content_type="text/html", status=400)
            user = await response.json()
        async with session.get(f"{API}/users/@me/guilds", headers=headers) as response:
            if response.status != 200:
                return web.Response(text=html_page("Verification failed", "Could not check your authorized server list. Make sure the guilds scope was approved."), content_type="text/html", status=400)
            guilds = await response.json()

    if not any(str(g.get("id")) == str(VERIFY_GUILD_ID) for g in guilds):
        return web.Response(text=html_page("Membership not found", "Your authorized Discord account is not currently a member of this server. This bot does not invite or add members."), content_type="text/html", status=403)

    guild = bot.get_guild(VERIFY_GUILD_ID)
    if guild is None:
        return web.Response(text=html_page("Bot unavailable", "The bot is not connected to the configured server."), content_type="text/html", status=503)
    cfg = await bot.db.settings(guild.id)
    verify_role_id = cfg.get("verify_role_id")
    member = guild.get_member(int(user["id"]))
    if member is None:
        try:
            member = await guild.fetch_member(int(user["id"]))
        except discord.NotFound:
            return web.Response(text=html_page("Membership check failed", "Discord confirmed no member record for this account in the target server."), content_type="text/html", status=403)
    verify_role = guild.get_role(int(verify_role_id)) if verify_role_id else None
    if verify_role is None:
        return web.Response(text=html_page("Configuration error", "The verification role has not been configured or no longer exists."), content_type="text/html", status=503)
    roles_to_add = [verify_role]
    extra_id = cfg.get("extra_role_id")
    if extra_id:
        extra = guild.get_role(int(extra_id))
        if extra:
            roles_to_add.append(extra)
    try:
        await member.add_roles(*roles_to_add, reason="OAuth2 confirmed existing server membership")
        remove_id = cfg.get("remove_role_id")
        remove_role = guild.get_role(int(remove_id)) if remove_id else None
        if remove_role and remove_role in member.roles:
            await member.remove_roles(remove_role, reason="OAuth2 verification completed")
    except discord.Forbidden:
        return web.Response(text=html_page("Role update failed", "The bot cannot manage the configured roles. Check Manage Roles and role hierarchy."), content_type="text/html", status=503)
    except discord.HTTPException:
        log.exception("Discord role update failed")
        return web.Response(text=html_page("Role update failed", "Discord could not update your roles. Please try again later."), content_type="text/html", status=503)

    return web.Response(text=html_page("Verification complete", "Your account is already a member of the server and your roles have been updated. You can return to Discord."), content_type="text/html")

async def start_web():
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="NadeSMP Discord bot service is online."))
    app.router.add_get("/oauth/callback", oauth_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner

async def run():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from Railway Variables.")
    await bot.db.init()
    runner = await start_web()
    try:
        await bot.start(TOKEN)
    finally:
        await runner.cleanup()
        await bot.close()

if __name__ == "__main__":
    asyncio.run(run())
