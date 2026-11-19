import asyncio
import difflib
import os
import random
import re
import sqlite3
import time
import unicodedata

import discord
from discord import app_commands
from discord.ext import tasks

from flags import COUNTRIES

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------- Einstellungen ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
ROUNDS = 5                                   # Flaggen pro Quiz
INTERVAL_HOURS = 12                          # Abstand zwischen Quizzen
ROUND_SECONDS = 20                           # Zeit pro Flagge
XP_PER_FLAG = int(os.getenv("XP_PER_FLAG", 10))      # XP für richtige Antwort
XP_QUIZ_WINNER = int(os.getenv("XP_QUIZ_WINNER", 20))  # Bonus für Gesamtsieger
DB_FILE = "quiz.db"
# -----------------------------------------------

db = sqlite3.connect(DB_FILE)
db.execute("CREATE TABLE IF NOT EXISTS config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, last_run REAL DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS xp (guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
db.commit()


def add_xp(guild_id, user_id, amount):
    db.execute(
        "INSERT INTO xp (guild_id, user_id, xp) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + ?",
        (guild_id, user_id, amount, amount),
    )
    db.commit()


def normalize(text: str) -> str:
    text = text.lower().strip()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text)


def is_correct(guess: str, answers: list[str]) -> bool:
    g = normalize(guess)
    if not g:
        return False
    for a in answers:
        if g == a:
            return True
        # kleine Tippfehler erlauben (nur bei längeren Namen)
        if len(a) >= 6 and difflib.SequenceMatcher(None, g, a).ratio() >= 0.88:
            return True
    return False


intents = discord.Intents.default()
intents.message_content = True  # im Developer Portal aktivieren!


class QuizBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.running = set()

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()

    @tasks.loop(minutes=5)
    async def scheduler(self):
        now = time.time()
        rows = db.execute("SELECT guild_id, channel_id, last_run FROM config WHERE channel_id IS NOT NULL").fetchall()
        for guild_id, channel_id, last_run in rows:
            if now - last_run >= INTERVAL_HOURS * 3600 and guild_id not in self.running:
                channel = self.get_channel(channel_id)
                if channel is None:
                    continue
                db.execute("UPDATE config SET last_run=? WHERE guild_id=?", (now, guild_id))
                db.commit()
                asyncio.create_task(self.run_quiz(channel))

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()

    async def wait_for_answer(self, channel, answers):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ROUND_SECONDS
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                msg = await self.wait_for(
                    "message",
                    check=lambda m: m.channel.id == channel.id and not m.author.bot,
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return None
            if is_correct(msg.content, answers):
                return msg

    async def run_quiz(self, channel: discord.TextChannel):
        gid = channel.guild.id
        if gid in self.running:
            return False
        self.running.add(gid)
        try:
            scores = {}
            picks = random.sample(COUNTRIES, ROUNDS)
            await channel.send(embed=discord.Embed(
                title="🌍 Flaggen-Quiz startet!",
                description=(
                    f"**{ROUNDS} Flaggen**, je **{ROUND_SECONDS} Sekunden**.\n"
                    f"Wer als Erstes richtig antwortet, bekommt **{XP_PER_FLAG} XP**.\n"
                    f"Schreib einfach den Ländernamen in den Chat!"
                ),
                color=discord.Color.blurple(),
            ))
            await asyncio.sleep(4)

            for i, (code, name, aliases) in enumerate(picks, 1):
                answers = [normalize(name)] + [normalize(a) for a in aliases]
                embed = discord.Embed(
                    title=f"Flagge {i}/{ROUNDS} – Welches Land ist das?",
                    color=discord.Color.gold(),
                )
                embed.set_image(url=f"https://flagcdn.com/w640/{code}.png")
                embed.set_footer(text=f"Du hast {ROUND_SECONDS} Sekunden Zeit")
                await channel.send(embed=embed)

                msg = await self.wait_for_answer(channel, answers)
                if msg:
                    add_xp(gid, msg.author.id, XP_PER_FLAG)
                    scores[msg.author.id] = scores.get(msg.author.id, 0) + 1
                    await msg.add_reaction("✅")
                    await channel.send(f"🎉 {msg.author.mention} hat es erraten: **{name}**! (+{XP_PER_FLAG} XP)")
                else:
                    await channel.send(f"⏰ Zeit abgelaufen! Die Antwort war **{name}**.")
                await asyncio.sleep(4)

            # Zusammenfassung
            if not scores:
                await channel.send("Niemand hat etwas erraten – bis zum nächsten Mal! 😅")
                return True

            ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            top = ranking[0][1]
            winners = [uid for uid, s in ranking if s == top]
            for uid in winners:
                add_xp(gid, uid, XP_QUIZ_WINNER)

            lines = [f"**{n}.** <@{uid}> – {s} richtig" for n, (uid, s) in enumerate(ranking, 1)]
            win_text = ", ".join(f"<@{u}>" for u in winners)
            embed = discord.Embed(
                title="🏁 Quiz beendet!",
                description="\n".join(lines) + f"\n\n🏆 Gewinner: {win_text} (+{XP_QUIZ_WINNER} Bonus-XP)",
                color=discord.Color.green(),
            )
            embed.set_footer(text="Nutze /leaderboard für die Gesamtwertung")
            await channel.send(embed=embed)
            return True
        finally:
            self.running.discard(gid)


bot = QuizBot()


@bot.tree.command(name="quiz_kanal", description="Legt den Kanal für das Flaggen-Quiz fest")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_kanal(interaction: discord.Interaction, kanal: discord.TextChannel):
    db.execute(
        "INSERT INTO config (guild_id, channel_id, last_run) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
        (interaction.guild_id, kanal.id, time.time()),
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ Das Quiz läuft jetzt alle {INTERVAL_HOURS} Stunden in {kanal.mention}. "
        f"Mit `/quiz_start` kannst du sofort eins starten.",
        ephemeral=True,
    )


@bot.tree.command(name="quiz_start", description="Startet sofort ein Flaggen-Quiz")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_start(interaction: discord.Interaction):
    row = db.execute("SELECT channel_id FROM config WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    channel = bot.get_channel(row[0]) if row and row[0] else interaction.channel
    if interaction.guild_id in bot.running:
        await interaction.response.send_message("Es läuft bereits ein Quiz!", ephemeral=True)
        return
    await interaction.response.send_message("Quiz wird gestartet…", ephemeral=True)
    db.execute(
        "INSERT INTO config (guild_id, channel_id, last_run) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET last_run=excluded.last_run",
        (interaction.guild_id, channel.id, time.time()),
    )
    db.commit()
    asyncio.create_task(bot.run_quiz(channel))


@bot.tree.command(name="leaderboard", description="Zeigt die Top 10 der Schlausten")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, xp FROM xp WHERE guild_id=? ORDER BY xp DESC LIMIT 10", (interaction.guild_id,)
    ).fetchall()
    if not rows:
        await interaction.response.send_message("Noch keine XP vergeben – spiel beim nächsten Quiz mit! 🌍")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{uid}> – **{xp} XP**"
        for i, (uid, xp) in enumerate(rows)
    ]
    await interaction.response.send_message(embed=discord.Embed(
        title="🧠 Leaderboard – Wer ist am schlausten?",
        description="\n".join(lines),
        color=discord.Color.gold(),
    ))


@bot.tree.command(name="rang", description="Zeigt deine XP und deinen Platz")
@app_commands.guild_only()
async def rang(interaction: discord.Interaction, nutzer: discord.Member = None):
    nutzer = nutzer or interaction.user
    row = db.execute(
        "SELECT xp FROM xp WHERE guild_id=? AND user_id=?", (interaction.guild_id, nutzer.id)
    ).fetchone()
    if not row:
        await interaction.response.send_message(f"{nutzer.display_name} hat noch keine XP.")
        return
    place = db.execute(
        "SELECT COUNT(*) + 1 FROM xp WHERE guild_id=? AND xp > ?", (interaction.guild_id, row[0])
    ).fetchone()[0]
    await interaction.response.send_message(f"📊 {nutzer.mention}: **{row[0]} XP** – Platz **{place}**")


@bot.event
async def on_ready():
    print(f"Eingeloggt als {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Bitte DISCORD_TOKEN in .env setzen (siehe .env.example)")
    bot.run(TOKEN)
