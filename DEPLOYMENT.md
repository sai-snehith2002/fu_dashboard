# Deploying the FollowUp Dashboard (live, hourly refresh)

## Feasibility check (read this first)

Your ask involves three separate pieces — one of them isn't actually
suited to what you had in mind. Straight talk:

| Piece | Verdict | Why |
|---|---|---|
| **Streamlit Community Cloud** as the host | ✅ Feasible on the free tier | Wakes on visits, auto-redeploys on git push, reads a CSV committed to the repo. 1 GB RAM is plenty for a 10-20 MB CSV. |
| **GitHub Actions** cron every hours for the Metabase pull | ✅ Feasible on the free tier | Query time is 2-3 min per run; that's ~36 min/day = ~18 hours/month, comfortably inside the free 2000 min/month for private repos (unlimited for public). Job timeout is 15 min per run — plenty of headroom over the 2-3 min query. |
| **GitHub Codespaces as the *hosting* platform** | ❌ **NOT feasible** | Codespaces is a *development* environment, not a hosting one. The container stops after 30 min of inactivity, the public URL dies with it, and the free tier's 120 core-hours/month gets burned in ~5 days of 24×7 uptime. It also gives you no way to make an app publicly reachable at a stable URL. |
| **GitHub Codespaces as the *development* environment** | ✅ Feasible and included below | Opens a full VS Code + Python 3.11 in the browser with all dependencies pre-installed and Streamlit's port pre-forwarded, so you can edit and preview the app without installing anything locally. This is what the included `.devcontainer/devcontainer.json` is for. |
| **10-20 MB CSV, 2-3 min query, 2-hourly refresh** | ✅ All fits | Streamlit reads a 15 MB CSV in ~1-2s. Git handles it fine (delta compression keeps repo growth to ~1-2 GB/year; well inside GitHub's 5 GB soft limit for years). |

So the actual, working shape of the deployment is:

**Streamlit Community Cloud for hosting + GitHub Actions cron for refresh + (optionally) Codespaces for editing the code.**

---

## How it works

```
GitHub Actions (runs every 2h, holds the Metabase API key)
        │
        ▼
    pulls fresh data from Metabase (2-3 min query)
        │
        ▼
    commits followup_dashboard.csv back to the repo
        │
        ▼
Streamlit Community Cloud sees the new commit → auto-reboots the app
        │
        ▼
    app.py just reads the CSV that's already sitting in the repo
```

The Metabase API key never leaves the GitHub Actions runner — it lives
in a repo secret, and the deployed Streamlit app never talks to
Metabase directly. That also means the app has nothing to fail against
if Metabase is briefly down; the last-known-good CSV keeps serving.

**Files in this repo that make it work:**
- `app.py` — the Streamlit dashboard (reads `followup_dashboard.csv`)
- `metrics.py` — pure pandas logic behind every card / table
- `data_pull.py` — pulls one card from Metabase and writes the CSV. Reads
  `MB_API_KEY` from either an environment variable (what GitHub Actions
  passes) or a local `config.toml` (what you use locally).
- `.github/workflows/refresh_data.yml` — the 2-hourly GitHub Actions job.
- `.devcontainer/devcontainer.json` — Codespaces dev setup (optional).
- `.gitignore` — keeps `config.toml` (your real API key) out of git.
- `requirements.txt` — tells Streamlit Cloud what to install.

---

## Step-by-step setup

### 1. Create a GitHub repo and push this project

If you're doing this from a local machine (skip to step 1b for the
Codespaces variant):

```bash
cd /path/to/this/folder
git init
git add app.py metrics.py data_pull.py followup_dashboard_query.sql \
        requirements.txt .gitignore .github .devcontainer DEPLOYMENT.md
git commit -m "FollowUp dashboard: initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<repo-name>.git
git push -u origin main
```

Deliberately **don't** `git add config.toml` — it's gitignored and
should never be committed since it can hold your real API key locally.

The repo can be public or private; both work with GitHub Actions and
with Streamlit Community Cloud (which can deploy from private repos too,
once you connect your GitHub account). If you want unlimited free
Actions minutes, make it public.

#### 1b (alternative). Create the repo from GitHub's UI, then edit it in a Codespace

If you'd rather not touch a local terminal at all:

1. Go to https://github.com/new → create an empty repo (no README,
   no gitignore — you'll add those from this project).
2. On the empty repo page → **Code** button → **Codespaces** tab →
   **Create codespace on main**.
3. Once VS Code loads in the browser, drag-and-drop the files from
   this folder into the codespace's file explorer (or use the terminal
   inside the codespace to `git clone`, then copy files across).
