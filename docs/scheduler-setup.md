# Scheduler Setup

Two Windows Task Scheduler tasks run the local autopilot. Both run only when you're
logged on (no stored password) and use the repo-local venv. Neither publishes
anything — they draft, regenerate local files, and pull read-only stats.

## Morning autopilot (daily 07:00)

Drafts everything so the `/today` control panel is ready when you wake up.

- Task name: `ShadowEdge-DailyOps-Morning`
- Runs: `run-daily-ops.bat` → in order:
  1. `listening.py scan` — find + draft engagement replies (LLM; never posts)
  2. `daily_posts.py generate` — draft today's X / LinkedIn / Stocktwits / Reddit posts (LLM)
  3. `video_script.py` — draft the next-video script from the winning theme (LLM)
  4. `ops_tracker.py` — regenerate `daily-ops\<today>.md` from current state (offline)
- Schedule: daily at 07:00, `StartWhenAvailable` (catches up a missed run), 10-min limit.
- Log: `state\logs\daily-ops.log`

```powershell
Get-ScheduledTaskInfo    -TaskName 'ShadowEdge-DailyOps-Morning'   # status / next run
Start-ScheduledTask      -TaskName 'ShadowEdge-DailyOps-Morning'   # run now
Unregister-ScheduledTask -TaskName 'ShadowEdge-DailyOps-Morning' -Confirm:$false  # remove
```

## YouTube stats feedback loop (daily 04:55 Eastern)

Pulls live YouTube stats before the workday so `/youtube` shows movement over
time, the editor can steer the next video, and `/today` is regenerated with the
fresh signal. Read-only against the YouTube Data API — never publishes.

- Task name: `ShadowEdge-YouTube-Stats-Daily`
- Runs: `run-youtube-stats.bat` → `youtube_stats.py pull`, `refresh_editor.py`, then `ops_tracker.py`
- Schedule: daily at 04:55 Eastern, `StartWhenAvailable`, 10-min limit.
- Log: `state\logs\youtube-stats.log`

```powershell
Get-ScheduledTaskInfo    -TaskName 'ShadowEdge-YouTube-Stats-Daily'
Start-ScheduledTask      -TaskName 'ShadowEdge-YouTube-Stats-Daily'
Unregister-ScheduledTask -TaskName 'ShadowEdge-YouTube-Stats-Daily' -Confirm:$false
```

To change a time, re-register the task with a different `-At`:

```powershell
$repo = 'C:\Users\jorda\GitHub\jordanhenning32\marketing-ops'
$action  = New-ScheduledTaskAction -Execute (Join-Path $repo 'run-daily-ops.bat') -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At '07:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'ShadowEdge-DailyOps-Morning' -Action $action -Trigger $trigger -Settings $settings -Force
```

Note: tasks run only when the user is logged on; the machine must be awake at the
scheduled time (or the run catches up on next wake).
