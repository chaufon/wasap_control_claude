# Working agreements for this repo

- After any code change here, restart the affected `systemctl --user` service(s)
  (`whatsapp-bridge.service`, `claude-orchestrator.service`) without asking first.
- When creating `whatsapp-inbox` or `whatsapp-outbox` folders (or any similar
  runtime data folder), don't list them in the root `.gitignore`. Instead,
  create a `.gitignore` file inside the folder itself containing just `*`, so
  the folder self-ignores.
