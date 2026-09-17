"""Real, ready-to-use outreach content for the CRM's bulk-researched leads
(NGOs, media houses, and future categories) - staff_lead_detail.html shows
whichever stage matches the lead's own real pipeline status, so
"until we convert them or drop them" is just that status field, not a
separate tracker. Nothing here ever sends on its own: every piece of
content is plain text a person reads, copies, and sends themselves (email,
call, WhatsApp/SMS) - see staff_lead_detail's own "no automation" framing.

Stage mapping reuses the lead's REAL pipeline stage rather than inventing a
parallel "day 0/2/5/10" timer, since a human decides when to move a real
lead forward here, same principle as the activation-nudge automation
question earlier in this project's history:
  new      -> stage 0: an opening question, not a pitch
  contacted -> stage 1: free-sample offer
  qualified -> stage 2: the demo/consultation call, case for value
  proposal  -> stage 3: closing the quote
  won/lost  -> no further stage; the sequence is over either way

Covers every org_type the client-facing "what best describes your
organization?" dropdown offers (marketing.html) - ngo, media_broadcast,
corporate, education, individual - so any lead, bulk-researched or
inbound, gets a real dedicated sequence rather than the generic fallback.
DECISION_MAKERS is real, general business knowledge about which ROLE
typically approves a vendor like this at that kind of organization
(a Program Manager, not "John Mwangi, Program Manager at X") - never a
specific named individual at a specific org, since verifying who
actually holds that title at each of 100+ researched leads isn't
something search results reliably confirm, and a wrong guessed name is
worse than an honest "ask for this role"."""
from __future__ import annotations

STAGE_LABELS = ("Opening question", "Free-sample offer", "Case for value / book a call", "Closing the quote")

DECISION_MAKERS: dict[str, str] = {
    "ngo": "Program Manager, or whoever handles Communications/M&E (Monitoring & Evaluation) - "
           "they're the ones who actually produce field reports and answer to funders in English.",
    "media_broadcast": "Head of Digital/Content, or the Producer of the specific show/segment - "
                        "not general reception. At a bigger house, ask for the Digital Content Manager.",
    "corporate": "Marketing Manager or Head of Communications - whoever owns internal training "
                 "videos, product content, or investor/stakeholder communications.",
    "education": "Dean of Digital Learning, E-learning Coordinator, or the head of the department "
                 "producing the course content - at a university, often sits inside an "
                 "'Open, Distance & e-Learning' (ODeL) office.",
    "individual": "The creator themselves - there's no separate decision-maker to route around.",
}

