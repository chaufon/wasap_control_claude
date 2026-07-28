# Working agreements for this repo

- After any code change here, restart the affected `systemctl --user` service(s)
  (`whatsapp-bridge.service`, `claude-orchestrator.service`) without asking first.
- Everytime the current behavior is changed, do not add explanations or justifications as comments or in the docs,
instead consider the new behavior as the original one; then update comments and docs accordingly.