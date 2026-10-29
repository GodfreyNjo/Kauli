# Switching on "Import from Google Drive, OneDrive and Dropbox"

Each service needs one small "app" created in its own developer console, because it is the service, not Kauli, that lets a website open its file picker. Each one works on its own: switch on whichever you want. The server `.env` gets the keys; after editing it, restart the app (`/root/kauli/deploy.sh`).

A button appears on the upload step only for a service whose settings are all present. Until then the Integrations page shows it as "Coming soon".

Kauli never sees the customer's Google, Microsoft or Dropbox password, and can only read the one file they pick.

---

## Dropbox (about 5 minutes, the easiest)

1. Go to **dropbox.com/developers/apps** and sign in with the Dropbox account that should own the app (a business account is fine).
2. **Create app**, choose **Scoped access**, then **App folder** (the smallest access), name it `Kauli`.
3. On the app's **Settings** tab, find **Chooser / Saver / Embedder domains** and add `kauli-forgemedia.com`. Save.
4. Copy the **App key** (not the secret).
5. In `.env` add: `DROPBOX_APP_KEY=the-app-key`

The app starts in "Development" mode (up to 50 users can use it). Click **Apply for production** when you are ready for everyone; for the Chooser only this is usually quick.

## OneDrive and Microsoft 365 (about 10 minutes)

1. Go to **portal.azure.com**, then **Microsoft Entra ID > App registrations > New registration**.
2. Name `Kauli`. **Supported account types:** "Accounts in any organizational directory and personal Microsoft accounts".
3. **Redirect URI:** platform **Single-page application (SPA)**, value `https://kauli-forgemedia.com/client`.
4. Register, then copy the **Application (client) ID**.
5. In `.env` add: `ONEDRIVE_CLIENT_ID=the-client-id`

No client secret is needed.

## Google Drive (about 15 minutes)

You already have a Google Cloud organization. Use the `kauli-analytics` project or a new one.

1. **APIs & Services > Library:** enable **Google Picker API** and **Google Drive API**.
2. **APIs & Services > Credentials > Create credentials > API key.** Under "API restrictions" restrict it to **Google Picker API**, and under "Website restrictions" add `https://kauli-forgemedia.com/*`. Copy the key.
3. **Credentials > Create credentials > OAuth client ID** (type **Web application**). Under **Authorised JavaScript origins** add `https://kauli-forgemedia.com`. Copy the **Client ID**. (If you already use Google sign-in with its own Web client, you can add the origin to that one and reuse its ID.)
4. **OAuth consent screen:** the only permission Kauli asks for is `.../auth/drive.file` ("see and download only the files you pick"). This is a non-sensitive permission, so it does not need Google's security review. Set the app to **In production** (Publishing status) so customers outside your organization can use it.
5. The project **number** is on the project's **Dashboard** (not the project ID).
6. In `.env` add:
   ```
   GOOGLE_PICKER_API_KEY=the-api-key
   GOOGLE_OAUTH_CLIENT_ID=the-client-id
   GOOGLE_PICKER_APP_ID=the-project-number
   ```

---

## After you edit `.env`
Restart with `/root/kauli/deploy.sh`, then open **Upload files** in a customer account. You should see **Or import from** with a button per service. Pick a small audio file to test.

## Calendar (no setup needed)
Calendar connections use each calendar's private link, so there is nothing to register. Customers connect at **Integrations > Connect your calendar**, or directly at `/client/calendar`.
