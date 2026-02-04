from .battlemetrics import BattleMetrics


async def setup(bot):
    """Load the BattleMetrics cog."""
    cog = BattleMetrics(bot)
    await bot.add_cog(cog)
