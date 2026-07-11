"""User-facing message catalog: translation and per-string overrides.

The design is gettext-style: the ENGLISH template is the key. Code stays
readable (`self._t("Nothing to undo — you have no posts.")`), unknown keys
fall back to themselves, and adding a language means adding one dict here
— no code changes anywhere else.

Plurals use template PAIRS chosen by the caller
(`"{n} posts" if n != 1 else "{n} post"`), because German plurals do not
follow the English +s rule.

Operators can override single strings via `bbs.strings` in config.yaml,
keyed by the English template — for rewording, not just translation.
"""

import logging
import string

_LOGGER = logging.getLogger(__name__)

# English template -> German template. Placeholders must match exactly
# (enforced by a unit test).
DE: dict[str, str] = {
    # generic / routing
    "Send !help for a list of commands.": "Sende !help für eine Befehlsliste.",
    "Unknown command '!{cmd}'. Send !help.": "Unbekannter Befehl '!{cmd}'. Sende !help.",
    # help
    "Commands:": "Befehle:",
    "!rooms — list rooms": "!rooms — Räume anzeigen",
    "!join <room> — enter a room": "!join <room> — Raum betreten",
    "!leave — leave current room": "!leave — aktuellen Raum verlassen",
    "!post <text> — post to current room": "!post <text> — in den Raum schreiben",
    "!read (n) — read new posts": "!read (n) — neue Beiträge lesen",
    "!search <text> — search posts": "!search <text> — Beiträge durchsuchen",
    "!undo — remove your last post": "!undo — letzten Beitrag entfernen",
    "!msg [name] <text> — private message": "!msg [name] <text> — private Nachricht",
    "!inbox — read private messages": "!inbox — private Nachrichten lesen",
    "!reply <text> — answer your last inbox message": "!reply <text> — letzte Inbox-Nachricht beantworten",
    "!who — members of current room": "!who — Mitglieder des Raums",
    "!users — recent users": "!users — aktive Nutzer",
    "!seen <name> — last activity of a user": "!seen <name> — letzte Aktivität eines Nutzers",
    "!whoami — your name": "!whoami — dein Name",
    "!whereami or !pwd — current room": "!whereami oder !pwd — aktueller Raum",
    "!stats — user and post counts": "!stats — Nutzer- und Beitragszahlen",
    "!weather (location) — current weather": "!weather (ort) — aktuelles Wetter",
    "!ping — signal quality": "!ping — Signalqualität",
    # rooms
    "No rooms available.": "Keine Räume vorhanden.",
    "Rooms:": "Räume:",
    "{n} member": "{n} Mitglied",
    "{n} members": "{n} Mitglieder",
    "{room} ({members}, {ago} ago)": "{room} ({members}, vor {ago})",
    "Usage: !join <room>": "Nutzung: !join <room>",
    "Room '{room}' does not exist. Send !rooms.": "Raum '{room}' existiert nicht. Sende !rooms.",
    "Joined '{room}'. !read for new posts, !post <text> to write.":
        "'{room}' betreten. !read für neue Beiträge, !post <text> zum Schreiben.",
    "You are not in a room.": "Du bist in keinem Raum.",
    "Left '{room}'.": "'{room}' verlassen.",
    # posting / reading
    "Usage: !post <text>": "Nutzung: !post <text>",
    "Join a room first: !join <room>": "Betritt zuerst einen Raum: !join <room>",
    "Posted to '{room}'.": "In '{room}' veröffentlicht.",
    "Usage: !read or !read <number>": "Nutzung: !read oder !read <zahl>",
    "No new posts in '{room}'.": "Keine neuen Beiträge in '{room}'.",
    "+{remaining} more — send !read again": "+{remaining} weitere — sende erneut !read",
    # search
    "Usage: !search <text>": "Nutzung: !search <text>",
    "Search term too short — use at least 2 characters.":
        "Suchbegriff zu kurz — nutze mindestens 2 Zeichen.",
    "No posts matching '{term}' in '{room}'.": "Keine Beiträge zu '{term}' in '{room}'.",
    "+{remaining} more — refine your search": "+{remaining} weitere — verfeinere deine Suche",
    # undo
    "Nothing to undo — you have no posts.": "Nichts rückgängig zu machen — du hast keine Beiträge.",
    "Too late — !undo works within {minutes}m of posting.":
        "Zu spät — !undo geht nur bis {minutes}m nach dem Posten.",
    "Removed your post in '{room}': {snippet}": "Dein Beitrag in '{room}' wurde entfernt: {snippet}",
    # private messages
    "Usage: !msg [name] <text>": "Nutzung: !msg [name] <text>",
    "Usage: !msg [name] <text>  (missing message text?)":
        "Nutzung: !msg [name] <text>  (Nachrichtentext fehlt?)",
    "Usage: !msg [name] <text>  (brackets required if the name has spaces)":
        "Nutzung: !msg [name] <text>  (Klammern nötig bei Namen mit Leerzeichen)",
    "Usage: !msg [name] <text>  (check the [ ] brackets and message text)":
        "Nutzung: !msg [name] <text>  (prüfe die [ ]-Klammern und den Text)",
    "You cannot send a message to yourself.": "Du kannst dir nicht selbst schreiben.",
    "Message queued for {name}.": "Nachricht für {name} eingereiht.",
    "No user '{token}' known. Try !users.": "Kein Nutzer '{token}' bekannt. Versuche !users.",
    "'{token}' is ambiguous — pick a key prefix:": "'{token}' ist mehrdeutig — nutze ein Key-Präfix:",
    "Send: !msg <keyprefix> <text>": "Sende: !msg <keypräfix> <text>",
    "Usage: !reply <text>": "Nutzung: !reply <text>",
    "No one to reply to yet — read your !inbox first.":
        "Noch niemand zum Antworten — lies zuerst deine !inbox.",
    "That user is no longer known to the BBS.": "Dieser Nutzer ist der BBS nicht mehr bekannt.",
    "No new messages.": "Keine neuen Nachrichten.",
    # who / users / whoami / whereami / stats
    "You are not in a room. Use !join <room>.": "Du bist in keinem Raum. Nutze !join <room>.",
    "No members in '{room}'.": "Keine Mitglieder in '{room}'.",
    "'{room}' members:": "Mitglieder von '{room}':",
    "No other users known yet.": "Noch keine anderen Nutzer bekannt.",
    "Recent users:": "Aktive Nutzer:",
    "Usage: !seen <name>": "Nutzung: !seen <name>",
    "[{name}] was last active {ago} ago.": "[{name}] war zuletzt vor {ago} aktiv.",
    "Send: !seen <keyprefix>": "Sende: !seen <keypräfix>",
    "You are known as [{name}].": "Du bist bekannt als [{name}].",
    "You are not in any room. Use !join <room>.": "Du bist in keinem Raum. Nutze !join <room>.",
    "You are in room '{room}'.": "Du bist im Raum '{room}'.",
    " {n} unread post.": " {n} ungelesener Beitrag.",
    " {n} unread posts.": " {n} ungelesene Beiträge.",
    " No unread posts.": " Keine ungelesenen Beiträge.",
    "Stats: {users}, {posts}, {rooms}.": "Statistik: {users}, {posts}, {rooms}.",
    "{n} user": "{n} Nutzer",
    "{n} users": "{n} Nutzer",
    "{n} post": "{n} Beitrag",
    "{n} posts": "{n} Beiträge",
    "{n} room": "{n} Raum",
    "{n} rooms": "{n} Räume",
    # admin / optional commands
    "Restart not available.": "Neustart nicht verfügbar.",
    "Restarting...": "Starte neu...",
    "No signal data available.": "Keine Signaldaten verfügbar.",
    "direct": "direkt",
    "Advert not available.": "Advert nicht verfügbar.",
    "Advert sent.": "Advert gesendet.",
    "Channel advert not configured.": "Kanal-Advert nicht konfiguriert.",
    "Channel advert sent.": "Kanal-Advert gesendet.",
    "Usage: !weather <location>": "Nutzung: !weather <ort>",
    "Weather is not configured.": "Wetter ist nicht konfiguriert.",
    "Weather unavailable for '{location}'.": "Wetter für '{location}' nicht verfügbar.",
    # sent by bbs.py
    "You were removed from '{room}' after {minutes}m inactivity. Send !join {room} to rejoin.":
        "Du wurdest nach {minutes}m Inaktivität aus '{room}' entfernt. Sende !join {room} zum Wiederbeitritt.",
    "You have {n} new message in your inbox. Send !inbox.":
        "Du hast {n} neue Nachricht in deiner Inbox. Sende !inbox.",
    "You have {n} new messages in your inbox. Send !inbox.":
        "Du hast {n} neue Nachrichten in deiner Inbox. Sende !inbox.",
}

_CATALOGS: dict[str, dict[str, str]] = {"en": {}, "de": DE}

SUPPORTED_LANGUAGES = tuple(_CATALOGS)


def placeholders(template: str) -> set[str]:
    """The set of {field} names in a format template."""
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


class Messages:
    """Resolve a template through overrides -> language catalog -> itself,
    then apply the placeholders. A broken override or translation falls
    back to the English template instead of crashing a command handler."""

    def __init__(self, language: str = "en", overrides: dict[str, str] | None = None) -> None:
        self._catalog = _CATALOGS.get(language, {})
        self._overrides = overrides or {}

    def t(self, template: str, **kwargs: object) -> str:
        chosen = self._overrides.get(template) or self._catalog.get(template, template)
        try:
            return chosen.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            _LOGGER.warning(
                f"Broken placeholders in translated string {chosen!r} — using the English original."
            )
            return template.format(**kwargs)
