# Development notes

This document is for future maintainers and agents improving the VK platform plugin.

## Plugin shape

The public plugin is intentionally a normal Hermes directory plugin:

```text
hermes-vk-platform/
  plugin.yaml
  __init__.py
  adapter.py
  setup_helper.py
  after-install.md
  README.md
  docs/
  tests/
```

Hermes plugin discovery requires:

- `plugin.yaml`
- `__init__.py`
- `register(ctx)` exported from `__init__.py`

`adapter.py` exposes `register(ctx)` and calls:

```python
ctx.register_platform(
    name="vk",
    label="VK Messenger",
    adapter_factory=_build_adapter,
    ...
)
```

## Runtime flow

```text
VK Group Long Poll
  -> message_new event
  -> VKAdapter parses peer_id / from_id / text / attachments
  -> adapter allowlist check
  -> MessageEvent(source.platform='vk', chat_id=peer_id, user_id=from_id)
  -> Hermes gateway session + agent
  -> VKAdapter.send(...)
  -> VK messages.send
```

Tool progress uses:

```text
Hermes progress bubble
  -> adapter.send(...)
  -> VK returns conversation_message_id where available
  -> adapter.edit_message(...)
  -> VK messages.edit
```

## Keep public/plugin boundaries clean

Do not add private deployment values to this repository. Use placeholders in docs and tests.

Forbidden in public files:

- real VK community ids;
- real VK peer ids from private chats;
- personal names or chat names;
- local filesystem paths from a maintainer machine;
- tokens, cookies, `.env`, logs, or screenshots with identifiers.

Safe examples:

- `123456789` for group id;
- `123456` / `789012` for user ids;
- `2000000001` / `2000000042` for group peer ids;
- `vk1.a.your-community-token` for a token placeholder.

## Configuration ownership

- Secrets belong in `~/.hermes/.env`.
- User-visible non-secret behavior may live in `~/.hermes/config.yaml`.
- The setup helper currently writes env vars through `hermes_cli.config.save_env_value()`.
- The adapter supports YAML bridging through `_apply_yaml_config()` for users who prefer `vk:` config blocks.

## Access control

The adapter sets:

```python
enforces_own_access_policy = True
```

This matters because VK supports peer-level allowlisting. The adapter must reject unauthorized messages before calling `handle_message(event)`.

Do not change the default to broad open access. If no allowlist is configured, public docs should strongly recommend adding one.

## Attachment behavior

Inbound attachments are converted into Hermes `MessageEvent.media_urls` and `media_types` where possible.

Important rules:

- Voice messages should be materialized locally so STT can read them.
- Direct file-like URLs may be cached under the Hermes cache directory.
- Public VK video watch pages such as `https://vk.com/video...` are **not** treated as downloadable video files.
- Regular video metadata may need `messages.getHistoryAttachments` or optional `VK_USER_TOKEN` + `video.get` to expose direct file URLs.

Outbound remote image URLs are sent as text URLs. Local image files use the VK message photo upload flow.

## Testing

From this plugin repository, use:

```bash
python -m pytest tests/test_vk_adapter.py
```

From a Hermes source checkout, you can also run the copied tests with Hermes' wrapper after setting `PYTHONPATH` to include this plugin directory if needed.

The tests must not require live VK credentials or network access. Mock `_vk_method`, `_download_attachment_async`, and `_multipart_upload_async` for behavior checks.

## Before release

Run:

```bash
python -m py_compile adapter.py setup_helper.py __init__.py
python -m pytest tests/test_vk_adapter.py
```

Then scan for private values:

```bash
grep -RInE 'REAL_TOKEN_PATTERN|PRIVATE_GROUP_ID|PRIVATE_PEER_ID|PRIVATE_CHAT_NAME|LOCAL_HOME_PATH|\.env$' . --exclude-dir=.git
```

The placeholder `VK_GROUP_TOKEN=vk1.a.your-community-token` in documentation is acceptable; real tokens are not.
