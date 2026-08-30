# Troubleshooting

## Quick status checks

```bash
hermes plugins list --enabled
hermes gateway status
grep -i 'VK:' ~/.hermes/logs/gateway.log | tail -80
```

## Required settings

`~/.hermes/.env` must contain:

```dotenv
VK_GROUP_TOKEN=vk1.a.your-community-token
VK_GROUP_ID=123456789
```

And at least one safe access control setting:

```dotenv
VK_ALLOWED_USERS=123456
# or
VK_ALLOWED_PEERS=2000000001
```

## Common problems

### `VK_GROUP_TOKEN and VK_GROUP_ID must be configured`

Set both env vars in `~/.hermes/.env`, then restart the gateway.

### `VK: ignoring unauthorized sender=... peer=...`

The plugin is working and intentionally dropped the message.

Add the exact sender to `VK_ALLOWED_USERS` or the exact peer to `VK_ALLOWED_PEERS`.

### Direct messages work, group chat does not

Check that:

- the VK community bot is actually added to the conversation;
- community settings allow adding the bot to chats;
- the group conversation peer id is allowlisted;
- you used `2000000000 + chat_id`, not just the visible chat id.

### Gateway started before `.env` changes

Restart:

```bash
hermes gateway restart
```

### Long Poll does not receive events

In VK community settings, check:

- Long Poll API is enabled;
- API version is supported;
- `message_new` event is enabled;
- community messages are enabled.

### Media does not arrive as expected

Some VK attachments expose only player/watch-page metadata. The plugin does not pretend those pages are direct files.

For video metadata fallback, configure an explicit gateway `VK_USER_TOKEN`; normal text/chat use does not need it. Do not copy tokens from publishing workflows. Use the gateway lifecycle helper instead.

Important constraints:

- Other installs should create their own VK app and provide its app id/protected key via `VK_GATEWAY_APP_ID` and `VK_GATEWAY_CLIENT_SECRET_FILE` or equivalent CLI flags.
- If the user is already authorized in a local VK browser session, opening the helper's OAuth URL may return a code quickly. If not, the user must log in/approve in VK; the helper cannot silently refresh a user token from only a community token.
- Without VK `offline` access, the token is temporary and `token-status` must be checked before relying on video enrichment.

```bash
python3 scripts/vk_gateway_user_token.py auth-url --scope 8212
python3 scripts/vk_gateway_user_token.py exchange-code --code '<code_from_oauth_blank_page>'
python3 scripts/vk_gateway_user_token.py token-status
```
