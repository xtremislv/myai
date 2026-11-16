---
name: myai-session-start
description: "Start every myai session by reading docs/CLAUDE_CODE_SERVER.md then docs/SESSION_WRAPUP.md; keep the wrap-up current, no secrets"
metadata:
  type: project
---

Before doing anything in /root/myai read `docs/CLAUDE_CODE_SERVER.md` (background, setup, processes, owner's rules
for this server) and `docs/SESSION_WRAPUP.md` (current state and next steps). Update SESSION_WRAPUP.md at the end of
each working session (update in place; `docs/HANDOFF.md` is the detailed log).

**Why:** the owner wants each new session to understand the system quickly (asked 2026-11-03).
**How to apply:** never put passwords, tokens, keys or the proxy VPS address in either doc: both are pushed to
GitHub (a push with a test password was once blocked as credential leakage). Related: [[myai-server-setup]],
[[myai-git-push-policy]].
