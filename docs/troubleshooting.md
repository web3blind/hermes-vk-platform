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

For video metadata fallback, optionally configure `VK_USER_TOKEN`, but normal text/chat use does not need it.