4. In the codespace terminal:
   ```bash
   git add app.py metrics.py data_pull.py followup_dashboard_query.sql \
           requirements.txt .gitignore .github .devcontainer DEPLOYMENT.md
   git commit -m "FollowUp dashboard: initial commit"
   git push
   ```
5. To preview the app right inside the codespace before deploying:
   ```bash
   streamlit run app.py
   ```
   The codespace will auto-forward port 8501 and pop a preview link
   in the notifications; open it to see the app running against
   whatever CSV you last generated locally. This is only for testing
   — the deployment doesn't run out of the codespace.

### 2. Add your Metabase API key as a GitHub Actions secret

In the GitHub repo: **Settings → Secrets and variables → Actions →
New repository secret**
- Name: `MB_API_KEY`
- Value: your real Metabase API key (the same one that goes in
  `config.toml` locally)

That's the only secret required — `url` and `card_id` already default to
your Metabase instance and card 5360 inside `data_pull.py`.

### 3. Test the scheduled pull manually (before waiting 2 hours)

Go to the repo's **Actions** tab → "Refresh FollowUp dashboard data" →
**Run workflow** (the `workflow_dispatch` trigger lets you fire it on
demand instead of waiting for the schedule). Check the run's logs — it
should:
- Print `[data_pull] Pulled N rows...` (takes 2-3 minutes because that's
  how long the Metabase query itself takes)
- If the data changed since the last run, push a new commit updating
  `followup_dashboard.csv`

If it fails at the "Pull latest data from Metabase" step, the error
message from `data_pull.py` will say why (bad/missing API key, Metabase
unreachable, etc.) — the workflow doesn't hide that output.

Once this works, it will keep running automatically every 2 hours (the
`cron: "0 */2 * * *"` schedule) with no further action needed.

### 4. Deploy the app on Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in
   (GitHub login works directly).
2. **New app** → pick your repo, branch `main`, main file path `app.py`.
3. Deploy. First build takes a minute or two.
4. Confirm **"Reboot on push"** is on for the app (Streamlit Cloud's
   app settings → this is the default, so normally nothing to change)
   — this is what makes each 2-hourly GitHub commit actually refresh
   the live dashboard.

No secrets need to be added on the Streamlit Cloud side — the app never
talks to Metabase itself, so it doesn't need `MB_API_KEY` at all.

### 5. Verify end-to-end

- Open the deployed app URL — it should load using whatever
  `followup_dashboard.csv` is currently in the repo.
- Trigger the GitHub Action manually again (step 3) and watch for a
  new commit.
- The Streamlit Cloud app should reboot within roughly a minute of
  that push (you'll briefly see a "rerunning" spinner if you have it
  open) and then reflect the refreshed numbers.

---

## Things worth knowing about the free tier

- **Sleeping apps:** a Community Cloud app that gets no visits for an
  extended stretch goes to sleep and shows a "wake this app up" screen
  to the next visitor. This doesn't affect data freshness (the GitHub
  Action keeps committing every 2 hours regardless), it just means the
  very first visitor after a long quiet period has to click one button
  and wait a few seconds for the app to spin back up.
- **Resource limits:** free-tier apps get roughly 1 GB RAM and shared
  CPU. Your dataset (10-20 MB) is well inside this.
- **Reboot blip:** every 2-hourly commit that actually changes the data
  triggers a full app reboot, which means a few seconds of "app is
  starting" for anyone viewing it right at that moment. If nothing
  changed in that cycle's pull, the workflow skips the commit (and the
  reboot) entirely — see the "Commit and push if the data changed"
  step.
- **Public link:** the deployed app is a fully public URL — anyone who
  has the link can open it without logging in. If you want to lock
  that down later, Streamlit Cloud's app settings has a
  "Who can view this app" control for restricting it to specific
  viewer emails, with no code changes needed on this end.
- **GitHub Actions minutes:** a 2-hourly job at ~3 minutes of actual
  runtime is roughly 270 minutes/month — well inside GitHub's free
  quota (2000 min/month for private repos, unlimited for public
  repos).
- **Repo bloat:** committing a 10-20 MB CSV every 2 hours grows the
  repo, but git's delta compression handles append-mostly data very
  well; realistic growth is ~1-2 GB/year, safely inside GitHub's 5 GB
  soft limit for years. If it ever becomes an issue, you can squash
  the auto-refresh history without disturbing your own commits (ask
  me to walk you through it when needed).

## If the anchor dates ever need to change

`cohort_from` (`2026-07-31`) and `start_date` (`2026-09-01`) are fixed
in `.github/workflows/refresh_data.yml`'s "Pull latest data from
Metabase" step — edit those two `--cohort-from` / `--start-date`
values directly in the workflow file and commit, the same way you'd
edit `config.toml` locally. `end_date` doesn't need touching; it's
recomputed as "today" (Asia/Kolkata) on every run.