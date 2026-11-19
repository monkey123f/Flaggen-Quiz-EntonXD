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
from discord.ext import tasks

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
ROUNDS = 5                      # Flaggen pro Quiz
INTERVAL_MINUTES = 5            # Pause zwischen dem Ende eines Quiz und dem nächsten automatischen Quiz
ACTIVITY_WINDOW_MINUTES = 10    # So lange gilt der Kanal nach der letzten Nachricht als "aktiv"
ROUND_TIMEOUT_MINUTES = 30      # Sicherheit: Quiz endet, wenn so lange niemand antwortet
HINT_1_SECONDS = 60             # Erster Tipp (Anfangsbuchstabe + Länge) nach X Sekunden
HINT_2_SECONDS = 120            # Zweiter Tipp (auch letzter Buchstabe) nach X Sekunden
PAUSE_BETWEEN_ROUNDS = 4        # Sekunden Pause zwischen den Flaggen
LEADERBOARD_SIZE = 10           # Plätze im Leaderboard
XP_PER_FLAG = int(os.getenv("XP_PER_FLAG", 10))          # XP für richtige Antwort
XP_QUIZ_WINNER = int(os.getenv("XP_QUIZ_WINNER", 20))    # Bonus-XP für den Quiz-Sieger
XP_PER_LEVEL = 100              # XP pro Level
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
    "guild_id INTEGER PRIMARY KEY, channel_id INTEGER, last_run REAL DEFAULT 0)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS xp ("
    "guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, correct INTEGER DEFAULT 0, "
    "PRIMARY KEY (guild_id, user_id))"
)
try:  # Migration für Datenbanken aus älteren Versionen
    db.execute("ALTER TABLE xp ADD COLUMN correct INTEGER DEFAULT 0")
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
            chars.append(c if c != " " else " ")
        elif i in reveal:
            chars.append(c)
        else:
            chars.append("_")
    return " ".join(chars)


def flag_emoji(code: str) -> str:
    if len(code) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in code.lower())


