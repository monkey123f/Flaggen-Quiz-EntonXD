import asyncio
import difflib
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata

import discord
from discord import app_commands
from discord.ext import tasks  # noqa: F401  (für spätere Erweiterungen)

from flags import COUNTRIES, EN_NAMES

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("flaggenquiz")

# ======================= Einstellungen =======================
TOKEN = os.getenv("DISCORD_TOKEN")
LEADERBOARD_INTERVAL_MINUTES = 5  # Leaderboard höchstens alle X Minuten (nur wenn Flaggen beantwortet wurden)
HINT_1_SECONDS = 60               # Erster Tipp (Anfangsbuchstabe + Länge) nach X Sekunden
HINT_2_SECONDS = 120              # Zweiter Tipp (auch letzter Buchstabe) nach X Sekunden
PAUSE_BETWEEN_FLAGS = 4           # Sekunden Pause nach einer gelösten Flagge
LEADERBOARD_SIZE = 10             # Plätze im Leaderboard
XP_PER_FLAG = int(os.getenv("XP_PER_FLAG", 10))  # XP für die richtige Antwort
XP_PER_LEVEL = 100                # XP pro Level
DB_FILE = "quiz.db"
# =============================================================

COLOR_PRIMARY = discord.Color.from_rgb(88, 101, 242)
COLOR_SUCCESS = discord.Color.from_rgb(46, 204, 113)
COLOR_GOLD = discord.Color.from_rgb(241, 196, 15)
COLOR_MUTED = discord.Color.from_rgb(149, 165, 166)
COLOR_WARN = discord.Color.from_rgb(230, 126, 34)
COLOR_ERROR = discord.Color.from_rgb(231, 76, 60)

# ----------------------- Datenbank -----------------------
db = sqlite3.connect(DB_FILE)
db.execute(
    "CREATE TABLE IF NOT EXISTS config ("
    "guild_id INTEGER PRIMARY KEY, channel_id INTEGER, last_run REAL DEFAULT 0, active INTEGER DEFAULT 1)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS xp ("
    "guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, correct INTEGER DEFAULT 0, "
    "PRIMARY KEY (guild_id, user_id))"
)
for _migration in (  # Migrationen für Datenbanken aus älteren Versionen
    "ALTER TABLE xp ADD COLUMN correct INTEGER DEFAULT 0",
    "ALTER TABLE config ADD COLUMN active INTEGER DEFAULT 1",
):
    try:
        db.execute(_migration)
    except sqlite3.OperationalError:
        pass
db.commit()


def add_xp(guild_id: int, user_id: int, amount: int, correct: int = 0) -> None:
    db.execute(
        "INSERT INTO xp (guild_id, user_id, xp, correct) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "xp = xp + excluded.xp, correct = correct + excluded.correct",
        (guild_id, user_id, amount, correct),
    )
    db.commit()


def save_channel(guild_id: int, channel_id: int) -> None:
    db.execute(
        "INSERT INTO config (guild_id, channel_id, active) VALUES (?, ?, 1) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, active=1",
        (guild_id, channel_id),
    )
    db.commit()


# ----------------------- Hilfsfunktionen -----------------------
def normalize(text: str) -> str:
    text = text.lower().strip()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text)


def country_answers(code: str, name: str, aliases: list[str]) -> list[str]:
    """Alle akzeptierten Antworten (Deutsch + Englisch), normalisiert."""
    names = [name] + list(aliases) + EN_NAMES.get(code, [])
    return list({normalize(n) for n in names})


# Alle gültigen Ländernamen – damit z. B. "Austria" nie als Tippfehler von "Australia" durchgeht
ALL_NAMES = set()
for _code, _name, _aliases in COUNTRIES:
    ALL_NAMES.update(country_answers(_code, _name, _aliases))


def is_correct(guess: str, answers: list[str]) -> bool:
    g = normalize(guess)
    if not g:
        return False
    if g in answers:
        return True
    if g in ALL_NAMES:  # ein anderes, gültiges Land -> falsch
        return False
    # kleine Tippfehler erlauben (nur bei längeren Namen)
    return any(
        len(a) >= 6 and difflib.SequenceMatcher(None, g, a).ratio() >= 0.9
        for a in answers
    )


def make_hint(name: str, level: int) -> str:
    letters = [i for i, c in enumerate(name) if c.isalpha()]
    reveal = {letters[0]}
    if level >= 2:
        reveal.add(letters[-1])
    chars = []
    for i, c in enumerate(name):
        if not c.isalpha():
            chars.append(c)
        elif i in reveal:
            chars.append(c)
        else:
            chars.append("_")
    return " ".join(chars)


