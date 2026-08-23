# VK Project Lanes

This document explains the VK Project Lanes feature for maintainers and coding agents.

VK does not provide Telegram-forum-topic-like bot-managed topics for group chats. Project Lanes emulate topics inside one physical VK peer by routing messages into Hermes synthetic thread sessions.

## Core idea

One VK peer remains the physical container:

```text
peer_id = 2000000001
```

Each project lane becomes a Hermes synthetic thread:

```text
chat_type = thread
thread_id = lane:<lane_id>
```

The resulting Hermes session key is expected to follow the normal gateway thread shape:

```text
agent:main:vk:thread:<peer_id>:lane:<lane_id>
```

Do not add special core session semantics for VK lanes. The adapter should create normal `MessageEvent` objects with a `SessionSource` that already contains the synthetic thread fields.

## Config shape

Canonical config lives under the plugin platform config:

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
              - id: example-project
                name: Example Project
                description: Short human-readable description
                folder: example-project
                skills:
                  - coding
                aliases:
                  - example
```

The legacy shape is still readable for compatibility:

```yaml
vk:
  project_lanes:
    "2000000001":
      lanes:
        - id: example-project
          name: Example Project
```

New automatic writes and imports should use the canonical `platforms.vk.extra.project_lanes` path.

## Runtime state

Runtime UI state is stored in the Hermes home file:

```text
~/.hermes/vk_project_lanes_state.json
```

Current keys:

```json
{
  "active": {},
  "pending_create": {},
  "pending_edit": {},
  "custom_lanes": {},
  "project_list_messages": {},
  "project_list_pages": {},
  "message_lanes": {},
  "pinned": {}
}
```

Meaning:

- `active`: active lane per `peer_id:user_id`.
- `pending_create`: short-lived guided `/project new` state.
- `pending_edit`: short-lived guided `/project edit` state.
- `custom_lanes`: fail-safe overlay if config write fails.
- `project_list_messages`: last editable project-list message per `peer_id:user_id`.
- `project_list_pages`: current page for text-only `Следующая` / `Предыдущая` fallback.
- `message_lanes`: delivered VK message id to lane id mapping, used for cron/reply routing.
- `pinned`: pinned lane ids per VK peer.

State is not a replacement for config. Config is the canonical durable lane definition. State is UI/routing cache plus fail-safe overlay.

## Commands and buttons

Persistent bottom keyboard for authorized chats/DMs:

```text
Проекты
Новый проект
Команды
```

Important: this keyboard is shown even before a peer has lanes, as long as the peer/user is authorized. This avoids forcing screen-reader users to remember commands.

Text commands:

```text
/project
/project list
/project list N
/project search <query>
/project <id-or-alias>
@alias <text>
/project new
/project new <name> <skills_csv> <context...>
/project edit
/project edit <id> <what to change...>
/project pin
/project unpin
/project pin <id>
/project unpin <id>
/project off
/invite
/commands
```

Project list buttons are VK `text` buttons, not callback-only buttons. Do not rely only on visual UI: every action needs a text fallback.

## Sorting

`/project list` ordering is:

1. pinned lanes for this VK peer;
2. lanes with the newest Hermes synthetic-thread session activity;
3. lanes without session activity in stable config/import order.

Session activity is read from `~/.hermes/state.db` using the standard Hermes session table, filtering by:

```text
source = vk
chat_id = <peer_id>
chat_type = thread
thread_id = lane:<lane_id>
```

If the DB is missing, locked, or has a changed schema, the adapter must fail open and keep config order instead of breaking `/project list`.

## Pinning

Pinning is per VK peer, not global.

User-facing controls:

- inline button after selecting a project: `Закрепить проект` / `Открепить проект`;
- text fallback: `/project pin`, `/project unpin`, `/project pin <id>`, `/project unpin <id>`.

Pinned lanes live in state under `pinned[peer_id]` and sort above session recency.

## Cron lane routing

Cron can deliver into a VK project lane with either:

```text
vk:<peer_id>:lane:<lane_id>
```

or `deliver=origin` when the job origin is a VK synthetic thread:

```json
{
  "origin": {
    "platform": "vk",
    "chat_id": "2000000001",
    "chat_type": "thread",
    "thread_id": "lane:example-project"
  },
  "deliver": "origin",
  "attach_to_session": true
}
```

When the adapter sends a cron message with `metadata.thread_id = lane:<lane_id>`, it stores:

```text
message_lanes["<peer_id>:<conversation_message_id>"] = "<lane_id>"
```

If a user replies to that cron message, the adapter routes the reply into that lane even when the user's current active lane is different.

This is critical for medication reminders, reminders, and any lane-specific recurring job.

## Accessibility rules

- Prefer VK `text` buttons for project controls.
- Keep visible labels meaningful: `Следующая`, `Предыдущая`, `Закрепить проект`.
- Do not rely only on payload/callback events.
- Keep command help copyable.
- Do not add a redundant persistent `Меню` button; the keyboard itself is the menu. Keep `Меню` as text fallback for old keyboards/manual input.

## Importer

Use the setup helper to import lanes from existing mappings:

```bash
python setup_helper.py lanes import \
  --to-peer 2000000001 \
  --from legacy,telegram,discord \
  --telegram-chat-id -1000000000000 \
  --dry-run
```

Then remove `--dry-run` after reviewing output.

Supported sources:

- legacy `vk.project_lanes`;
- Telegram `platforms.telegram.extra.group_topics`;
- Telegram `platforms.telegram.extra.dm_topics`;
- Discord thread mappings if configured.

The importer writes to canonical `platforms.vk.extra.project_lanes`.

## What not to do

- Do not change Hermes core session semantics for VK lanes.
- Do not hardcode private peer ids, group ids, names, invite links, tokens, or local paths.
- Do not make project selection callback-only.
- Do not make direct LLM calls from VK callback/update handlers.
- Do not let config/state parse errors bubble out of `_handle_update()`.
- Do not break text command fallbacks when changing keyboards.
