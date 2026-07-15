# Hermes VK Platform Plugin

VK Messenger / VK community bot platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

This plugin lets Hermes receive messages from VK community messages via **VK Group Long Poll** and reply through the VK `messages.send` API.

## Features

- Inbound VK `message_new` events through Group Long Poll.
- Outbound replies with `messages.send`.
- Direct messages and VK group conversations (`peer_id = 2000000000 + chat_id`).
- Safe allowlist controls for VK users, peers, and peer+user policies.
- TTL dedupe for repeated Long Poll `message_new` events after reconnect.
- Optional cron/home-channel delivery through `VK_HOME_CHANNEL`.
- Message editing for Hermes tool-progress bubbles through `messages.edit`.
- Inbound media handling for photos, documents, voice messages, audio, video messages, and direct video files with bounded downloads.
- Outbound media upload for image files, documents, voice files, and videos.
- Optional per-channel prompts and skill bindings through Hermes config.
- No mandatory third-party Python dependency; the adapter uses Python stdlib for VK HTTP calls.

## Requirements

- Hermes Agent with plugin support.
- A VK community/group.
- Community messages enabled in VK.
- VK Group Long Poll API enabled for `message_new` events.
- A VK community access token with `messages` permission.

## Install

From GitHub:

```bash
hermes plugins install web3blind/hermes-vk-platform --enable
```

Or with a full URL:

```bash
hermes plugins install https://github.com/web3blind/hermes-vk-platform.git --enable
```

Then configure and restart the gateway:

```bash
hermes gateway setup
hermes gateway restart
```

If you prefer manual setup, edit `~/.hermes/.env` as shown below.

## VK setup checklist

1. Create or choose a VK community.
2. Open community settings and enable community messages.
3. Enable bot capabilities / allow messages from users.
4. Enable Long Poll API for the community.
5. Enable the `message_new` event type.
6. Create a community token with `messages` permission.
7. If using group conversations, allow adding the community bot to chats and add it to the target chat.
8. Configure allowlists before starting the gateway.

VK documentation entry points:

- Community tokens: <https://dev.vk.com/en/api/access-token/community-token>
- Bots Long Poll API: <https://dev.vk.com/en/api/bots-long-poll/getting-started>
- Messages API: <https://dev.vk.com/en/method/messages.send>

## Configuration

### Required environment variables

Store these in `~/.hermes/.env`:

```dotenv
VK_GROUP_TOKEN=vk1.a.your-community-token
VK_GROUP_ID=123456789
```

`VK_GROUP_ID` is the numeric community id without a leading minus sign.

### Recommended allowlist

Do **not** run a personal Hermes agent open to all VK users. Configure at least one allowlist:

```dotenv
# Allow specific VK user ids:
VK_ALLOWED_USERS=123456,789012

# And/or allow specific VK peers/chats:
VK_ALLOWED_PEERS=123456,2000000001
```

By default the access policy is backward-compatible:

```text
allowed user OR allowed peer
```

That means `VK_ALLOWED_PEERS=2000000001` allows **every participant of that VK chat** to talk to Hermes. This can be useful for a small private chat whose membership you fully control, but it is risky for larger or public group conversations.

For stricter access, choose an explicit policy:

```dotenv
# Backward-compatible default: allowed user OR allowed peer.
VK_ACCESS_POLICY=any

# Only users from VK_ALLOWED_USERS may talk to Hermes.
VK_ACCESS_POLICY=user_only

# Only peers/chats from VK_ALLOWED_PEERS may talk to Hermes.
# Warning: in group chats, this allows every participant of the chat.
VK_ACCESS_POLICY=peer_only

# Sender must be in VK_ALLOWED_USERS AND chat must be in VK_ALLOWED_PEERS.
VK_ACCESS_POLICY=peer_and_user
```

You can also restrict particular chats to particular users:

```dotenv
# Format: peer:user|user;peer:user
VK_ALLOWED_USERS_BY_PEER=2000000001:123456|789012;2000000002:123456
```

`VK_ALLOWED_USERS_BY_PEER` takes precedence for the listed peer. If a peer is listed there, only the listed users are accepted in that peer.

For VK group conversations, peer id is:

```text
peer_id = 2000000000 + chat_id
```

Example: VK chat id `1` becomes peer id `2000000001`.

### Unsafe testing mode

Only for isolated test communities:

```dotenv
VK_ALLOW_ALL_USERS=true
```

