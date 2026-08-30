# Hermes VK Platform Plugin

VK Messenger / VK community bot platform adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

This plugin lets Hermes receive messages from VK community messages via **VK Group Long Poll** and reply through the VK `messages.send` API.

## Features

- Inbound VK `message_new` and `message_edit` events through Group Long Poll.
- Outbound replies with `messages.send`.
- Direct messages and VK group conversations (`peer_id = 2000000000 + chat_id`).
- Safe allowlist controls for VK users, peers, and peer+user policies.
- TTL dedupe for repeated Long Poll `message_new` events after reconnect.
- Optional cron/home-channel delivery through `VK_HOME_CHANNEL`.
- Message editing for Hermes tool-progress bubbles through `messages.edit`.
- Inbound media handling for photos, documents, voice messages, audio, video messages, and direct video files with bounded downloads.
- Outbound media upload for image files, documents, voice files, and videos.
- If VK `video.save` rejects an MP4 with auth/permission errors under group-token auth, video delivery falls back to sending the MP4 as a document attachment.
- Markdown-style Hermes output is converted to VK-readable plain text because the regular VK `messages.send` API has no Telegram-like `parse_mode` for Markdown/HTML.
- Optional per-channel prompts and skill bindings through Hermes config.
- Optional Telegram-style message-reaction acks (`VK_REACTIONS_ENABLED`): a progress reaction while the agent works, then 👍/👎 on completion.
- No mandatory third-party Python dependency; the adapter uses Python stdlib for VK HTTP calls.

## Documentation

- [`docs/development.md`](docs/development.md) — plugin structure, runtime flow, testing, and release checks.
- [`docs/project-lanes.md`](docs/project-lanes.md) — virtual project lanes, pinning, sorting, importer, cron lane routing, and accessibility rules.
- [`docs/update-guide.md`](docs/update-guide.md) — agent-facing checklist for updating the plugin against a new Hermes version.
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — common setup and runtime failures.

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
3. Enable bot capabilities for conversations:
   - allow messages from users;
   - allow adding the community bot to chats/conversations;
   - enable the option usually named like **chat bot**, **bot can work in conversations**, or **messages in conversations**.
   - In VK's Russian UI this is often under
     **Управление сообществом → Сообщения → Настройки для бота → Возможности ботов**:
     set **Возможности ботов** to enabled and enable **Разрешать добавлять сообщество в чаты**.
4. Enable Long Poll API for the community.
5. Enable the `message_new` and `message_edit` event types.
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

# Optional explicit gateway user token for video metadata fallback.
# Do not copy tokens from publishing workflows. Use scripts/vk_gateway_user_token.py
# to create/refresh this via server-side OAuth code exchange.
VK_USER_TOKEN=
```

Gateway user tokens are short-lived unless VK grants offline access. Manage them with:

```bash
# Print an OAuth URL for the user to open; the returned code is exchanged server-side.
python3 scripts/vk_gateway_user_token.py auth-url --scope 8212

# Exchange the OAuth code, store ~/.hermes/secrets/vk_gateway_user_token,
# update ~/.hermes/.env as VK_USER_TOKEN, and live-validate with users.get.
python3 scripts/vk_gateway_user_token.py exchange-code --code '<code_from_oauth_blank_page>'

# Before relying on video.get/media enrichment, validate freshness.
python3 scripts/vk_gateway_user_token.py token-status
```

### Message reactions (👌 → 👍/👎)

Optional Telegram-style processing acks. When enabled, the bot reacts to the
incoming message with a "seen, working on it" reaction as soon as processing
starts, and swaps it for a final 👍 (success) or 👎 (failure) reaction when the
agent finishes. Reactions are cosmetic: API failures never break message flow.

```dotenv
VK_REACTIONS_ENABLED=true
```

VK identifies reactions by numeric id, not emoji. The default community set:
`1`=❤️ `2`=🔥 `3`=😂 `4`=👍 `8`=😡 `10`=👌 `16`=🎉. Defaults: progress `10`
(👌 — there is no 👀 in the VK default set), OK `4` (👍), FAIL `8`. If your
community rearranged reactions, override any step:

```dotenv
# Defaults shown:
VK_REACTION_PROGRESS=10
VK_REACTION_OK=4
VK_REACTION_FAIL=8
```

Set a step to `0` to disable it (e.g. `VK_REACTION_PROGRESS=0` to only mark
final outcomes). On cancelled/interrupted turns the progress reaction is
removed. Requires the community token to have the messages permission; some
very old API versions do not expose `messages.deleteReaction` — the adapter
probes support lazily and degrades gracefully.

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

### Optional VK project lanes

VK community bots do not have Telegram-style topics or bot-managed Messenger folders. If one physical VK chat should host several Hermes project contexts, configure virtual project lanes as a VK plugin feature:

```yaml
platforms:
  vk:
    extra:
      project_lanes:
        enabled: true
        chats:
          "2000000001":
            base_folder: ai-projects
            default_skills:
              - coding
            lanes:
              - id: rialo
                name: Rialo
                description: ретродроп и аналитика проекта Rialo
                folder: rialo
                skills:
                  - blockchain-project-research
                  - coding
                aliases:
                  - rialo
