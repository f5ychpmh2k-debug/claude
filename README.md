# Anti-Raid Discord Bot

Protects **your own server** from join floods and spam.

- **Join-flood detection**: N joins within T seconds triggers automatic lockdown; new joiners are kicked or timed out until it expires.
- **Spam / mass-mention detection**: offenders are timed out and their recent messages purged.
- Alerts go to a log channel. Moderators (Manage Messages) and whitelisted roles are exempt from spam checks.

## Setup
1. Create an app at https://discord.com/developers/applications, add a bot, and enable the **Server Members Intent**.
2. Invite with scopes `bot applications.commands` and permissions: Kick Members, Moderate Members, Manage Messages, View Channels, Send Messages.
3. Put the bot's role above the roles it should moderate.
4. ```
   pip install -r requirements.txt
   cp .env.example .env   # add your token
   python bot.py
   ```

## Commands (admin only)
`/status`, `/lockdown`, `/unlock`, `/logchannel`, `/config <key> <value>`, `/whitelist <role>`
