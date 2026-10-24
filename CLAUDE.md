# Working agreements for this repo

- When creating `whatsapp-inbox` or `whatsapp-outbox` folders (or any similar
  runtime data folder), don't list them in the root `.gitignore`. Instead,
  create a `.gitignore` file inside the folder itself containing just `*`, so
  the folder self-ignores.