Do not use this for a personal Hermes instance with tools enabled.

### Optional settings

```dotenv
# Default VK peer for cron/home delivery:
VK_HOME_CHANNEL=2000000001

# Download all inbound attachments to Hermes cache, not only file-like direct URLs:
VK_DOWNLOAD_ATTACHMENTS=false

# Maximum inbound attachment download size in bytes. Default: 25 MiB.
VK_MAX_ATTACHMENT_BYTES=26214400

# In-memory duplicate event TTL in seconds. Default: 1800.
VK_DEDUPE_TTL_SECONDS=1800

# Optional user token for video metadata fallback. Not needed for normal chat usage.
VK_USER_TOKEN=
```

## Optional YAML config

The plugin can also bridge a top-level `vk:` block from `~/.hermes/config.yaml` into the platform config.

```yaml
vk:
  group_id: "123456789"
  allowed_users:
    - "123456"
  allowed_peers:
    - "2000000001"
  access_policy: "peer_and_user"
  allowed_users_by_peer:
    "2000000001":
      - "123456"
  max_attachment_bytes: 26214400
  dedupe_ttl_seconds: 1800
  home_channel: "2000000001"
  channel_prompts:
    "2000000001": "This is a trusted VK chat. Keep replies concise."
  channel_skill_bindings:
    - id: "2000000001"
      skills:
        - research
```

Secrets such as `group_token` are better stored in `.env`, not config YAML.

## Usage

Start or restart the Hermes gateway after configuration:

```bash
hermes gateway restart
```

Then send a message to the VK community or an allowed VK chat.

Hermes will create sessions keyed by VK platform, chat type, peer id, and sender id. This keeps different VK chats isolated.

## Cron/home delivery

If `VK_HOME_CHANNEL` is set, cron jobs and messaging deliveries can target VK by platform name where Hermes supports plugin platform delivery.

```dotenv
VK_HOME_CHANNEL=2000000001
```

## Troubleshooting

### Hermes does not answer

Check:

1. Gateway is running: `hermes gateway status`.
2. Plugin is enabled: `hermes plugins list --enabled`.
3. `VK_GROUP_TOKEN` and `VK_GROUP_ID` are set in `~/.hermes/.env`.
4. VK Long Poll is enabled and `message_new` events are selected.
5. The sender or peer is allowlisted.
6. Gateway was restarted after changing `.env`.

Logs:

```bash
grep -i 'VK:' ~/.hermes/logs/gateway.log | tail -80
```

### Unauthorized sender in logs

A log line like this means the adapter deliberately ignored the message:

```text
VK: ignoring unauthorized sender=<user_id> peer=<peer_id>
```

Add the exact user id to `VK_ALLOWED_USERS` or the exact peer id to `VK_ALLOWED_PEERS`, then restart the gateway. If `VK_ACCESS_POLICY=peer_and_user`, both must match. If the peer is listed in `VK_ALLOWED_USERS_BY_PEER`, the sender must be listed for that peer.

### Group chat peer id confusion

VK group conversation peer ids start at `2000000000`.

If VK chat id is `42`, use:

```text
VK_ALLOWED_PEERS=2000000042
```

### Tool progress creates multiple messages or does not update

This plugin implements `edit_message()` via VK `messages.edit`. If progress editing fails, check that VK returned an editable `conversation_message_id` for sent messages and inspect gateway logs for VK API errors.

### Long messages

VK messages are chunked according to the adapter max message length. The default is `4096` characters.

## Security model

- The adapter enforces its own allowlist before passing messages to Hermes.
- `VK_ALLOWED_PEERS` permits every participant in the listed group chat unless you use `VK_ACCESS_POLICY=peer_and_user` or `VK_ALLOWED_USERS_BY_PEER`.
- Repeated Long Poll events are deduplicated in memory by `peer_id + conversation_message_id` for `VK_DEDUPE_TTL_SECONDS`.
- Inbound attachment downloads are streamed to cache and capped by `VK_MAX_ATTACHMENT_BYTES`.
- Tokens are redacted from adapter errors.
- `VK_ALLOW_ALL_USERS=true` is intentionally treated as unsafe.
- Remote image URLs are sent as URLs instead of silently downloading arbitrary remote image data for outbound sends.
- Inbound downloadable attachments are stored under the Hermes cache directory for gateway media processing.

## Development

See [`docs/development.md`](docs/development.md).

## License

MIT. See [`LICENSE`](LICENSE).
