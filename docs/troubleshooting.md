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
- `message_new` is enabled for incoming messages;
- `message_edit` is enabled for edits;
- **`message_event` is enabled for callback buttons**;
- community messages are enabled.

### Callback buttons keep loading; Allow Once / Deny does nothing

VK renders keyboards even when their Long Poll event subscription is disabled.
Open community settings → API usage → Long Poll API → Event types and enable
**`message_event`**. Receiving ordinary messages (`message_new`) is not enough.
The message-history fallback only recovers text messages, not callback clicks.

For an API-based diagnosis, inspect `groups.getLongPollSettings` with the community
token without printing it. Expect `is_enabled=true` and `events.message_event=1`.
An authorized operator can enable only `message_event=1` through
`groups.setLongPollSettings`; preserve all other settings and read them back.
The plugin diagnoses missing subscriptions but never changes community settings
silently at startup.

Check fresh gateway log entries for `types=message_event` after a click. If the
event arrives but the prompt is expired, use a new request; do not reuse an old
button after a gateway restart. If a callback is unavailable, reply to the
original approval prompt with `/approve` or `/deny` in its original project.
For slash confirmations use `/approve`, `/always`, or `/cancel`. Text approvals
resolve the oldest pending operation in that project; review it before approving.

Access policies still apply to buttons. An allowlisted peer permits its allowed
participants, not just the person who started a shared lane. Use `user_only`,
`peer_and_user`, or per-peer user allowlists when approvals must be owner-only.

### Media does not arrive as expected

Some VK attachments expose only player/watch-page metadata. The plugin does not pretend those pages are direct files.

For video metadata fallback, optionally configure `VK_USER_TOKEN`, but normal text/chat use does not need it.
