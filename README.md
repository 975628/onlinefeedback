# FeedbackHub

## Run locally

Install the requirements and start the app from PowerShell:

```powershell
python -m pip install -r requirements.txt
$env:ADMIN_USERNAME = [Environment]::GetEnvironmentVariable("ADMIN_USERNAME", "User")
$env:ADMIN_PASSWORD = [Environment]::GetEnvironmentVariable("ADMIN_PASSWORD", "User")
$env:FLASK_SECRET_KEY = [Environment]::GetEnvironmentVariable("FLASK_SECRET_KEY", "User")
$env:FEEDBACKHUB_DATA_DIR = [Environment]::GetEnvironmentVariable("FEEDBACKHUB_DATA_DIR", "User")
$env:FEEDBACK_RETENTION_DAYS = [Environment]::GetEnvironmentVariable("FEEDBACK_RETENTION_DAYS", "User")
python app.py
```

The app stops with a clear error if the admin password or Flask secret key is
missing; it does not generate or print fallback credentials.

Waitress serves the app on `127.0.0.1:5000`. Open the feedback form at
`http://127.0.0.1:5000/` and the admin dashboard at
`http://127.0.0.1:5000/admin`.

## Keep admin credentials stable on Windows

Run this once in PowerShell. It creates a strong admin password and Flask session
key, saves them in your Windows user environment (not in the source files), and
prints the password once so you can sign in:

```powershell
$python = (Get-Command python).Source
$adminPassword = & $python -c "import secrets; print(secrets.token_urlsafe(24))"
$secretKey = & $python -c "import secrets; print(secrets.token_hex(32))"
[Environment]::SetEnvironmentVariable("ADMIN_USERNAME", "admin", "User")
[Environment]::SetEnvironmentVariable("ADMIN_PASSWORD", $adminPassword, "User")
[Environment]::SetEnvironmentVariable("FLASK_SECRET_KEY", $secretKey, "User")
Write-Host "Admin username: admin"
Write-Host "Admin password (save it now): $adminPassword"
```

Close and reopen the terminal/VS Code, then restart the app so it loads the saved
values. The password and session key stay the same between restarts. Do not share
or commit these values.

If a password has been exposed, rotate it from PowerShell without printing the
new password:

```powershell
$python = (Get-Command python).Source
$adminPassword = & $python -c "import secrets; print(secrets.token_urlsafe(24))"
$secretKey = & $python -c "import secrets; print(secrets.token_hex(32))"
[Environment]::SetEnvironmentVariable("ADMIN_PASSWORD", $adminPassword, "User")
[Environment]::SetEnvironmentVariable("FLASK_SECRET_KEY", $secretKey, "User")
```

Changing the Flask secret key invalidates existing admin sessions. Restart the
app and retrieve the new password only when you need it:

```powershell
[Environment]::GetEnvironmentVariable("ADMIN_PASSWORD", "User")
```

## Abuse protections

Admin login is limited to 5 attempts per IP per 15 minutes. Feedback submissions
are limited to 10 per IP per hour. Limits are stored in SQLite so they survive
app restarts; behind a reverse proxy, configure the trusted proxy settings below
so the app sees the real client IP. CSV text cells beginning with spreadsheet
formula characters are prefixed to prevent them from being evaluated as formulas.

CSRF tokens stay stable for the browser session rather than rotating after each
POST. This allows forms opened in separate tabs to remain valid. Tokens are still
checked on every state-changing request and are replaced when an admin signs in.

## Database location, backups, and restore checks

On Windows the SQLite database lives in
`%LOCALAPPDATA%\FeedbackHub\feedbackhub.sqlite3`, outside the OneDrive project
folder. On first startup, if the old project `database.db` exists and the new
database does not, the app copies it with SQLite's backup API, verifies its
integrity and feedback table, and only then removes the old project-folder copy.
If verification fails, startup stops and the old database remains in place.
Set `FEEDBACKHUB_DATA_DIR` before starting the app to choose another persistent
directory; do not put it inside a synced/public web directory.

Create and restore-test a dated backup manually:

```powershell
python backup_database.py
```

The script makes a consistent SQLite snapshot under
`%LOCALAPPDATA%\FeedbackHub\backups` and restores it into a temporary database
to check SQLite integrity and feedback row counts.

