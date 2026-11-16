---
name: myai-git-push-policy
description: "Push myai work freely, only on non-main branches; credentials in a repo-local helper; commit as xtremislv"
metadata:
  type: feedback
---

For /root/myai (github.com/xtremislv/myai) commit and push freely, as long as the work goes on branches other than
`main`. Never push to main.

**Why:** the owner reviews work before it reaches main; the fine-grained token covers only this repo.
**How to apply:** plain `git push`; credentials come from the repo-local credential helper (never print or commit its
file). Commit with `git -c user.name=xtremislv -c user.email=xtremislv@users.noreply.github.com` (no global identity).
Record new branches in BRANCHES.md. Related: [[myai-server-setup]].