```

The legacy `vk.project_lanes` shape is still read for backward compatibility, but new automatic writes and imports use `platforms.vk.extra.project_lanes`.

Behavior:

- one VK `peer_id` remains the physical chat container;
- each lane is routed as a Hermes synthetic thread with `thread_id=lane:<id>`;
- active lane selection is stored per `peer_id + user_id`;
- the lane transcript is shared inside the synthetic thread, like Telegram topics / Discord threads;
- `/new` while a lane is active resets only that lane session;
- `/project search <query>` searches configured and chat-created lanes;
- `/project new <name> <skills_csv> <context...>` creates a lane immediately when the id is safe and persists it to the approved VK project-lane config path;
- `/project new` and `Новый проект` create parseable labeled/free-form lanes immediately, or fall back to a normal Hermes agent turn for ambiguous input;
- `/project edit <id> <what to change...>` edits an existing lane immediately when the requested field changes can be parsed safely and persists the updated lane;
- `/project edit` starts the same lightweight guided flow for the active lane: parseable field changes are applied immediately, ambiguous text falls back to a normal Hermes agent turn with edit context;
- `/project pin` and `/project unpin` pin or unpin the active lane for that VK chat; pinned lanes stay above session-recency sorting;
- `/invite` returns the current VK chat invite link from the bot using `messages.getInviteLink(peer_id=<current>, reset=0)`;
- `Команды` / `/commands` shows a screen-reader-friendly text list of VK chat commands with clickable text-command buttons;
- `/project off` returns that user to the root VK chat context;
- `VK_HOME_CHANNEL` is not changed by project lanes.

Commands and buttons:

```text
/project                  show current project/menu
/project list             show project list
/project search <query>   search configured and chat-created lanes
/project off              disable active lane for you in this VK chat
/project <alias-or-id>    switch active lane
@alias <text>             send one message to a lane without switching
/project new              start conversational project creation
/project new <name> <skills_csv> <context...>
                          fast fallback creation draft
/project edit             start editing the active lane
/project edit <id> <what to change...>
                          fast fallback edit of an existing lane