PLAYBOOKS: dict[str, list[dict]] = {
    "ngo": [
        {
            "email_subject": "Quick question about {company}'s Swahili content",
            "email_body": (
                "Hi there,\n\n"
                "I run Kauli, a Nairobi-based localization service - we turn Swahili/Kikuyu video "
                "and audio into English (and back), AI-drafted then verified word-by-word by a real "
                "human editor before anything ships.\n\n"
                "I noticed {company} does real field/program work, and organizations like yours often "
                "need English versions of Swahili reports for funders or international partners. "
                "I'm curious - how are you currently handling that, if at all?\n\n"
                "No pitch here, genuinely just asking.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Open with the same question as the email - how do they currently handle Swahili-to-English content for funders/partners?",
                "Listen first. Don't mention pricing or Kauli's process unless asked.",
                "If they don't produce Swahili video/audio content at all, that's a real disqualifier - thank them and end the call, don't force a pitch.",
                "If they do: ask roughly how often (monthly volume) and who currently does it (nobody / a freelancer / an agency).",
                "Close by offering the free 2-minute sample - only if the conversation naturally gets there.",
            ],
            "text_message": (
                "Hi, I'm Godfrey from Kauli - we localize Swahili video/audio to English for Kenyan "
                "orgs. Quick q: how does {company} currently handle English versions of Swahili field "
                "reports for funders? Not pitching, just curious about your workflow."
            ),
        },
        {
            "email_subject": "A free sample, on your own content",
            "email_body": (
                "Hi again,\n\n"
                "Following up on my last note - here's a concrete, zero-risk way to see if this is "
                "useful for {company}: send me a 2-minute clip (a field report, training video, "
                "anything real) and I'll return a human-verified English subtitle file within 24 "
                "hours. Free, no card, no commitment.\n\n"
                "If the quality holds up, we can talk about your real volume. If it doesn't, you've "
                "lost nothing.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Reiterate the free-sample offer in one sentence - a real 2-minute clip, 24-hour turnaround, human-verified, no cost.",
                "Ask them to send the clip during or right after the call if possible - momentum matters more than a follow-up email.",
                "Handle 'we already use Google Translate / an intern' - that's exactly the comparison the sample is meant to win.",
                "Set a real expectation: 24 hours, not 'soon' - and actually hit it.",
            ],
            "text_message": (
                "Still happy to do that free 2-minute sample for {company} whenever you have a clip "
                "handy - just send it over and I'll have a human-verified English version back to you "
                "within 24 hours."
            ),
        },
        {
            "email_subject": "What you saw in the sample, and what real volume looks like",
            "email_body": (
                "Hi,\n\n"
                "Now that you've seen the sample - AI-drafted, then a real editor checked every line - "
                "here's what it looks like at real volume for {company}: transparent per-minute "
                "pricing (see kauli-forgemedia.com/pricing), a human editor on every single order "
                "(never just raw AI output), and turnaround measured in hours, not the 4-6 weeks a "
                "traditional agency quotes.\n\n"
                "Worth a 15-minute call to talk through your actual reporting calendar and volume? "
                "Happy to work around your schedule.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "This IS the consultation call - come with their real sample already delivered and referenced by name/topic.",
                "Ask directly: how many reports/videos per month need this treatment? Which languages (Swahili->English, or also Kikuyu)?",
                "Walk through pricing transparently - per-minute rate, no hidden fees, exactly what Rev's own growth lead credited their conversion rate to.",
                "Ask about their internal approval process - who signs off on a new vendor, and what they'd need from you to move forward (a quote, a sample invoice, references).",
                "End with a concrete next step and a date, not 'let me think about it'.",
            ],
            "text_message": (
                "Glad the sample landed well! Want to hop on a quick 15-min call to talk through what "
                "regular volume would look like for {company} - pricing's fully transparent, happy to "
                "work around your schedule."
            ),
        },
        {
            "email_subject": "Your quote - and what happens next",
            "email_body": (
                "Hi,\n\n"
                "Following up on our call - attached/below is a real quote for {company} based on what "
                "you described. No expiry pressure, just want to make sure it's still useful and answer "
                "anything that's come up since we talked.\n\n"
                "If you're ready, the fastest path is just uploading your next real clip at "
                "kauli-forgemedia.com and I'll personally keep an eye on it.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Reference the specific quote/numbers already sent - don't re-pitch from scratch.",
                "Ask directly what's still in the way of a yes - budget approval, a competing quote, timing.",
                "If it's budget: remind them the free sample already proved the quality, so the real question is just volume/cost, which is flexible.",
                "If it's a genuine 'not now': ask permission to check back in 60 days rather than letting the lead go cold silently.",
                "Get a real next step booked before ending the call, even if it's just 'I'll follow up Friday'.",
            ],
            "text_message": (
                "Just sent {company}'s quote over email - let me know if anything's unclear or if it'd "
                "help to jump on a quick call to close out any questions."
            ),
        },
    ],
    "media_broadcast": [
        {
            "email_subject": "How does {company} handle English versions of vernacular content?",
            "email_body": (
                "Hi there,\n\n"
                "I run Kauli, a Nairobi-based localization service - Swahili/Kikuyu video to English "
                "(and back), AI-drafted then verified word-by-word by a real human editor before "
                "delivery.\n\n"
                "I'm curious how {company} currently handles English subtitles or dubs for content "
                "that needs to travel beyond a Swahili/vernacular-speaking audience - syndication, "
                "international distribution, YouTube, that kind of thing. Genuinely just asking, not "
                "pitching yet.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask for the producer or content/digital manager by name if you don't have one yet - the person who decides on subtitling/localization vendors, not general reception.",
                "Open with the same real question - how do they currently handle this, if at all?",
                "Listen for pain points: turnaround time on breaking news content, cost of an agency, inconsistent freelancer quality.",
                "Don't mention price unless asked - the differentiator at this stage is speed + human verification, not cost.",
            ],
            "text_message": (
                "Hi, I'm Godfrey from Kauli - we do Swahili/Kikuyu-to-English localization for video. "
                "Quick q for {company}: how do you currently handle English subtitles for content that "
                "needs to reach beyond a local audience?"
            ),
        },
        {
            "email_subject": "A free 2-minute sample, on a real segment",
            "email_body": (
                "Hi again,\n\n"
                "Concrete way to see if this is useful for {company}: send me a real 2-minute clip - a "
                "news segment, a feature, anything - and I'll return a human-verified English subtitle "
                "file within 24 hours. Free, no commitment.\n\n"
                "If it's fast and clean enough for your workflow, let's talk about a real volume "
                "arrangement. If not, no harm done.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Push for a real clip during the call if possible - broadcast teams move fast, so momentum matters.",
                "Ask what format they'd actually need the output in (SRT burned into video, a separate subtitle file, a dubbed track) so the sample matches their real workflow.",
                "24-hour turnaround is the headline here - repeat it, and actually deliver on time.",
            ],
            "text_message": (
                "Still happy to run that free 2-minute sample for {company} - send a clip whenever, "
                "24-hour turnaround, human-verified."
            ),
        },
        {
            "email_subject": "What real volume and turnaround looks like for {company}",
            "email_body": (
                "Hi,\n\n"
                "Now that you've seen the sample quality - here's what it looks like at real volume: "
                "transparent per-minute pricing, a human editor on every order (never raw AI output "
                "alone), and turnaround in hours for standard content, faster for rush jobs.\n\n"
                "Worth 15 minutes to talk through your actual content calendar and volume? Happy to "
                "work around a newsroom schedule.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask about real volume: how many minutes of content per week/month would realistically need this?",
                "Ask about rush/breaking-news needs specifically - a broadcaster's turnaround expectations are tighter than most clients, worth addressing directly with the rush-order option.",
                "Walk through pricing transparently.",
                "Ask who else needs to sign off (a procurement/vendor process is common at larger media houses) and what they'd need to move forward.",
                "End with a concrete next step and date.",
            ],
            "text_message": (
                "Glad the sample worked! Want to grab 15 minutes to talk through regular volume and "
                "turnaround for {company} - including rush-job options for breaking content?"
            ),
        },
        {
            "email_subject": "Your quote for {company}",
            "email_body": (
                "Hi,\n\n"
                "Following up with a real quote based on what we discussed. No pressure - just want to "
                "confirm it's still useful and answer anything new that's come up.\n\n"
                "Fastest path if you're ready: upload your next real clip at kauli-forgemedia.com and "
                "I'll personally keep an eye on it.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Reference the specific quote already sent.",
                "Ask what's still in the way - budget, a competing vendor, an internal approval process.",
                "If it's a procurement process: offer to provide whatever documentation (a formal quote, references, a sample invoice) their process needs.",
                "If genuinely not now: ask permission to check back in 60 days.",
                "Get a concrete next step booked before ending the call.",
            ],
            "text_message": (
                "Sent {company}'s quote over email - happy to jump on a quick call if it'd help close "
                "out any remaining questions."
            ),
        },
    ],
    "corporate": [
        {
            "email_subject": "How does {company} handle Swahili content for internal/external comms?",
            "email_body": (
                "Hi there,\n\n"
                "I run Kauli, a Nairobi-based localization service - Swahili/Kikuyu video and audio to "
                "English (and back), AI-drafted then verified word-by-word by a real human editor.\n\n"
                "I'm curious how {company} currently handles English versions of Swahili training "
                "videos, product content, or stakeholder communications, if that comes up. Not "
                "pitching yet, genuinely asking about your workflow.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask for Marketing or Communications, not a general switchboard.",
                "Open with the real question - how do they currently handle this, if ever?",
                "Listen for the actual use case: investor updates, internal training, product marketing, customer support content.",
                "Don't pitch price yet - the hook here is speed and consistency vs. an ad-hoc freelancer.",
            ],
            "text_message": (
                "Hi, I'm Godfrey from Kauli - Swahili/Kikuyu-to-English video localization. Curious how "
                "{company} currently handles this for training or comms content, if it comes up."
            ),
        },
        {
            "email_subject": "A free 2-minute sample on your own content",
            "email_body": (
                "Hi again,\n\n"
                "Zero-risk way to check the fit: send a real 2-minute clip (training video, internal "
                "update, anything) and I'll return a human-verified English version within 24 hours. "
                "Free, no commitment.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Offer the free sample concretely - 2 minutes, 24 hours, human-verified, free.",
                "Ask what format their team actually needs (subtitle file, dubbed audio, burned captions).",
                "Push for the clip during the call if you can - corporate approval cycles are slower, so early momentum matters.",
            ],
            "text_message": "Happy to run a free 2-minute sample for {company} - 24-hour turnaround, human-verified, no cost.",
        },
        {
            "email_subject": "What ongoing volume looks like for {company}",
            "email_body": (
                "Hi,\n\n"
                "Now that you've seen the sample - transparent per-minute pricing, a human editor on "
                "every order, and turnaround in hours. Worth 15 minutes to talk through your real "
                "volume (training library, recurring comms, etc.)?\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask about recurring vs. one-off needs - a training library or investor-update cadence is a real repeat-revenue signal.",
                "Ask who else needs to approve a new vendor (procurement, legal for a services contract) and what that process needs from you.",
                "Walk through pricing transparently.",
                "Get a concrete next step booked.",
            ],
            "text_message": "Glad the sample worked for {company}! Want to grab 15 minutes to talk through ongoing volume and how procurement usually works on your end?",
        },
        {
            "email_subject": "Your quote for {company}",
            "email_body": (
                "Hi,\n\n"
                "Following up with a real quote based on our conversation. Happy to adjust the "
                "structure (per-project, monthly retainer) to whatever fits your budgeting cycle.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Reference the specific quote already sent.",
                "Ask directly what's still in the way - budget cycle timing, a competing vendor, an internal procurement step.",
                "Offer whatever documentation their procurement process needs (formal quote, references, a services agreement).",
                "If genuinely not now: ask permission to check back in 60-90 days, matching a corporate budget cycle.",
            ],
            "text_message": "Sent {company}'s quote over email - let me know if it'd help to jump on a call to close out any procurement questions.",
        },
    ],
    "education": [
        {
            "email_subject": "How does {company} localize course content into English?",
            "email_body": (
                "Hi there,\n\n"
                "I run Kauli, a Nairobi-based localization service - Swahili/Kikuyu video and audio to "
                "English (and back), AI-drafted then verified word-by-word by a real human editor.\n\n"
                "I'm curious how {company} currently handles English versions of Swahili course "
                "content or lecture recordings, if that's ever come up - for international students, "
                "accreditation, or just accessibility. Not pitching yet, genuinely asking.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask for the e-learning/ODeL (Open, Distance & e-Learning) office, or whoever runs the LMS, not general admissions.",
                "Open with the real question about their current workflow.",
                "Listen for accessibility/accreditation angles specifically - Kauli's accessibility positioning is a real, relevant hook here.",
                "Ask about scale: how many courses/hours of content exist or get added per term.",
            ],
            "text_message": (
                "Hi, I'm Godfrey from Kauli - Swahili/Kikuyu-to-English video localization, also useful "
                "for accessibility/captioning. Curious how {company} handles this for course content."
            ),
        },
        {
            "email_subject": "A free 2-minute sample on a real lecture clip",
            "email_body": (
                "Hi again,\n\n"
                "Zero-risk way to see the fit: send a real 2-minute lecture/course clip and I'll return "
                "a human-verified English subtitle file within 24 hours. Free, no commitment.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Offer the free sample concretely.",
                "Ask whether they need subtitles only, or a full dubbed English track for the course.",
                "Push for a real clip during the call - academic terms have real deadlines, use that urgency honestly.",
            ],
            "text_message": "Happy to run a free 2-minute sample on a real course clip for {company} - 24-hour turnaround, human-verified.",
        },
        {
            "email_subject": "What a full course library would look like for {company}",
            "email_body": (
                "Hi,\n\n"
                "Now that you've seen the sample - transparent per-minute pricing, a human editor on "
                "every order, real turnaround times. Worth 15 minutes to talk through a real course "
                "library or term-by-term volume?\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask about real scale - how many courses, how many hours per term.",
                "Ask about accreditation/accessibility requirements specifically - a real compliance angle that can justify budget.",
                "Walk through pricing transparently, including any bulk/retainer option.",
                "Ask who approves a new vendor (procurement office, department head) and get a concrete next step.",
            ],
            "text_message": "Glad the sample worked! Want to grab 15 minutes to talk through a full course library for {company}?",
        },
        {
            "email_subject": "Your quote for {company}",
            "email_body": (
                "Hi,\n\n"
                "Following up with a real quote based on our conversation - happy to structure it "
                "around your term/semester calendar.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Reference the specific quote already sent.",
                "Ask what's still in the way - a procurement cycle, budget approval, term timing.",
                "Offer documentation their procurement office needs.",
                "If genuinely not now: ask permission to check back before the next term starts.",
            ],
            "text_message": "Sent {company}'s quote over email - happy to hop on a call if it'd help close things out before term starts.",
        },
    ],
    "individual": [
        {
            "email_subject": "Loved your channel - quick question",
            "email_body": (
                "Hi,\n\n"
                "I run Kauli - we localize Swahili/Kikuyu video into English (and back), AI-drafted "
                "then verified by a real human editor before delivery.\n\n"
                "I came across {company}'s content and think an English version could genuinely grow "
                "your reach to a bigger audience. Is that something you've thought about, or run into "
                "friction with before?\n\n"
                "Godfrey"
            ),
            "call_points": [
                "This is almost always a DM/text/comment first, not a cold call - lead with genuine, specific appreciation for their actual content.",
                "Ask if they've tried subtitling/localization before and what went wrong (cost, quality, turnaround).",
                "Don't pitch price yet - the hook is reach (a bigger, English-speaking audience) not cost savings.",
            ],
            "text_message": (
                "Hey! Really enjoyed your last video - I run Kauli, we do Swahili-to-English "
                "localization for creators. Ever thought about an English version to grow your reach?"
            ),
        },
        {
            "email_subject": "Free sample on your next upload",
            "email_body": (
                "Hi again,\n\n"
                "Here's a no-risk way to test it: send me your next video (or a recent one) and I'll "
                "return a human-verified English subtitle file within 24 hours, free.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Offer the free sample - one real video, 24-hour turnaround, free.",
                "Ask which platform matters most (YouTube captions, TikTok, Instagram) so the sample format actually fits their workflow.",
            ],
            "text_message": "Happy to do a free English subtitle file on your next video - 24hrs, no cost, just want you to see the quality.",
        },
        {
            "email_subject": "What this looks like per video going forward",
            "email_body": (
                "Hi,\n\n"
                "Hope the sample landed well! Per-video pricing is simple and per-minute - most "
                "creators your size land in the $X-Y range per video. Want to talk through making this "
                "a regular part of your upload routine?\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Ask about upload frequency - weekly/monthly cadence tells you real recurring revenue potential.",
                "Keep pricing simple and concrete for an individual, not a corporate-style quote process.",
                "Offer to push captions straight to their YouTube video (Kauli's real caption-push feature) as a frictionless close.",
            ],
            "text_message": "Glad you liked the sample! Want to make this a regular thing for your uploads? Pricing's simple, happy to explain.",
        },
        {
            "email_subject": "Ready when you are",
            "email_body": (
                "Hi,\n\n"
                "No pressure at all - whenever you're ready, just upload your next clip at "
                "kauli-forgemedia.com and I'll personally keep an eye on it.\n\n"
                "Godfrey"
            ),
            "call_points": [
                "Keep this one light - individuals rarely need a hard close, just a low-friction reminder.",
                "Ask directly if anything's still unclear about pricing or the process.",
            ],
            "text_message": "No rush - whenever you're ready, just upload your next clip and I'll take it from there.",
        },
    ],
}

