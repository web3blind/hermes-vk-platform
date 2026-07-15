# VK Platform plugin installed

The VK Messenger platform plugin has been installed.

## Next steps

1. Configure it:

```bash
hermes gateway setup
```

Choose **VK Messenger** when prompted, or set the required variables manually in `~/.hermes/.env`:

```dotenv
VK_GROUP_TOKEN=vk1.a.your-community-token
VK_GROUP_ID=123456789
VK_ALLOWED_USERS=123456
# or:
VK_ALLOWED_PEERS=2000000001
```

2. Restart the gateway:

```bash
hermes gateway restart
```

3. Send a message to your VK community or an allowed VK chat.

## Important safety note

Do not leave a personal Hermes agent open to all VK users. Prefer `VK_ALLOWED_USERS` or `VK_ALLOWED_PEERS`.

Use `VK_ALLOW_ALL_USERS=true` only for temporary testing in an isolated community.

## Docs

Read `README.md` in this plugin directory for the full setup guide, troubleshooting, and development notes.
