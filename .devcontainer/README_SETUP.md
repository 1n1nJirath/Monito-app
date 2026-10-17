# Monito AnurakSciCU — Setup / Handover

## 1. Files
- `app.py`: Streamlit application.
- `requirements.txt`: package list for Streamlit Community Cloud.

## 2. Google Sheets tabs
The app uses these tabs in one Google Sheets file:

1. `รายชื่อ` — columns must be exactly `Name`, `Dept`, `Year`, `DOB`.
2. `ผลจับฉลาก` — create the tab once; it can be blank before the first draw. The app writes `Name`, `Dept`, `Year`, `Monito` here. **DOB is not written to this tab.**
3. `ตั้งค่า` — a small internal tab with header row `Key`, `Value`. The app writes the salted Admin-password hash here. Hide the tab and protect it against accidental edits. The application’s Service Account needs Editor permission. This third tab is needed so an Admin can change their password without a developer updating code or Streamlit Secrets every time.

Share the spreadsheet with the Service Account email as **Editor**. Keep spreadsheet access limited to trusted staff; do not share it publicly.

### Participant data
- `Name` must be unique. If two participants use the same name, distinguish them with full name/nickname so the app can identify the correct person.
- `DOB` must be 8 digits in `DDMMYYYY` format. Format that column as **Plain text** in Google Sheets so leading zeroes are retained.
- Each circle must contain at least 3 people. The app will not allow singleton or 2-person circles because a 2-person circle would necessarily create a reciprocal pair.
- Do not edit `รายชื่อ` after the draw. To start another camp, back up the result file, reset results through the Admin UI, then replace the roster.

## 3. Streamlit Community Cloud Secrets
Set these in the app’s **Settings → Secrets**. Do not commit actual credentials to GitHub.

```toml
# One-time bootstrap password used only before an Admin password has been set.
# Use a random value of at least 16 characters. After first setup, the app ignores it.
ADMIN_BOOTSTRAP_PASSWORD = "REPLACE_WITH_A_RANDOM_ONE_TIME_PASSWORD"

[app]
spreadsheet_url = "https://docs.google.com/spreadsheets/d/REPLACE_WITH_SPREADSHEET_ID/edit"

[connections.gsheets]
type = "service_account"
project_id = "REPLACE_WITH_PROJECT_ID"
private_key_id = "REPLACE_WITH_PRIVATE_KEY_ID"
private_key = "-----BEGIN PRIVATE KEY-----\\nREPLACE_WITH_PRIVATE_KEY_CONTENT\\n-----END PRIVATE KEY-----\\n"
client_email = "REPLACE_WITH_SERVICE_ACCOUNT_EMAIL"
client_id = "REPLACE_WITH_CLIENT_ID"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "REPLACE_WITH_CLIENT_X509_CERT_URL"
```

Copy the real Service Account fields from the downloaded Google Cloud JSON key. Keep the `private_key` newlines correctly escaped as `\\n` in TOML.

## 4. First-time Admin setup
1. Deploy `app.py` and `requirements.txt` to Streamlit Community Cloud.
2. Confirm the Google Sheets file has the three tabs above and is shared with the Service Account.
3. Open the web app and expand **สำหรับสตาฟ / Admin**.
4. Sign in with `ADMIN_BOOTSTRAP_PASSWORD` from Streamlit Secrets.
5. Immediately set a new Admin password of at least 12 characters.
6. After this, the app stores only a salted PBKDF2 hash in `ตั้งค่า`. Routine password changes happen through the Admin UI, not by editing code or Secrets.

The bootstrap secret is ignored after the password is initialized. If the Admin password is forgotten and all active Admin sessions are gone, the spreadsheet owner will need to perform a controlled recovery of the `ตั้งค่า` credentials.

## 5. Workflow
- Admin signs in before the first draw.
- Select the desired number of circles. Each circle is closed within its own group and has at least 3 people.
- The app validates the pairing and checks the result after writing it to Google Sheets.
- Participants can check their own result on the website while online by selecting their name and entering DOB.
- For offline use, an authenticated Admin downloads the Excel backup from the app. The file includes only `Name`, `Dept`, `Year`, and `Monito`, with no DOB; it can be opened without internet.
- Admin can search for someone who forgot their result and clear the results tab after exporting a backup.

## 6. Important limitation
DOB is retained as the participant-facing password to match the camp workflow, but a date of birth can be known or guessed. It is a convenience check, not strong identity authentication. The app limits incorrect attempts per active Streamlit session; this is not a global anti-brute-force system.
