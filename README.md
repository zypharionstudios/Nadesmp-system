# NadeSMP Discord Bot

Python Discord bot for support tickets, OAuth2 membership verification, and welcome messages.

## Features
- \`/setup\`: configure team role, ticket category, ticket log, verification role, optional extra role, and optional role to remove.
- \`/panel-send\`: publish Team Application, Developer Application, Beta Tester Application, Report, and Questions ticket buttons.
- \`/verify-panel-send\`: publish the OAuth2 membership-check panel.
- \`/welcome-system\`: configure a welcome channel and message.
- All slash commands are administrator-only. Normal members can use the public ticket and verification buttons.

## OAuth2 behavior (membership check only)
The user explicitly authorizes Discord OAuth2 with the \`identify\` and \`guilds\` scopes. The callback checks whether the authorized account is already in the configured server. If yes, the bot applies the configured roles. If not, it reports that membership was not found.

This implementation does **not** invite, pull, or add users to the server. It does not request \`guilds.join\` and does not store OAuth access tokens. The OAuth token is used only for the callback's identity and membership requests.

## Setup
1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable the Server Members Intent.
3. Invite the bot with \`bot\` and \`applications.commands\` scopes.
4. Give it View Channels, Send Messages, Embed Links, Manage Channels, Manage Roles, and Read Message History.
5. Put the bot's highest role above every role it must assign or remove.
6. Add the Railway variables described by the project owner; never commit a real \`.env\` file or secrets.
7. Set the Discord OAuth2 redirect URL to \`https://YOUR-RAILWAY-DOMAIN/oauth/callback\`.
8. Mount a Railway Volume at \`/app/data\` and set \`DATABASE_PATH=/app/data/bot.sqlite3\`.

## Discord commands
- \`/setup team_role:... ticket_category:... ticket_log:... verify_role:... extra_role:... remove_role:...\`
- \`/panel-send channel:#support\`
- \`/verify-panel-send channel:#verify\`
- \`/welcome-system channel:#welcome message:Welcome {user} to {server}!\`

Welcome placeholders: \`{user}\`, \`{username}\`, \`{server}\`.

## Notes
- Verification is restricted to server ID \`1539199898430476328\`.
- The user must already be a member of that server to pass the membership check.
- The bot's role hierarchy and permissions must allow role updates.
- Configure a persistent Railway Volume or SQLite settings will be lost when the container is replaced.
