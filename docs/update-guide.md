# Updating the plugin for a new Hermes version

This guide is for maintainers or coding agents adapting `hermes-vk-platform` to a newer Hermes Agent checkout.

The goal is to keep this repository as a standalone public plugin while allowing the same code to be copied into a local Hermes checkout for runtime testing.

## Repository roles

Standalone public plugin:

```text
hermes-vk-platform/
  adapter.py
  setup_helper.py
  plugin.yaml
  __init__.py
  README.md
  docs/
  tests/test_vk_adapter.py
```

Runtime Hermes checkout plugin copy:

```text
<hermes-agent>/plugins/platforms/vk/
  adapter.py
  setup_helper.py
```

Runtime Hermes checkout tests:

```text
<hermes-agent>/tests/plugins/platforms/test_vk_adapter.py
```

Keep the standalone plugin and runtime copy in sync when validating local changes.

## Before changing anything

1. Inspect the current Hermes checkout.
2. Read `AGENTS.md` if present.
3. Check the current platform/plugin APIs instead of assuming old signatures.
4. Do not print or copy secrets.
5. Do not commit private peer ids, chat names, invite links, screenshots, logs, or local paths.

Useful read-only checks from a Hermes checkout:

```bash
git status --short
python -m py_compile plugins/platforms/vk/adapter.py plugins/platforms/vk/setup_helper.py
```

For the standalone plugin:

```bash
git status --short
python3 -m py_compile adapter.py setup_helper.py __init__.py
python3 -m pytest -q
```

## Sync workflow

When the runtime Hermes copy is the source of truth after live testing:

```bash
cp <hermes-agent>/plugins/platforms/vk/adapter.py ./adapter.py
cp <hermes-agent>/plugins/platforms/vk/setup_helper.py ./setup_helper.py
cp <hermes-agent>/tests/plugins/platforms/test_vk_adapter.py ./tests/test_vk_adapter.py
```

When the standalone plugin is the source of truth before runtime testing:

```bash
cp ./adapter.py <hermes-agent>/plugins/platforms/vk/adapter.py
cp ./setup_helper.py <hermes-agent>/plugins/platforms/vk/setup_helper.py
cp ./tests/test_vk_adapter.py <hermes-agent>/tests/plugins/platforms/test_vk_adapter.py
```

After copying tests from runtime to standalone, confirm imports still work. The standalone tests normally import directly from `adapter`, not `plugins.platforms.vk.adapter`.

## Required verification

Runtime Hermes checkout:

```bash
python -m py_compile plugins/platforms/vk/adapter.py plugins/platforms/vk/setup_helper.py
scripts/run_tests.sh tests/plugins/platforms/test_vk_adapter.py
git diff --check -- plugins/platforms/vk/adapter.py plugins/platforms/vk/setup_helper.py tests/plugins/platforms/test_vk_adapter.py
```

Standalone plugin repo:

```bash
python3 -m py_compile adapter.py setup_helper.py __init__.py
python3 -m pytest -q
git diff --check -- adapter.py setup_helper.py tests/test_vk_adapter.py README.md docs/
```

If dependency metadata changes, follow the Hermes repo rules for lockfiles. This plugin currently has no mandatory third-party Python dependencies.

## Compatibility surfaces to re-check after Hermes updates

### 1. Platform registration

Check `gateway/platform_registry.py` and the current `PlatformEntry` fields. This plugin uses:

- `adapter_factory`;
- `check_fn`;
- `validate_config`;
- `is_connected`;
- allowlist/env metadata;
- `cron_deliver_env_var`;
- `parse_target_ref_fn`;
- `standalone_sender_fn`.

If `PlatformEntry` changes, update `register(ctx)` and tests.

### 2. Send target parsing

VK lane cron delivery depends on the plugin target parser:

```text
vk:<peer_id>:lane:<lane_id>
```

The parser must resolve to:

```python
(chat_id, thread_id, error) == ("2000000001", "lane:example", None)
```