def flag_emoji(code: str) -> str:
    if len(code) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in code.lower())


def bar(value: int, total: int, length: int = 10) -> str:
    filled = max(0, min(length, round(length * value / total))) if total else 0
    return "▰" * filled + "▱" * (length - filled)


def rank_label(index: int) -> str:
    medals = ["🥇", "🥈", "🥉"]
    return medals[index] if index < 3 else f"`{index + 1:>2}.`"


def level_of(xp: int) -> int:
    return xp // XP_PER_LEVEL + 1


def leaderboard_embed(guild_id: int, limit: int = LEADERBOARD_SIZE) -> discord.Embed | None:
    rows = db.execute(
        "SELECT user_id, xp, correct FROM xp WHERE guild_id=? ORDER BY xp DESC, correct DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    if not rows:
        return None
    total_players = db.execute("SELECT COUNT(*) FROM xp WHERE guild_id=?", (guild_id,)).fetchone()[0]
    lines = [
        f"{rank_label(i)} <@{uid}>  ·  **{xp} XP**  ·  Level {level_of(xp)}"
        for i, (uid, xp, _correct) in enumerate(rows)
    ]
    embed = discord.Embed(
        title="🧠 Leaderboard – Wer ist am schlausten?",
        description="\n".join(lines),
        color=COLOR_GOLD,
    )
    embed.set_footer(text=f"{total_players} Spieler insgesamt  ·  Mit /rang siehst du deinen Platz")
    return embed


def info_embed(text: str, color: discord.Color = COLOR_PRIMARY, title: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=text, color=color)


# ----------------------- Bot -----------------------
intents = discord.Intents.default()
intents.message_content = True  # im Developer Portal aktivieren!


class QuizBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.running: dict[int, asyncio.Task] = {}  # guild_id -> Task der Dauer-Schleife
        self.state: dict[int, dict] = {}            # guild_id -> Zähler für das Leaderboard
        self._resumed = False

    async def setup_hook(self):
        # Befehle nur synchronisieren, wenn gewünscht (verhindert Rate-Limits bei jedem Start)
        if os.getenv("SYNC_COMMANDS") == "1":
            await self.tree.sync()
            log.info("Slash-Befehle synchronisiert")

    async def on_ready(self):
        log.info("Eingeloggt als %s (%d Länder im Quiz)", self.user, len(COUNTRIES))
        if self._resumed:
            return
        self._resumed = True
        # Nach einem Neustart laufen die Quizze in den gespeicherten Kanälen automatisch weiter
        rows = db.execute("SELECT guild_id, channel_id FROM config WHERE active=1 AND channel_id IS NOT NULL").fetchall()
        for guild_id, channel_id in rows:
            channel = self.get_channel(channel_id)
            if channel is None:
                log.warning("Kanal %s (Server %s) nicht gefunden", channel_id, guild_id)
                continue
            self.start_loop(channel, announce=False)
            log.info("Quiz in Server %s fortgesetzt", guild_id)

    # ---------- Dauer-Schleife ----------
    def start_loop(self, channel: discord.TextChannel, announce: bool) -> asyncio.Task:
        gid = channel.guild.id
        old = self.running.get(gid)
        if old:
            old.cancel()
        st = {"channel": channel, "answered": 0, "last_board": time.time()}
        self.state[gid] = st
        task = asyncio.create_task(self.run_loop(channel, st, announce))
        self.running[gid] = task
        return task

    def stop_loop(self, guild_id: int) -> bool:
        task = self.running.pop(guild_id, None)
        self.state.pop(guild_id, None)
        if task:
            task.cancel()
            return True
        return False

    async def wait_for_answer(self, channel, name: str, answers: list[str]):
        """Wartet unbegrenzt auf die richtige Antwort und zeigt nach und nach Tipps an."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        hints = [(HINT_1_SECONDS, 1), (HINT_2_SECONDS, 2)]
        next_hint = 0
        while True:
            wait = None
            if next_hint < len(hints):
                wait = max(start + hints[next_hint][0] - loop.time(), 0.1)
            try:
                msg = await self.wait_for(
                    "message",
                    check=lambda m: m.channel.id == channel.id and not m.author.bot,
                    timeout=wait,
                )
            except asyncio.TimeoutError:
                if next_hint < len(hints) and loop.time() >= start + hints[next_hint][0]:
                    await channel.send(embed=discord.Embed(
                        title="💡 Tipp",
                        description=f"`{make_hint(name, hints[next_hint][1])}`",
                        color=COLOR_WARN,
                    ))
                    next_hint += 1
                continue
            if is_correct(msg.content, answers):
                return msg

    async def run_loop(self, channel: discord.TextChannel, st: dict, announce: bool):
        gid = channel.guild.id
        bag: list = []
        number = 0
        try:
            if announce:
                intro = discord.Embed(
                    title="🌍 Flaggen-Quiz",
                    description=(
                        "Ab jetzt läuft das Flaggen-Quiz dauerhaft.\n"
                        "Schreibe den Ländernamen in den Chat – **Deutsch oder English**. "
                        "Die nächste Flagge kommt erst, wenn die aktuelle erraten wurde."
                    ),
                    color=COLOR_PRIMARY,
                )
                intro.add_field(name="Pro Flagge", value=f"+{XP_PER_FLAG} XP", inline=True)
                intro.add_field(name="Leaderboard", value=f"alle {LEADERBOARD_INTERVAL_MINUTES} Min bei Aktivität", inline=True)
                await channel.send(embed=intro)
                await asyncio.sleep(3)

            while True:
                if not bag:  # alle Länder einmal durchmischen, damit sich nichts wiederholt
                    bag = COUNTRIES[:]
                    random.shuffle(bag)
                code, name, aliases = bag.pop()
                number += 1
                try:
                    await self._ask(channel, st, number, code, name, aliases)
                except asyncio.CancelledError:
                    raise
                except discord.NotFound:
                    log.warning("Kanal %s existiert nicht mehr – Quiz in Server %s beendet", channel.id, gid)
                    db.execute("UPDATE config SET active=0 WHERE guild_id=?", (gid,))
                    db.commit()
                    return
                except discord.HTTPException as e:
                    log.warning("Discord-Fehler im Server %s: %s – neuer Versuch in 30 s", gid, e)
                    await asyncio.sleep(30)
                except Exception:
                    log.exception("Fehler im Quiz (Server %s) – weiter in 10 s", gid)
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        finally:
            if self.running.get(gid) is asyncio.current_task():
                self.running.pop(gid, None)
                self.state.pop(gid, None)

    async def _ask(self, channel: discord.TextChannel, st: dict, number: int, code: str, name: str, aliases: list[str]):
        gid = channel.guild.id
        answers = country_answers(code, name, aliases)
        english = EN_NAMES.get(code, [name])[0]

        question = discord.Embed(
            title="Welche Flagge ist das?",
            description=f"Flagge **#{number}**",
            color=COLOR_PRIMARY,
        )
        question.set_image(url=f"https://flagcdn.com/w640/{code}.png")
        question.set_footer(text="Schreibe den Ländernamen (Deutsch oder English) in den Chat")
        await channel.send(embed=question)

        started = time.monotonic()
        msg = await self.wait_for_answer(channel, name, answers)
        took = time.monotonic() - started

        add_xp(gid, msg.author.id, XP_PER_FLAG, correct=1)
        st["answered"] += 1
        try:
            await msg.add_reaction("✅")
        except discord.HTTPException:
            pass

        result = discord.Embed(
            title=f"{flag_emoji(code)} {name}",
            description=f"✅ **{msg.author.display_name}** hat die Flagge erraten!",
            color=COLOR_SUCCESS,
        )
        result.add_field(name="English", value=english, inline=True)
        result.add_field(name="Belohnung", value=f"+{XP_PER_FLAG} XP", inline=True)
        result.add_field(name="Zeit", value=f"{took:.1f} s", inline=True)
        result.set_thumbnail(url=msg.author.display_avatar.url)
        result.set_footer(text=f"Flagge #{number}")
        await channel.send(embed=result)

        # Leaderboard: höchstens alle X Minuten und nur, wenn Flaggen beantwortet wurden
        if st["answered"] > 0 and time.time() - st["last_board"] >= LEADERBOARD_INTERVAL_MINUTES * 60:
            board = leaderboard_embed(gid)
            st["answered"] = 0
            st["last_board"] = time.time()
            if board:
                await channel.send(embed=board)

        await asyncio.sleep(PAUSE_BETWEEN_FLAGS)


bot = QuizBot()


# ----------------------- Slash-Befehle -----------------------
@bot.tree.command(name="quiz_kanal", description="Legt den Kanal fest und startet dort das dauerhafte Flaggen-Quiz")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_kanal(interaction: discord.Interaction, kanal: discord.TextChannel):
    perms = kanal.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            embed=info_embed(
                f"Der Bot braucht in {kanal.mention} die Rechte **Nachrichten senden** und **Links einbetten**.",
                COLOR_ERROR, "Fehlende Rechte",
            ),
            ephemeral=True,
        )
        return
    save_channel(interaction.guild_id, kanal.id)
    bot.start_loop(kanal, announce=True)
    await interaction.response.send_message(
        embed=info_embed(
            f"Das Flaggen-Quiz läuft jetzt dauerhaft in {kanal.mention}.\n"
            f"Pause mit `/quiz_stop`, weiter mit `/quiz_start`.",
            COLOR_SUCCESS, "✅ Kanal gespeichert",
        ),
        ephemeral=True,
    )


@bot.tree.command(name="quiz_start", description="Startet das dauerhafte Flaggen-Quiz")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_start(interaction: discord.Interaction):
    if interaction.guild_id in bot.running:
        await interaction.response.send_message(
            embed=info_embed("Das Quiz läuft bereits.", COLOR_WARN), ephemeral=True
        )
        return
    row = db.execute("SELECT channel_id FROM config WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    channel = (bot.get_channel(row[0]) if row and row[0] else None) or interaction.channel
    save_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        embed=info_embed(f"Das Quiz startet in {channel.mention}.", COLOR_SUCCESS, "🚀 Quiz gestartet"),
        ephemeral=True,
    )
    bot.start_loop(channel, announce=True)


@bot.tree.command(name="quiz_stop", description="Stoppt das Flaggen-Quiz")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_stop(interaction: discord.Interaction):
    st = bot.state.get(interaction.guild_id)
    channel = st["channel"] if st else None
    if not bot.stop_loop(interaction.guild_id):
        await interaction.response.send_message(
            embed=info_embed("Es läuft gerade kein Quiz.", COLOR_WARN), ephemeral=True
        )
        return
    db.execute("UPDATE config SET active=0 WHERE guild_id=?", (interaction.guild_id,))
    db.commit()
    await interaction.response.send_message(
        embed=info_embed("Das Quiz wurde gestoppt. Mit `/quiz_start` geht es weiter.", COLOR_ERROR), ephemeral=True
    )
    if channel:
        try:
            await channel.send(embed=info_embed("Das Flaggen-Quiz wurde von einem Admin gestoppt.", COLOR_ERROR, "🛑 Quiz gestoppt"))
        except discord.HTTPException:
            pass


@bot.tree.command(name="leaderboard", description="Zeigt die Top 10 der Schlausten")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    board = leaderboard_embed(interaction.guild_id)
    if board is None:
        await interaction.response.send_message(
            embed=info_embed("Noch keine XP vergeben – spiel beim nächsten Quiz mit! 🌍", COLOR_MUTED, "Leaderboard")
        )
        return
    await interaction.response.send_message(embed=board)


@bot.tree.command(name="rang", description="Zeigt deine XP, dein Level und deinen Platz")
@app_commands.guild_only()
async def rang(interaction: discord.Interaction, nutzer: discord.Member | None = None):
    nutzer = nutzer or interaction.user
    row = db.execute(
        "SELECT xp, correct FROM xp WHERE guild_id=? AND user_id=?", (interaction.guild_id, nutzer.id)
    ).fetchone()
    if not row:
        await interaction.response.send_message(
            embed=info_embed(f"{nutzer.mention} hat noch keine XP gesammelt.", COLOR_MUTED, "Noch keine Statistik")
        )
        return
    xp, correct = row
    place = db.execute(
        "SELECT COUNT(*) + 1 FROM xp WHERE guild_id=? AND xp > ?", (interaction.guild_id, xp)
    ).fetchone()[0]
    into_level = xp % XP_PER_LEVEL
    embed = discord.Embed(title=f"📊 Profil von {nutzer.display_name}", color=COLOR_PRIMARY)
    embed.set_thumbnail(url=nutzer.display_avatar.url)
    embed.add_field(name="Platz", value=f"#{place}", inline=True)
    embed.add_field(name="XP", value=str(xp), inline=True)
    embed.add_field(name="Richtige Flaggen", value=str(correct), inline=True)
    embed.add_field(
        name=f"Level {level_of(xp)}",
        value=f"`{bar(into_level, XP_PER_LEVEL)}`  {into_level}/{XP_PER_LEVEL} XP bis Level {level_of(xp) + 1}",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Bitte DISCORD_TOKEN in .env setzen (siehe .env.example)")
    bot.run(TOKEN, log_handler=None)
