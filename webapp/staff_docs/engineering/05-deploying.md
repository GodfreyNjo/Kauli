# Deploying

**Deploy only when the owner asks.** A deploy replaces what customers are using, so it is a deliberate act, not a side effect of finishing a task.

## Before

1. `bash scripts/check.sh` passes (quality gate and the whole test suite).
2. The change is reviewed and merged to `main`, and `git log` shows the commits you expect.
3. **If it changes the database** (a new table or column, or a migration that updates rows): take a backup first and check it is sound (step 3 below).
4. Say what is going out, in plain words, and what you will check afterwards.

## How a deploy works

The server builds its own image from a copy of the repository. We send it only the new commits, as a git bundle, rather than pushing to a public host.

1. Make a bundle of everything since the commit the server is on: `git bundle create k.bundle <server-commit>..HEAD`.
2. Copy it to the server with the deploy key (the key and the server address are in the team's password manager, never in the repository).
3. On the server, **back up the database** with SQLite's own backup call (a plain file copy of a live database can be torn), then check it:

```bash
docker exec kauli python3 -c "
import sqlite3
src = sqlite3.connect('/app/webapp/data/kauli_demo.db'); dst = sqlite3.connect('/app/webapp/data/backup-pre-YYYYMMDD.db'); src.backup(dst)
print(sqlite3.connect('/app/webapp/data/backup-pre-YYYYMMDD.db').execute('pragma integrity_check').fetchone())"
```

4. Bring the commits in: `git pull --ff-only /root/k.bundle HEAD` in the repository directory on the server. (`--ff-only` refuses anything that is not a clean continuation.)
5. Build the image in the background (it takes a few minutes) and wait for it to report success: `docker build -t kauli:latest .`
6. Run `./deploy.sh` in that directory. It restarts the container with the data volume attached, bound to the loopback address only, and prints whether the stored files survived.

## After: check the live site

Do all of these, and say what you saw.

- [ ] The pages you changed load (HTTP 200) and show the change. Add `?cb=123` to skip caches.
- [ ] The home page, sign-in and one service page load.
- [ ] `docker logs kauli --since 3m | grep -ci error` is **0**.
- [ ] The container's port is **not** reachable from outside (`curl http://<server>:8000` must fail).
- [ ] The user and order counts are what they were (nothing was lost).
- [ ] If there was a migration: the rows it should have changed changed, and no others.
- [ ] The Diagnostics page shows no new failure.

## Rolling back

The data volume is never touched by a deploy, so rolling back means running the previous code:

1. On the server, put the repository back on the last good commit (`git reset --hard <good-commit>`), rebuild, and run `./deploy.sh`.
2. A migration only adds columns and tables and does not need undoing. If a migration changed **rows** wrongly, restore from the backup you took in step 3, and say so to the owner first.
3. Write down what failed, add the missing test, and fix forward.

## What never to do

- Never edit the live database by hand, or copy it while it is being written.
- Never put a secret in a command that is saved in shell history or in a message.
- Never touch other applications' containers on the shared server.
- Never skip the checks because the change is "small".