At runtime, Hermes' send-message tool calls the plugin parser through the platform registry. Keep `test_vk_target_parser_accepts_project_lane_thread_target` or equivalent coverage.

### 3. SessionSource / MessageEvent

Project lanes depend on normal Hermes synthetic thread routing:

```text
chat_type = thread
thread_id = lane:<lane_id>
```

If `gateway.session.SessionSource` or `gateway.platforms.base.MessageEvent` changes, update lane routing and tests. Do not introduce a VK-only core route unless the Hermes core API itself requires it.

### 4. Cron delivery metadata

Cron lane routing depends on `metadata.thread_id` reaching `VKAdapter.send()` for live delivery and `_standalone_send(..., thread_id=...)` for fallback delivery.

Re-check:

- `cron/scheduler.py` delivery path;
- `tools/send_message_tool.py` standalone path;
- `gateway/delivery.py` live routing path.

Required invariant: a cron output delivered to `lane:<id>` must produce a `message_lanes` entry so reply-to-message routing can select that lane.

### 5. Hermes session DB schema

Project list sorting reads `~/.hermes/state.db` table `sessions` for:

- `source`;
- `chat_id`;
- `chat_type`;
- `thread_id`;
- `started_at`;
- `ended_at`;
- `last_activity_at`.

If Hermes changes these fields, update `_lane_last_activity()` and tests. The adapter must fail open to config order if the DB is unavailable, locked, or migrated.

### 6. VK API shapes

The adapter relies on these VK API methods:

- `groups.getLongPollServer`;
- Long Poll `message_new` / `message_edit`;
- `messages.send`;
- `messages.edit`;
- `messages.getInviteLink`;
- upload server methods for media.

VK may return either scalar message ids or `peer_ids` response arrays. Keep `_sent_message_id()` tolerant.

## Project Lanes regression checklist

After any meaningful adapter update, verify tests or manual checks cover:

- authorized chats/DMs get the persistent `Проекты / Новый проект / Команды` keyboard;
- unauthorized peers/users do not get controls;
- `Проекты` works even if VK prefixes text with a bot mention;
- project list buttons are VK `text` buttons, not callback-only;
- project buttons have meaningful visible labels and no reliance on `color`;
- page buttons are `Следующая` / `Предыдущая`;
- page navigation can edit the same message when VK returns `conversation_message_id`;
- `/project new` creates and selects a lane when parseable;
- `/project edit` edits current or explicit lanes when parseable;
- `/project pin` and `/project unpin` affect only the current VK peer;
- project sorting is pinned first, then newest session, then config order;
- `/invite` returns the current VK chat invite and does not use hardcoded links;
- cron target `vk:<peer>:lane:<id>` parses;
- cron-delivered messages create `message_lanes` anchors;
- reply to a cron-delivered VK message routes into the anchored lane even when another lane is active.

## Live test guidance

Prefer tests first. Use live VK only for final smoke checks, and never print tokens or invite links.

Safe live smoke examples:

1. Send `/project list` in an authorized test peer.
2. Click a project text button.
3. Press `Закрепить проект`, then reopen `/project list`.
4. Send a controlled test message with `metadata={"thread_id": "lane:<id>"}` and verify `message_lanes` state has a new anchor.
5. Reply to that message and verify the resulting session source is `chat_type=thread`, `thread_id=lane:<id>`.

For any live config mutation:

1. backup config/state first;
2. mutate only the intended peer/job;
3. verify counts and target ids;
4. restart the gateway if the running process needs config/code reload.

## Public release hygiene

Before publishing:

```bash
python3 -m py_compile adapter.py setup_helper.py __init__.py
python3 -m pytest -q
git diff --check -- adapter.py setup_helper.py tests/test_vk_adapter.py README.md docs/
```

Then scan for private material:

```bash
grep -RInE 'vk1\.a\.[A-Za-z0-9._-]{20,}|access_token|invite_link|PRIVATE_|/home/|/root/' \
  . --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.pytest_cache
```

Placeholders are fine. Real tokens, invite links, personal peer ids, and local deployment paths are not.