# Real, honest fallback for any org_type without a dedicated sequence yet
# (corporate, education, individual, or anything future) - same structure,
# generic enough to still be useful rather than leaving the section blank.
_GENERIC = [
    {
        "email_subject": "Quick question about {company}'s Swahili/Kikuyu content",
        "email_body": (
            "Hi there,\n\nI run Kauli, a Nairobi-based localization service - Swahili/Kikuyu video and "
            "audio to English (and back), AI-drafted then verified by a real human editor before "
            "delivery.\n\nCurious how {company} currently handles this, if it comes up at all - not "
            "pitching, genuinely asking.\n\nGodfrey"
        ),
        "call_points": [
            "Open with the same real question - how do they currently handle Swahili/Kikuyu-to-English content, if ever?",
            "Listen for a real need before pitching anything.",
            "If there's no real fit, thank them and close - don't force it.",
        ],
        "text_message": "Hi, I'm Godfrey from Kauli - Swahili/Kikuyu-to-English video localization. Curious how {company} currently handles this, if it comes up. Not pitching, just asking.",
    },
    {
        "email_subject": "A free 2-minute sample",
        "email_body": (
            "Hi again,\n\nZero-risk way to see if this is useful: send a real 2-minute clip and I'll "
            "return a human-verified English version within 24 hours. Free, no commitment.\n\nGodfrey"
        ),
        "call_points": ["Offer the free sample.", "Push for a real clip during the call if possible.", "Set the real 24-hour expectation and hit it."],
        "text_message": "Still happy to run a free 2-minute sample for {company} - 24-hour turnaround, human-verified.",
    },
    {
        "email_subject": "What real volume looks like for {company}",
        "email_body": (
            "Hi,\n\nNow that you've seen the sample - transparent per-minute pricing, a human editor on "
            "every order, fast turnaround. Worth 15 minutes to talk through real volume?\n\nGodfrey"
        ),
        "call_points": ["Ask about real volume and cadence.", "Walk through pricing transparently.", "Get a concrete next step booked."],
        "text_message": "Glad the sample landed well! Want to grab 15 minutes to talk through volume for {company}?",
    },
    {
        "email_subject": "Your quote for {company}",
        "email_body": (
            "Hi,\n\nFollowing up with a real quote based on our conversation. Fastest path if ready: "
            "upload your next clip at kauli-forgemedia.com.\n\nGodfrey"
        ),
        "call_points": ["Reference the quote already sent.", "Ask what's still in the way.", "Get a concrete next step or a real 60-day check-back."],
        "text_message": "Sent {company}'s quote over email - happy to hop on a call to close out any questions.",
    },
]

_STATUS_TO_STAGE = {"new": 0, "contacted": 1, "qualified": 2, "proposal": 3}


def get_playbook_stage(org_type: str | None, status: str, company: str) -> dict | None:
    """None once a lead is won/lost (or on an unrecognized status) - the
    sequence is over either way, nothing left to draft. company is
    substituted into every {company} placeholder so what staff sees is
    ready to copy and send as-is, not a template they have to edit first."""
    stage_index = _STATUS_TO_STAGE.get(status)
    if stage_index is None:
        return None
    sequence = PLAYBOOKS.get(org_type or "", _GENERIC)
    stage = sequence[stage_index]
    safe_company = company or "your organization"
    return {
        "stage_number": stage_index + 1,
        "stage_label": STAGE_LABELS[stage_index],
        "total_stages": len(STAGE_LABELS),
        "decision_maker": DECISION_MAKERS.get(org_type or ""),
        "email_subject": stage["email_subject"].format(company=safe_company),
        "email_body": stage["email_body"].format(company=safe_company),
        "call_points": [p.format(company=safe_company) for p in stage["call_points"]],
        "text_message": stage["text_message"].format(company=safe_company),
    }
