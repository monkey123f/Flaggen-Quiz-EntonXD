# Flaggen-Quiz Discord Bot

Alle 12 Stunden startet automatisch ein Quiz mit 5 Flaggen. Wer eine Flagge als Erstes errät, bekommt XP.
Wer am Ende die meisten richtigen hat, bekommt Bonus-XP. Dazu gibt es ein XP-Leaderboard.

## Einrichtung

1. Auf https://discord.com/developers/applications eine Application erstellen → **Bot** → Token kopieren.
2. Beim Bot unter **Privileged Gateway Intents** die **Message Content Intent** aktivieren.
3. Bot einladen (OAuth2 → URL Generator): Scopes `bot` + `applications.commands`,
   Rechte: Nachrichten lesen/senden, Embeds, Reaktionen hinzufügen.
4. Projekt vorbereiten:

```bash
pip install -r requirements.txt
cp .env.example .env      # Token eintragen
python bot.py
```

## Befehle

| Befehl | Wer | Beschreibung |
|---|---|---|
| `/quiz_kanal #kanal` | Admins | Kanal festlegen, in dem alle 12 h das Quiz läuft |
| `/quiz_start` | Admins | Sofort ein Quiz starten |
| `/leaderboard` | alle | Top 10 nach XP |
| `/rang [nutzer]` | alle | Eigene XP und Platz |

## Anpassen

Oben in `bot.py`: `ROUNDS`, `INTERVAL_HOURS`, `ROUND_SECONDS`.
XP-Werte per `.env` (`XP_PER_FLAG`, `XP_QUIZ_WINNER`).
Weitere Länder in `flags.py` ergänzen (ISO-Code, deutscher Name, Alternativschreibweisen).
Flaggenbilder kommen von flagcdn.com. Daten liegen in `quiz.db` (SQLite).
