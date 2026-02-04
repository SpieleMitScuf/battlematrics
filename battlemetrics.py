"""BattleMetrics Cog for RedBot - Display server player counts in voice channels."""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

log = logging.getLogger("red.battlemetrics")

BATTLEMETRICS_API_URL = "https://api.battlemetrics.com"


class BattleMetrics(commands.Cog):
    """Display BattleMetrics server info in locked voice channels."""

    __version__ = "1.0.0"
    __author__ = "RedBot Community"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=8472651839274651,  # Unique identifier
            force_registration=True,
        )

        # Global defaults
        self.config.register_global(
            api_token=None,
            update_interval=300,  # 5 minutes default (API friendly)
        )

        # Guild-specific defaults
        self.config.register_guild(
            servers={},  # {battlemetrics_server_id: {"channel_id": int, "name": str}}
            category_id=None,
            channel_format="[{players}/{max}] {name}",
        )

        self.session: Optional[aiohttp.ClientSession] = None
        self._update_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def cog_load(self):
        """Called when the cog is loaded."""
        self.session = aiohttp.ClientSession()
        self._update_task = self.bot.loop.create_task(self._update_loop())
        log.info("BattleMetrics cog loaded")

    async def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self._update_task:
            self._update_task.cancel()
        if self.session:
            await self.session.close()
        log.info("BattleMetrics cog unloaded")

    async def _wait_until_ready(self):
        """Wait until the bot is ready."""
        await self.bot.wait_until_ready()
        self._ready.set()

    async def _update_loop(self):
        """Background task to update all voice channels."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)  # Initial delay

        while True:
            try:
                await self._update_all_channels()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in update loop: {e}")

            interval = await self.config.update_interval()
            await asyncio.sleep(interval)

    async def _fetch_server_info(self, server_id: str) -> Optional[Dict]:
        """Fetch server information from BattleMetrics API."""
        api_token = await self.config.api_token()

        headers = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        url = f"{BATTLEMETRICS_API_URL}/servers/{server_id}"

        try:
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {})
                elif resp.status == 429:
                    log.warning("BattleMetrics API rate limited")
                    return None
                else:
                    log.warning(f"BattleMetrics API returned status {resp.status}")
                    return None
        except asyncio.TimeoutError:
            log.warning(f"Timeout fetching server {server_id}")
            return None
        except Exception as e:
            log.error(f"Error fetching server {server_id}: {e}")
            return None

    async def _update_all_channels(self):
        """Update all tracked server channels across all guilds."""
        for guild in self.bot.guilds:
            servers = await self.config.guild(guild).servers()
            if not servers:
                continue

            channel_format = await self.config.guild(guild).channel_format()

            for bm_server_id, server_data in servers.items():
                channel_id = server_data.get("channel_id")
                if not channel_id:
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    continue

                # Fetch server info
                info = await self._fetch_server_info(bm_server_id)
                if not info:
                    continue

                attributes = info.get("attributes", {})
                players = attributes.get("players", 0)
                max_players = attributes.get("maxPlayers", 0)
                server_name = attributes.get("name", "Unknown")
                status = attributes.get("status", "offline")

                # Format channel name
                if status == "offline":
                    new_name = f"[OFFLINE] {server_data.get('name', server_name)}"
                else:
                    new_name = channel_format.format(
                        players=players,
                        max=max_players,
                        name=server_data.get("name", server_name),
                        status=status,
                    )

                # Truncate to Discord's limit (100 chars)
                new_name = new_name[:100]

                # Only update if name changed (avoid unnecessary API calls)
                if channel.name != new_name:
                    try:
                        await channel.edit(name=new_name)
                        log.debug(f"Updated channel {channel_id} to: {new_name}")
                    except discord.HTTPException as e:
                        log.error(f"Failed to update channel {channel_id}: {e}")

                # Small delay between updates to be nice to Discord API
                await asyncio.sleep(2)

    @commands.group(name="battlemetrics", aliases=["bm"])
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def battlemetrics(self, ctx: commands.Context):
        """BattleMetrics server tracker - Display player counts in voice channels."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @battlemetrics.command(name="settoken")
    @checks.is_owner()
    async def set_token(self, ctx: commands.Context, token: str):
        """Set the BattleMetrics API token (Bot Owner only).

        NOTE: A token is NOT required! The API works without authentication.
        A token only provides higher rate limits (300/min vs 60/min).

        Get your token at: https://www.battlemetrics.com/developers
        This message will be deleted for security.
        """
        await self.config.api_token.set(token)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send("API token has been set.", delete_after=10)

    @battlemetrics.command(name="setinterval")
    @checks.is_owner()
    async def set_interval(self, ctx: commands.Context, seconds: int):
        """Set the update interval in seconds (Bot Owner only).

        Minimum: 60 seconds (to respect API rate limits)
        Recommended: 300 seconds (5 minutes)
        """
        if seconds < 60:
            return await ctx.send("Interval must be at least 60 seconds to respect API rate limits.")

        await self.config.update_interval.set(seconds)
        await ctx.send(f"Update interval set to {seconds} seconds.")

    @battlemetrics.command(name="setcategory")
    async def set_category(self, ctx: commands.Context, category: discord.CategoryChannel):
        """Set the category where server info channels will be created.

        Example: [p]battlemetrics setcategory Server Stats
        """
        await self.config.guild(ctx.guild).category_id.set(category.id)
        await ctx.send(f"Server info channels will be created in: **{category.name}**")

    @battlemetrics.command(name="setformat")
    async def set_format(self, ctx: commands.Context, *, format_string: str):
        """Set the channel name format.

        Available placeholders:
        - {players} - Current player count
        - {max} - Max players
        - {name} - Server name (custom or from BattleMetrics)
        - {status} - Server status

        Default: [p]battlemetrics setformat [{players}/{max}] {name}
        """
        await self.config.guild(ctx.guild).channel_format.set(format_string)
        await ctx.send(f"Channel format set to: `{format_string}`")

    @battlemetrics.command(name="add")
    async def add_server(
        self, ctx: commands.Context, server_id: str, *, custom_name: str = None
    ):
        """Add a BattleMetrics server to track.

        Find the server ID from the BattleMetrics URL:
        https://www.battlemetrics.com/servers/squad/12345678
        The ID would be: 12345678

        Example: [p]battlemetrics add 12345678 My Squad Server
        """
        # Check if category is set
        category_id = await self.config.guild(ctx.guild).category_id()
        if not category_id:
            return await ctx.send(
                "Please set a category first with `[p]battlemetrics setcategory <category>`"
            )

        category = ctx.guild.get_channel(category_id)
        if not category:
            return await ctx.send("The configured category no longer exists. Please set a new one.")

        # Verify server exists on BattleMetrics
        async with ctx.typing():
            info = await self._fetch_server_info(server_id)
            if not info:
                return await ctx.send(
                    "Could not find that server on BattleMetrics. "
                    "Please check the server ID and try again."
                )

            attributes = info.get("attributes", {})
            server_name = custom_name or attributes.get("name", "Unknown Server")
            players = attributes.get("players", 0)
            max_players = attributes.get("maxPlayers", 0)

            # Create locked voice channel
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(
                    connect=False,
                    view_channel=True,
                ),
                ctx.guild.me: discord.PermissionOverwrite(
                    connect=True,
                    manage_channels=True,
                    view_channel=True,
                ),
            }

            # Add admin role permissions if exists
            for role in ctx.guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(
                        connect=False,
                        view_channel=True,
                    )

            channel_format = await self.config.guild(ctx.guild).channel_format()
            channel_name = channel_format.format(
                players=players,
                max=max_players,
                name=server_name,
                status="online",
            )[:100]

            try:
                channel = await category.create_voice_channel(
                    name=channel_name,
                    overwrites=overwrites,
                    reason=f"BattleMetrics tracker for server {server_id}",
                )
            except discord.HTTPException as e:
                return await ctx.send(f"Failed to create voice channel: {e}")

            # Save to config
            async with self.config.guild(ctx.guild).servers() as servers:
                servers[server_id] = {
                    "channel_id": channel.id,
                    "name": server_name,
                    "added_by": ctx.author.id,
                    "added_at": datetime.utcnow().isoformat(),
                }

        await ctx.send(
            f"Now tracking **{server_name}** (ID: {server_id})\n"
            f"Current players: {players}/{max_players}\n"
            f"Channel created: {channel.mention}"
        )

    @battlemetrics.command(name="remove")
    async def remove_server(self, ctx: commands.Context, server_id: str):
        """Remove a tracked server and delete its channel.

        Example: [p]battlemetrics remove 12345678
        """
        async with self.config.guild(ctx.guild).servers() as servers:
            if server_id not in servers:
                return await ctx.send("That server is not being tracked.")

            server_data = servers[server_id]
            channel_id = server_data.get("channel_id")
            server_name = server_data.get("name", "Unknown")

            # Delete the channel
            if channel_id:
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    try:
                        await channel.delete(reason="BattleMetrics tracker removed")
                    except discord.HTTPException:
                        pass

            del servers[server_id]

        await ctx.send(f"Removed **{server_name}** (ID: {server_id}) from tracking.")

    @battlemetrics.command(name="list")
    async def list_servers(self, ctx: commands.Context):
        """List all tracked BattleMetrics servers."""
        servers = await self.config.guild(ctx.guild).servers()

        if not servers:
            return await ctx.send("No servers are being tracked. Use `[p]battlemetrics add` to add one.")

        embed = discord.Embed(
            title="Tracked BattleMetrics Servers",
            color=discord.Color.blue(),
        )

        for server_id, data in servers.items():
            channel_id = data.get("channel_id")
            channel = ctx.guild.get_channel(channel_id) if channel_id else None
            channel_mention = channel.mention if channel else "Channel deleted"

            embed.add_field(
                name=data.get("name", "Unknown"),
                value=f"ID: `{server_id}`\nChannel: {channel_mention}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @battlemetrics.command(name="refresh")
    async def refresh_servers(self, ctx: commands.Context):
        """Manually refresh all tracked server channels."""
        async with ctx.typing():
            servers = await self.config.guild(ctx.guild).servers()
            if not servers:
                return await ctx.send("No servers are being tracked.")

            await ctx.send("Refreshing all tracked servers...")

            channel_format = await self.config.guild(ctx.guild).channel_format()
            updated = 0
            failed = 0

            for bm_server_id, server_data in servers.items():
                channel_id = server_data.get("channel_id")
                if not channel_id:
                    continue

                channel = ctx.guild.get_channel(channel_id)
                if not channel:
                    continue

                info = await self._fetch_server_info(bm_server_id)
                if not info:
                    failed += 1
                    continue

                attributes = info.get("attributes", {})
                players = attributes.get("players", 0)
                max_players = attributes.get("maxPlayers", 0)
                status = attributes.get("status", "offline")

                if status == "offline":
                    new_name = f"[OFFLINE] {server_data.get('name', 'Unknown')}"
                else:
                    new_name = channel_format.format(
                        players=players,
                        max=max_players,
                        name=server_data.get("name", "Unknown"),
                        status=status,
                    )[:100]

                try:
                    await channel.edit(name=new_name)
                    updated += 1
                except discord.HTTPException:
                    failed += 1

                await asyncio.sleep(2)

        await ctx.send(f"Refresh complete. Updated: {updated}, Failed: {failed}")

    @battlemetrics.command(name="info")
    async def server_info(self, ctx: commands.Context, server_id: str):
        """Get detailed info about a BattleMetrics server.

        Example: [p]battlemetrics info 12345678
        """
        async with ctx.typing():
            info = await self._fetch_server_info(server_id)
            if not info:
                return await ctx.send("Could not fetch server information.")

            attributes = info.get("attributes", {})

            embed = discord.Embed(
                title=attributes.get("name", "Unknown Server"),
                color=discord.Color.green() if attributes.get("status") == "online" else discord.Color.red(),
            )

            embed.add_field(
                name="Players",
                value=f"{attributes.get('players', 0)}/{attributes.get('maxPlayers', 0)}",
                inline=True,
            )
            embed.add_field(
                name="Status",
                value=attributes.get("status", "Unknown").title(),
                inline=True,
            )
            embed.add_field(
                name="Game",
                value=attributes.get("game", "Unknown"),
                inline=True,
            )

            if attributes.get("ip"):
                embed.add_field(
                    name="IP",
                    value=f"`{attributes.get('ip')}:{attributes.get('port', '')}`",
                    inline=True,
                )

            embed.add_field(
                name="Country",
                value=attributes.get("country", "Unknown"),
                inline=True,
            )
            embed.add_field(
                name="Rank",
                value=f"#{attributes.get('rank', 'N/A')}",
                inline=True,
            )

            embed.set_footer(text=f"Server ID: {server_id}")
            embed.url = f"https://www.battlemetrics.com/servers/{attributes.get('game', 'unknown')}/{server_id}"

        await ctx.send(embed=embed)

    @battlemetrics.command(name="settings")
    async def show_settings(self, ctx: commands.Context):
        """Show current BattleMetrics settings for this server."""
        category_id = await self.config.guild(ctx.guild).category_id()
        channel_format = await self.config.guild(ctx.guild).channel_format()
        update_interval = await self.config.update_interval()
        api_token = await self.config.api_token()
        servers = await self.config.guild(ctx.guild).servers()

        category = ctx.guild.get_channel(category_id) if category_id else None

        embed = discord.Embed(
            title="BattleMetrics Settings",
            color=discord.Color.blue(),
        )

        embed.add_field(
            name="Category",
            value=category.mention if category else "Not set",
            inline=True,
        )
        embed.add_field(
            name="Update Interval",
            value=f"{update_interval} seconds",
            inline=True,
        )
        embed.add_field(
            name="API Token",
            value="Set (higher rate limits)" if api_token else "Not set (not required)",
            inline=True,
        )
        embed.add_field(
            name="Channel Format",
            value=f"`{channel_format}`",
            inline=False,
        )
        embed.add_field(
            name="Tracked Servers",
            value=str(len(servers)),
            inline=True,
        )

        await ctx.send(embed=embed)

    @battlemetrics.command(name="search")
    async def search_servers(self, ctx: commands.Context, *, query: str):
        """Search for servers on BattleMetrics.

        Example: [p]battlemetrics search Squad Server Name
        """
        api_token = await self.config.api_token()
        headers = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        params = {
            "filter[search]": query,
            "filter[game]": "squad",
            "page[size]": 10,
        }

        async with ctx.typing():
            try:
                async with self.session.get(
                    f"{BATTLEMETRICS_API_URL}/servers",
                    headers=headers,
                    params=params,
                    timeout=30,
                ) as resp:
                    if resp.status != 200:
                        return await ctx.send("Failed to search servers.")

                    data = await resp.json()
                    servers = data.get("data", [])
            except Exception as e:
                return await ctx.send(f"Error searching servers: {e}")

            if not servers:
                return await ctx.send("No servers found matching your query.")

            embed = discord.Embed(
                title=f"Search Results for: {query}",
                color=discord.Color.blue(),
            )

            for server in servers[:10]:
                attrs = server.get("attributes", {})
                server_id = server.get("id", "Unknown")
                name = attrs.get("name", "Unknown")
                players = attrs.get("players", 0)
                max_players = attrs.get("maxPlayers", 0)
                status = attrs.get("status", "unknown")

                embed.add_field(
                    name=name[:256],
                    value=(
                        f"ID: `{server_id}`\n"
                        f"Players: {players}/{max_players}\n"
                        f"Status: {status.title()}"
                    ),
                    inline=False,
                )

            embed.set_footer(text="Use [p]battlemetrics add <server_id> to track a server")

        await ctx.send(embed=embed)