For automatic daily backups while signed into Windows, register a user task:

```powershell
$project = (Get-Location).Path
$python = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$project\backup_database.py`""
$trigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName "FeedbackHub daily backup" -Action $action -Trigger $trigger -Settings $settings -Description "Back up and restore-test FeedbackHub SQLite data daily"
```

The app also runs `backup_database.py` daily at 2:00 AM while the Windows user is
signed in; missed runs start when the task scheduler becomes available again.
Backups contain personal feedback too. Keep them access-controlled and encrypted,
and copy a backup to a separate protected device or storage account; a backup on
the same computer cannot recover from disk loss. The app removes local backup
files older than 30 days. Run `python backup_database.py` after a backup restore
to verify the restored database.

## Feedback privacy and retention

The app stores submitted names, email addresses, ratings, comments, and submission
times. Use this information only to review and respond to feedback; limit
dashboard and backup access to authorized operators. Do not collect sensitive
personal information in comments. The configured retention period is 90 days:
expired feedback is deleted from the live database at startup and before the
feedback form writes data or an admin views/exports it. Full-database backup
files are retained for 30 days, so deleted feedback can remain in backups for up
to 30 additional days before backup rotation removes those snapshots. Set
`FEEDBACK_RETENTION_DAYS` to a different positive number of days only after
checking your business purpose and applicable privacy law. Inform submitters
about the data collected, purpose, retention, and a contact for privacy requests.

## Put it behind HTTPS for internet access

Do not expose Waitress directly to the internet. It listens only on localhost.
Use a reverse proxy such as Caddy for public HTTPS:

1. Point a domain you control to the server and allow inbound ports 80 and 443.
2. Install Caddy on the server and copy `Caddyfile.example` to its Caddyfile.
   Replace `feedback.example.com` with your real domain.
3. In an elevated PowerShell, enable secure cookies and trust exactly one local
   proxy (Caddy). These are user-scoped settings; restart the app afterward:

   ```powershell
   [Environment]::SetEnvironmentVariable("COOKIE_SECURE", "true", "User")
   [Environment]::SetEnvironmentVariable("TRUST_PROXY", "1", "User")
   [Environment]::SetEnvironmentVariable("FEEDBACK_RETENTION_DAYS", "90", "User")
   ```

4. Start the app with `python app.py`, then start Caddy with its Caddyfile.
   Caddy obtains and renews the HTTPS certificate automatically when the domain
   and network ports are configured correctly.

The app adds HSTS on HTTPS requests. Only enable `TRUST_PROXY=1` when the app is
reachable solely through one trusted reverse proxy. Keep the operating system,
Python packages, and database backups secured and up to date.

## Temporary free demo on Render

The included `render.yaml` configures a Render free web service. This is for
temporary demos only: the SQLite database is stored on the service's
ephemeral filesystem, so feedback can be lost when the service restarts or
redeploys. Free services can also sleep while idle. Do not use this setup for
real or important feedback. Render provides an HTTPS URL, so a separate domain
and Caddy installation are optional.

1. Sign in to Render, create a Blueprint, and connect the
   `975628/onlinefeedback` GitHub repository.
2. Select the free web-service plan if prompted. Do not add a paid plan or
   persistent disk for this temporary demo.
3. When prompted, set `ADMIN_PASSWORD` to a strong unique password and
   `FLASK_SECRET_KEY` to a separate random secret. Keep both in Render's
   environment settings; never put them in GitHub or share them.
4. Deploy the Blueprint. When the deployment finishes, open the Render URL and
   check the feedback form and `/admin/login`.

If you forget the admin password, open the service's **Environment** page in
Render, replace `ADMIN_PASSWORD` with a new strong password, and choose
**Save, rebuild, and deploy**. Leave `FLASK_SECRET_KEY` unchanged; this demo
does not provide email-based password recovery.

For example, generate values locally in PowerShell with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))"
python -c "import secrets; print(secrets.token_hex(32))"
```

Use the first output as `ADMIN_PASSWORD` and the second as `FLASK_SECRET_KEY`.
Save the password somewhere private so you can sign in later. The Render
Blueprint does not contain either secret value. Check Render's billing screen
before confirming; if it shows a charge, stop and do not create the service.