/project pin              pin active project first in this VK chat
/project unpin            unpin active project
/invite                   show the current VK chat invite link
/commands                 show all VK chat commands
```

Project-list page navigation uses visible `Предыдущая` / `Следующая` text buttons. The hidden payload still carries the exact fallback command such as `/project list 2`; if VK sends only the visible label, the adapter falls back to saved per-user page state and moves relative to the last shown list page. Lists are ordered as pinned lanes first, then lanes with the newest Hermes synthetic-thread session activity, then lanes without activity in stable config/import order.

The `Команды` response includes an inline command keyboard with text buttons for copy-hostile commands such as `/project list`, `/project new`, `/project edit`, `/project off`, and `/invite`. These buttons deliberately use `text` actions so screen readers expose the command label and VK sends the same text a user could type manually.

VK keyboard actions useful for this plugin:

- `text` — sends a visible button label as message text and may also carry payload. This is the preferred accessible/default action for project controls.
- `callback` — sends a callback event without normal message text. Avoid for core navigation in VK desktop/browser because it can be exposed unreliably or remain stuck as loading.
- `open_link` — opens a URL, useful for external resources, not for chat commands.
- platform-specific actions such as location/app/payment are not appropriate for the project-lane command menu.

Example fallback draft command:

```text
/project new Rialo blockchain-project-research,coding ретродроп и аналитика проекта Rialo
```

`/project new` without arguments and the `Новый проект` button start a normal Hermes agent-mediated creation flow. The VK adapter only records short-lived pending state and injects a strict project-creation instruction into the next ordinary Hermes turn; it does not directly call a model inside the VK callback path.

Configured/imported lanes live in `platforms.vk.extra.project_lanes`. Runtime UI state such as active lane, pinned lanes, pending create/edit prompts, editable list-message ids, and delivered-message lane anchors remains in `~/.hermes/vk_project_lanes_state.json`. When a user creates or edits a lane from VK and the field changes are deterministic, the adapter writes the lane to the canonical config path automatically, similar to Hermes' topic/thread persistence model, so users do not need to remember a separate export step. If config writing fails, the adapter keeps a state overlay as a fail-safe and logs a warning.

Cron jobs can target a VK project lane with the explicit target form `vk:<peer_id>:lane:<lane_id>` or by using `deliver=origin` when the job origin is a VK synthetic thread. Cron-delivered VK messages are remembered by message id, so replying to the cron message routes into that lane even if the user's currently active project in the same physical VK chat is different.

To import existing mappings into a VK chat:

```bash
python plugins/platforms/vk/setup_helper.py lanes import --to-peer 2000000001 --from legacy,telegram,discord --telegram-chat-id -1001234567890 --dry-run
python plugins/platforms/vk/setup_helper.py lanes import --to-peer 2000000001 --from legacy,telegram,discord --telegram-chat-id -1001234567890
```

The importer reads Telegram topics from `platforms.telegram.extra.group_topics` and `dm_topics` (optionally filtered by `--telegram-chat-id`), legacy VK lanes from `vk.project_lanes`, and Discord thread mapping sections when present. It writes `platforms.vk.extra.project_lanes.enabled: true` and upserts lanes under `chats.<peer>.lanes`.

For authorized VK chats and DMs, normal adapter replies also attach the persistent command keyboard. This keeps `Проекты / Новый проект / Команды` visible even before the first project exists and after ordinary gateway messages such as `/stop` responses.

Project lists are sorted by latest Hermes thread-session activity for that VK peer. The most recently used project appears first; lanes without sessions stay below in config order.

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

`VK_HOME_CHANNEL` is only the default peer for home/cron/notification delivery.
It is not the per-chat conversation context, and it should not be moved when a
new thematic VK chat is created unless the operator explicitly says to make that
new peer the default Home channel.

When creating a new thematic chat such as "content", "style", or "research":

1. Create or choose the new VK conversation.
2. Add its peer id to `VK_ALLOWED_PEERS` or the relevant peer allowlist.
3. Add any matching `vk.channel_prompts` / `vk.channel_skill_bindings` for that
   peer if a special context is needed.
4. Restart the gateway.
5. Send a test message in the new chat.

Do **not** rewrite `VK_HOME_CHANNEL` as part of that flow unless the user asks to
move Home/default delivery. Keep the existing Home peer stable.

If an agent is asked to "create a content chat", it should confirm whether the
request means:

- create/configure a separate allowed VK peer for content work; or
- move the default Home channel to that peer.

The safe default is the first option: create/configure a separate chat and leave
Home unchanged.

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

### `VK API error 912: This is a chat bot feature`

This usually means the VK community exists and the bot may even be present in
the chat, but the community is not yet allowed to work as a chat bot in group
conversations.

Open the VK community settings and enable the conversation bot settings. VK's UI
labels vary, but look for options like:

- **Messages** / community messages;
- **Bot capabilities** / **Chat bot**;
- **Bot can work in conversations**;
- **Allow adding the community to chats/conversations**;
- **Long Poll API** with `message_new` enabled.

In VK's Russian UI, the exact path is commonly:

```text
Управление сообществом
→ Сообщения
→ Настройки для бота
→ Возможности ботов
```

There enable:

- **Возможности ботов**;
- **Разрешать добавлять сообщество в чаты**.

If you are using Hermes with browser access to a logged-in VK admin account,
Hermes can help navigate this UI and turn the setting on after you explicitly
approve that account action. The plugin installer itself does not silently
change VK community settings.

After changing VK settings, restart Hermes and test the peer again:

```bash
hermes gateway restart
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


VK project lanes accessibility note: `/project list` must include project names and `/project <id>` fallback as plain text, not only VK keyboard buttons. Project selection buttons use VK `text` actions with visible labels, not callback-only actions; some VK desktop/browser clients hide or poorly expose callback/inline keyboards from the accessible tree.