def progress_dots(current: int, total: int) -> str:
    return "●" * current + "○" * (total - current)


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
        self.running: dict[int, asyncio.Task] = {}  # guild_id -> laufende Quiz-Task
        self.last_activity: dict[int, float] = {}   # channel_id -> Zeitpunkt der letzten Nachricht eines Users

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        self.last_activity[message.channel.id] = time.time()

    async def setup_hook(self):
        # Befehle nur synchronisieren, wenn gewünscht (verhindert Rate-Limits bei jedem Start)
        if os.getenv("SYNC_COMMANDS") == "1":
            await self.tree.sync()
            log.info("Slash-Befehle synchronisiert")
        self.scheduler.start()

    @tasks.loop(seconds=30)
    async def scheduler(self):
        now = time.time()
        rows = db.execute("SELECT guild_id, channel_id, last_run FROM config WHERE channel_id IS NOT NULL").fetchall()
        for guild_id, channel_id, last_run in rows:
            try:
                if now - last_run >= INTERVAL_MINUTES * 60 and guild_id not in self.running:
                    channel = self.get_channel(channel_id)
                    if channel is None:
                        continue
                    # Nur starten, wenn im Kanal zuletzt jemand geschrieben hat
                    last_msg = self.last_activity.get(channel_id, 0)
                    if now - last_msg > ACTIVITY_WINDOW_MINUTES * 60:
                        continue
                    db.execute("UPDATE config SET last_run=? WHERE guild_id=?", (now, guild_id))
                    db.commit()
                    asyncio.create_task(self.run_quiz(channel))
            except Exception:
                log.exception("Fehler im Scheduler (Server %s)", guild_id)

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()

    async def wait_for_answer(self, channel, name: str, answers: list[str]):
        """Wartet auf die richtige Antwort. Zeigt nach und nach Tipps an. Gibt None bei Timeout zurück."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + ROUND_TIMEOUT_MINUTES * 60
        hints = [(HINT_1_SECONDS, 1), (HINT_2_SECONDS, 2)]
        next_hint = 0
        while True:
            now = loop.time()
            if now >= deadline:
                return None
            wait = deadline - now
            if next_hint < len(hints):
                wait = min(wait, start + hints[next_hint][0] - now)
            try:
                msg = await self.wait_for(
                    "message",
                    check=lambda m: m.channel.id == channel.id and not m.author.bot,
                    timeout=max(wait, 0.1),
                )
            except asyncio.TimeoutError:
                if next_hint < len(hints) and loop.time() >= start + hints[next_hint][0]:
                    level = hints[next_hint][1]
                    await channel.send(embed=discord.Embed(
                        title="💡 Tipp",
                        description=f"`{make_hint(name, level)}`",
                        color=COLOR_WARN,
                    ))
                    next_hint += 1
                continue
            if is_correct(msg.content, answers):
                return msg

    async def run_quiz(self, channel: discord.TextChannel) -> bool:
        gid = channel.guild.id
        if gid in self.running:
            return False
        self.running[gid] = asyncio.current_task()
        try:
            await self._play(channel)
            return True
        except asyncio.CancelledError:
            await channel.send(embed=info_embed("Das Quiz wurde von einem Admin abgebrochen.", COLOR_ERROR, "🛑 Quiz abgebrochen"))
            return False
        except discord.Forbidden:
            log.warning("Fehlende Rechte im Kanal %s (Server %s)", channel.id, gid)
            return False
        except Exception:
            log.exception("Quiz in Server %s fehlgeschlagen", gid)
            return False
        finally:
            self.running.pop(gid, None)
            # Die Pause bis zum nächsten Quiz beginnt erst, wenn dieses Quiz zu Ende ist
            db.execute("UPDATE config SET last_run=? WHERE guild_id=?", (time.time(), gid))
            db.commit()

    async def _play(self, channel: discord.TextChannel):
        gid = channel.guild.id
        scores: dict[int, int] = {}
        picks = random.sample(COUNTRIES, ROUNDS)

        intro = discord.Embed(
            title="🌍 Flaggen-Quiz",
            description=(
                "Erkenne die Flagge und schreibe den Ländernamen in den Chat – **Deutsch oder English**.\n"
                "Wer zuerst richtig liegt, gewinnt die Runde."
            ),
            color=COLOR_PRIMARY,
        )
        intro.add_field(name="Runden", value=f"{ROUNDS} Flaggen", inline=True)
        intro.add_field(name="Pro Flagge", value=f"+{XP_PER_FLAG} XP", inline=True)
        intro.add_field(name="Quiz-Sieger", value=f"+{XP_QUIZ_WINNER} XP", inline=True)
        intro.set_footer(text="Die nächste Flagge erscheint, sobald die aktuelle gelöst wurde  ·  Los geht's gleich …")
        await channel.send(embed=intro)
        await asyncio.sleep(5)

        for i, (code, name, aliases) in enumerate(picks, 1):
            answers = country_answers(code, name, aliases)
            english = EN_NAMES.get(code, [name])[0]

            question = discord.Embed(
                title="Welche Flagge ist das?",
                description=f"`{progress_dots(i, ROUNDS)}`  Runde **{i}** von **{ROUNDS}**",
                color=COLOR_PRIMARY,
            )
            question.set_image(url=f"https://flagcdn.com/w640/{code}.png")
            question.set_footer(text="Schreibe den Ländernamen (Deutsch oder English) in den Chat")
            await channel.send(embed=question)

            started = time.monotonic()
            msg = await self.wait_for_answer(channel, name, answers)

            if msg is None:
                await channel.send(embed=discord.Embed(
                    title=f"⏰ Niemand hat geantwortet",
                    description=(
                        f"Die Antwort war **{flag_emoji(code)} {name}**.\n"
                        f"Seit {ROUND_TIMEOUT_MINUTES} Minuten gab es keine richtige Antwort – das Quiz wird beendet."
                    ),
                    color=COLOR_MUTED,
                ))
                break

            took = time.monotonic() - started
            add_xp(gid, msg.author.id, XP_PER_FLAG, correct=1)
            scores[msg.author.id] = scores.get(msg.author.id, 0) + 1
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
            result.set_footer(text=f"Runde {i} von {ROUNDS}")
            await channel.send(embed=result)

            if i < ROUNDS:
                await asyncio.sleep(PAUSE_BETWEEN_ROUNDS)

        # ---- Auswertung + Leaderboard ----
        embeds = []
        if scores:
            ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            top = ranking[0][1]
            winners = [uid for uid, s in ranking if s == top]
            for uid in winners:
                add_xp(gid, uid, XP_QUIZ_WINNER)

            lines = [
                f"{rank_label(n)} <@{uid}>  ·  **{s}** richtig"
                for n, (uid, s) in enumerate(ranking)
            ]
            summary = discord.Embed(title="🏁 Quiz beendet – Ergebnis", description="\n".join(lines), color=COLOR_GOLD)
            summary.add_field(
                name="🏆 Quiz-Sieger",
                value=", ".join(f"<@{u}>" for u in winners) + f"  ·  +{XP_QUIZ_WINNER} Bonus-XP",
                inline=False,
            )
        else:
            summary = discord.Embed(
                title="🏁 Quiz beendet",
                description="Diesmal hat niemand eine Flagge erraten. Beim nächsten Mal klappt's bestimmt!",
                color=COLOR_MUTED,
            )

        next_run = int(time.time() + INTERVAL_MINUTES * 60)
        summary.add_field(
            name="Nächstes Quiz",
            value=f"Frühestens <t:{next_run}:R> – sobald im Kanal etwas los ist",
            inline=False,
        )
        embeds.append(summary)

        board = leaderboard_embed(gid)
        if board:
            embeds.append(board)
        await channel.send(embeds=embeds)


bot = QuizBot()


# ----------------------- Slash-Befehle -----------------------
@bot.tree.command(name="quiz_kanal", description="Legt den Kanal für das Flaggen-Quiz fest")
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
    db.execute(
        "INSERT INTO config (guild_id, channel_id, last_run) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
        (interaction.guild_id, kanal.id, time.time()),
    )
    db.commit()
    await interaction.response.send_message(
        embed=info_embed(
            f"In {kanal.mention} startet automatisch ein neues Quiz, sobald die letzte Runde "
            f"**{INTERVAL_MINUTES} Minuten** her ist und im Kanal etwas los ist.\n"
            f"Mit `/quiz_start` startest du sofort eine Runde.",
            COLOR_SUCCESS, "✅ Kanal gespeichert",
        ),
        ephemeral=True,
    )


@bot.tree.command(name="quiz_start", description="Startet sofort ein Flaggen-Quiz")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_start(interaction: discord.Interaction):
    if interaction.guild_id in bot.running:
        await interaction.response.send_message(
            embed=info_embed("Es läuft bereits ein Quiz.", COLOR_WARN), ephemeral=True
        )
        return
    row = db.execute("SELECT channel_id FROM config WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    channel = (bot.get_channel(row[0]) if row and row[0] else None) or interaction.channel
    db.execute(
        "INSERT INTO config (guild_id, channel_id, last_run) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET last_run=excluded.last_run",
        (interaction.guild_id, channel.id, time.time()),
    )
    db.commit()
    await interaction.response.send_message(
        embed=info_embed(f"Das Quiz startet in {channel.mention}.", COLOR_SUCCESS, "🚀 Quiz gestartet"),
        ephemeral=True,
    )
    asyncio.create_task(bot.run_quiz(channel))


@bot.tree.command(name="quiz_stop", description="Bricht das laufende Flaggen-Quiz ab")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def quiz_stop(interaction: discord.Interaction):
    task = bot.running.get(interaction.guild_id)
    if not task:
        await interaction.response.send_message(
            embed=info_embed("Es läuft gerade kein Quiz.", COLOR_WARN), ephemeral=True
        )
        return
    task.cancel()
    await interaction.response.send_message(
        embed=info_embed("Das Quiz wird abgebrochen …", COLOR_ERROR), ephemeral=True
    )


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


@bot.event
async def on_ready():
    log.info("Eingeloggt als %s (%d Länder im Quiz)", bot.user, len(COUNTRIES))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Bitte DISCORD_TOKEN in .env setzen (siehe .env.example)")
    bot.run(TOKEN, log_handler=None)
