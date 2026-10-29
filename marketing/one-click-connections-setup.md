# One-click connections: what we set up once

Customers only press **Connect**. The two things below are done once by us, never by customers. Until a provider's keys are in the
server `.env`, its card shows "Coming soon" instead of a Connect button.

## Google Docs

1. Google Cloud Console, the same project used for sign-in > **APIs & Services > Library** > enable **Google Drive API**.
2. **Credentials > your Web OAuth client** (the one whose ID is already `GOOGLE_OAUTH_CLIENT_ID`).
   - Add this **Authorised redirect URI**: `https://kauli-forgemedia.com/client/integrations/gdocs/callback`
   - Copy its **Client secret**.
3. **OAuth consent screen > Data access**: add the scope `.../auth/drive.file` (Kauli can only touch documents it creates itself).
   While the app is in "Testing" only listed test users can connect; press **Publish app** when ready (`drive.file` needs no Google review).
4. On the server, add to `.env` and redeploy:
   ```
   GOOGLE_OAUTH_CLIENT_SECRET=...        (GOOGLE_OAUTH_CLIENT_ID is already set)
   ```

## Slack (optional, replaces pasting a webhook address)

1. api.slack.com/apps > Create New App > From scratch. **OAuth & Permissions**: add the redirect URL
   `https://kauli-forgemedia.com/client/integrations/slack/callback`, and the bot scope is not needed: under **Incoming Webhooks** switch it on.
2. **Basic Information**: copy the Client ID and Client Secret into `.env` as `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET`.

## What customers see

Connect > Google's own sign-in window > "Connection Successful" > a Settings dialog with switches (save every finished order
automatically, include summary, transcript link, timestamps, speaker names) > Save. Settings can be changed or the connection removed from the same card.
