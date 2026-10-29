# Google Analytics for Kauli: set it up once, read it every week

**What is already in place**
- The site sends data to Google Analytics 4 (web stream "Kauli Marketing Site", measurement id **G-29L6CFPNEB**) from all public pages, **only after the visitor accepts the cookie banner**.
- Server-side events are sent for people who accepted analytics: `sign_up`, `login`, `generate_lead`, `order_submitted`, `purchase`.
- **New in this release** (also only after consent):
  - Visitor events: `scroll_depth` (25/50/75/100%), `view_pricing`, `video_play`, `video_complete`, `form_start`, `contact_click` (email clicks), `cta_click` (with the button's `cta_location`), and for the interactive demo `demo_play`, `demo_scenario`, `demo_language`.
  - Product-use events from the customer account: `transcript_download` (format), `transcript_share`, `note_created`, `vocabulary_added`, `assistant_question`, `assistant_escalated`, `calendar_interest`, `integration_request`, `referral_visit`.
  - None of them carry names, emails, file names or transcript text.
- Inside Kauli: **Staff > Site analytics** shows the Google numbers (once connected below) plus our own orders, payments, upload steps and blog readers.

---

## Part 1: Switch on what makes the numbers useful (about 15 minutes, in analytics.google.com)

Open the **Kauli property**, then **Admin** (the gear, bottom left).

1. **Server events need a secret.** Admin > Data streams > your web stream > **Measurement Protocol API secrets** > Create. Put the value in the server `.env` as `GA_API_SECRET`. Without it, `sign_up`, `purchase` and every product-use event are silently dropped. (If you already did this earlier, nothing to do.)
2. **Key events** (these used to be called conversions). Admin > **Events**. After each event has appeared once, switch **Mark as key event** on for: `sign_up`, `generate_lead`, `order_submitted`, `purchase`. Optional extras: `transcript_download`, `note_created` (customers using the product).
3. **Custom definitions**, so the details show up in reports. Admin > **Custom definitions > Create custom dimension**. Scope: **Event**. Create these (name, then the event parameter):
   | Dimension name | Event parameter |
   |---|---|
   | CTA location | `cta_location` |
   | Scroll percent | `percent` |
   | Page path (event) | `page_path` |
   | Video | `video` |
   | Form | `form` |
   | Demo scenario | `scenario` |
   | Demo language | `language` |
   | Download format | `format` |
   | Contact method | `method` |
   | Note source | `source` |
   | Integration provider | `provider` |
   New dimensions fill from the day you create them, not backwards.
4. **Enhanced measurement.** Admin > Data streams > web stream > **Enhanced measurement**: keep page views, scrolls, outbound clicks, site search, video engagement and file downloads on.
5. **Hide your own team's visits.** Data streams > web stream > Configure tag settings > **Show all** > **Define internal traffic**: add your office or home IP. Then Admin > **Data filters**: the "Internal Traffic" filter is created as **Testing**; switch it to **Active** after you check it in DebugView.
6. **Keep the history.** Admin > Data collection and modification > **Data retention** > set event data to **14 months**.
7. **Link Google Search Console** (shows which Google searches bring people to the site). Admin > Product links > **Search Console links** > Link. Then in Reports, open Library and add the Search Console collection.
8. **Check it works.** Admin > **DebugView**, then visit the site yourself with the cookie banner accepted and click around: events should appear within seconds.

---

## Part 2: Show the numbers inside Kauli (about 10 minutes)

1. **Google Cloud** (console.cloud.google.com): create a project (any name), then APIs & Services > Library > turn on **Google Analytics Data API**.
2. APIs & Services > Credentials > **Create credentials > Service account**. Then open it > **Keys > Add key > JSON**. A file downloads; treat it like a password.
3. In Google Analytics: Admin > **Property access management** > add the service account's email (it ends `.iam.gserviceaccount.com`) with the role **Viewer**.
4. Admin > **Property details**: copy the numeric **Property ID**.
5. On the server `.env` add:
   ```
   GA4_PROPERTY_ID=123456789
   GA4_SERVICE_ACCOUNT_FILE=/path/on/server/service-account.json
   ```
   (or paste the whole JSON on one line into `GA4_SERVICE_ACCOUNT_JSON`). Restart the app.
6. Open **Staff > Site analytics**. Numbers are cached for 10 minutes; the **Refresh** chip forces an update.

The key only has **read-only** access, and Kauli never shows individual visitors, only totals.

---

## Part 3: The weekly check (15 minutes, same day each week)

Open **Staff > Site analytics**, set **28 days**, and go through it top to bottom. The page lists "What to look at" for you; here is the logic behind each angle and what to do.

| Angle | Where | What to look for | What to do |
|---|---|---|---|
| **Traffic** | Visitors tile and the daily chart | Down more than 15% vs the previous period | Check which channel dropped; check search rankings |
| **Where they come from** | Channels, Top sources | A channel with many visits and **0 key events** | Match the landing page to what brought them, or stop paying for it |
| **First impression** | Landing pages | Low "Engaged" % on your top landing page | Rewrite the headline and first screen; speed |
| **Content** | Most viewed pages, Top blog posts | Pages with many views but a few seconds each | Rework the opening, add a clear next step |
| **Conversion funnel** | "From visit to payment" | The biggest drop between two steps | Fix that step first: start click to account means the sign-up page; account to order means the upload form |
| **Upload form** | Upload steps (inside Kauli) | A big drop between steps | Simplify that step |
| **Audience** | Countries | US share (our focus market) | Aim posts, ads and wording at US searches |
| **Devices** | Devices | Mobile engaged % far below desktop | Test the pages on a phone |
| **Interest in products** | Events table: `view_pricing`, `demo_play`, `video_play` | Many pricing views but few sign-ups | Clarify pricing; add proof near the plans |
| **Product use** | Events table: `transcript_download`, `note_created`, `assistant_question` | Customers not using the features we built | Show the feature in onboarding emails |
| **Support signals** | `assistant_question`, `assistant_escalated` | Many escalations | Add the question to the assistant's help material |

**A few habits that keep the data honest**
- Compare like with like: this 28 days against the previous 28.
- Don't judge a change in less than 2 weeks.
- Use UTM links on everything you post: `?utm_source=youtube&utm_medium=video&utm_campaign=promo` (lowercase, no spaces). Then "Top sources" shows exactly which post or video brought each visitor.
- For the promo video: put the link `https://kauli-forgemedia.com/?utm_source=youtube&utm_medium=video&utm_campaign=promo` in the description and pinned comment.

---

## Part 4: Deeper reports in Google Analytics itself

- **Reports > Acquisition > Traffic acquisition**: channels with engagement and key events.
- **Reports > Engagement > Pages and screens**: every page, and **Events** for the new events.
- **Explore > Funnel exploration**: build the funnel `cta_click` > `sign_up` > `order_submitted` > `purchase` and break it down by device or channel.
- **Explore > Path exploration**: what visitors do after the home page.
- **Audiences**: create "Viewed pricing but did not sign up" (`view_pricing` and not `sign_up`) to use for later remarketing, if you ever advertise.
- **Looker Studio** (lookerstudio.google.com): connect the Google Analytics source for a shareable dashboard. Start from the "GA4 report" template.

---

## Privacy
- Analytics only loads after the visitor accepts cookies, and the server only sends events for people who carry the analytics cookie. The cookie policy at `/privacy#cookies` already says this; it covers the new events because they are the same kind of data (page and interaction counts).
- Never put a person's name, email, file name or transcript text into an event. The new product-use events send only a kind or a count.
