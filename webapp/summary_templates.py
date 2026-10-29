"""The summary formats a customer can choose from for a finished transcript.

Each template is a name and the sections ("Key Output Sections") the summary is written in. The summary is written only from the
customer's own transcript; a section the recording does not cover says so instead of being filled in.
"""
from __future__ import annotations

CATEGORIES = ["General", "Sales", "HR", "Health", "Customer Success", "Media & Podcasting", "Marketing", "Education", "Consulting", "Product"]
ICONS = {"General": "✨", "Sales": "🎯", "HR": "🌱", "Health": "🩺", "Customer Success": "👑", "Media & Podcasting": "🎤",
         "Marketing": "📣", "Education": "🎓", "Consulting": "👥", "Product": "🚀"}


def _t(tid: str, name: str, category: str, sections: list[tuple[str, str]], heading: str = "Key Output Sections") -> dict:
    return {"id": tid, "name": name, "category": category, "heading": heading,
            "sections": [{"title": t, "hint": h} for t, h in sections]}


TEMPLATES: list[dict] = [
    # ------------------------------------------------------------------------------------------------ General
    _t("general-meeting", "General Meeting", "General", [
        ("Discussion Topics", "the main subjects and issues talked about"),
        ("Decisions Made", "what was agreed or settled"),
        ("Action Items", "specific tasks, with who owns them and by when, if said")]),
    _t("sprint-planning", "Sprint Planning", "General", [
        ("Sprint Goal", "the single, measurable aim of the sprint"),
        ("Sprint Backlog", "the prioritised work the team committed to, with estimates if given"),
        ("Strategy & Dependencies", "the technical approach and how tasks depend on each other"),
        ("Risks & Assumptions", "what could get in the way of the sprint")]),
    _t("basic-summary", "Basic Summary", "General", [
        ("Main Topic", "what the recording is about"),
        ("Key Points", "the core arguments and main points"),
        ("Overview", "a short, clearly structured recap")], heading="Key Output"),
    _t("board-meeting", "Board Meeting", "General", [
        ("Executive Summary", "the critical outcomes and major shifts in direction"),
        ("Decisions & Motions", "formal approvals, resolutions and sanctions"),
        ("Financial & Operational Highlights", "the key figures and performance points"),
        ("Strategic Discussion", "the high-level strategy and planning that was debated")]),
    _t("standard", "Standard", "General", [
        ("Main Topic / Thesis", "the central subject, message and purpose"),
        ("Key Arguments & Ideas", "the main claims, supporting evidence and how they connect"),
        ("Essential Takeaways", "actionable insights and final conclusions")]),
    _t("brainstorming", "Brainstorming", "General", [
        ("Business Problem", "the central challenge or trade-off"),
        ("Strategic Pillars", "the ideas grouped into dominant themes"),
        ("Decision Criteria & Solution", "how options were judged and what was chosen"),
        ("Critical Gaps", "what is still unexplored or unresolved")]),
    _t("kickoff-meeting", "Kick off Meeting", "General", [
        ("Project Definition", "what the initiative is"),
        ("Scope & Objectives", "what it covers and how success is measured"),
        ("Stakeholders & Context", "who is involved and the situation it starts from"),
        ("Solution & Rationale", "the chosen approach and why")]),
    # ------------------------------------------------------------------------------------------------ Sales
    _t("sales-discovery", "Discovery Call", "Sales", [
        ("Customer Situation", "who they are and what they do today"),
        ("Pain Points", "the problems they described"),
        ("Needs & Goals", "what they want to achieve"),
        ("Budget, Timeline & Decision Makers", "anything said about money, timing and who decides"),
        ("Next Steps", "what was agreed to happen next")]),
    _t("sales-demo", "Product Demo", "Sales", [
        ("What Was Shown", "the features and use cases covered"),
        ("Questions & Reactions", "what the prospect asked and how they responded"),
        ("Objections", "concerns or hesitations raised"),
        ("Next Steps", "follow-ups and who owns them")]),
    _t("sales-negotiation", "Negotiation & Closing", "Sales", [
        ("Offer on the Table", "price, scope and terms discussed"),
        ("Objections & Concessions", "what was pushed back on and what was given"),
        ("Agreed Terms", "what both sides accepted"),
        ("Open Points & Next Steps", "what is left to settle and by when")]),
    # ------------------------------------------------------------------------------------------------ HR
    _t("hr-interview", "Candidate Interview", "HR", [
        ("Candidate Background", "experience and career path as described"),
        ("Skills & Examples", "skills shown and the examples given"),
        ("Strengths", "what stood out positively"),
        ("Concerns", "gaps or doubts that came up"),
        ("Follow-up Questions", "what is still worth asking")]),
    _t("hr-one-on-one", "One-on-One", "HR", [
        ("Updates", "what has happened since last time"),
        ("Feedback", "feedback given in either direction"),
        ("Goals & Growth", "development and career topics"),
        ("Blockers", "what is in the way"),
        ("Action Items", "who will do what")]),
    _t("hr-review", "Performance Review", "HR", [
        ("Achievements", "accomplishments discussed"),
        ("Areas to Improve", "development points raised"),
        ("Goals for Next Period", "targets that were set"),
        ("Support Needed", "help or resources requested"),
        ("Agreed Actions", "commitments made")]),
    # ------------------------------------------------------------------------------------------------ Health
    _t("health-consultation", "Consultation", "Health", [
        ("Reason for Visit", "why the person came"),
        ("Symptoms & History", "what was reported, as said"),
        ("Discussion & Advice", "what the professional explained or advised"),
        ("Plan & Follow-up", "the agreed next steps")]),
    _t("health-team", "Care Team Meeting", "Health", [
        ("Cases Discussed", "who or what was reviewed"),
        ("Decisions", "what the team decided"),
        ("Responsibilities", "who will do what"),
        ("Follow-up", "reviews and dates mentioned")]),
    _t("health-coaching", "Wellness Coaching", "Health", [
        ("Current Situation", "how things are going"),
        ("Goals", "what the person wants to work on"),
        ("Strategies Discussed", "habits and approaches suggested"),
        ("Commitments", "what they agreed to try")]),
    # ------------------------------------------------------------------------------------------------ Customer Success
    _t("cs-onboarding", "Onboarding Call", "Customer Success", [
        ("Customer Goals", "what they want from the product"),
        ("Setup Progress", "what is done and what is left"),
        ("Training Covered", "what was explained or shown"),
        ("Blockers", "problems or risks mentioned"),
        ("Next Steps", "actions and owners")]),
    _t("cs-checkin", "Check-in / Business Review", "Customer Success", [
        ("Results So Far", "outcomes and usage discussed"),
        ("Feedback", "what the customer likes and dislikes"),
        ("Risks & Opportunities", "churn signals or room to grow"),
        ("Action Items", "follow-ups and owners")]),
    _t("cs-escalation", "Support Escalation", "Customer Success", [
        ("The Issue", "what went wrong, as described"),
        ("Impact", "how it affects the customer"),
        ("What Was Tried", "steps already taken"),
        ("Resolution & Next Steps", "what was promised and by when")]),
    # ------------------------------------------------------------------------------------------------ Media & Podcasting
    _t("media-podcast", "Podcast Episode", "Media & Podcasting", [
        ("Episode Summary", "what the episode is about"),
        ("Key Topics", "the main subjects in the order they came up"),
        ("Notable Quotes", "memorable lines, quoted exactly as spoken"),
        ("People & Resources Mentioned", "guests, books, tools and links named")]),
    _t("media-interview", "Interview", "Media & Podcasting", [
        ("Guest & Context", "who is speaking and why"),
        ("Key Insights", "the most useful things said"),
        ("Notable Quotes", "lines worth quoting, exactly as spoken"),
        ("Takeaways", "what the audience should remember")]),
    _t("media-video", "Video / Show Notes", "Media & Podcasting", [
        ("Hook", "the opening idea in a sentence"),
        ("Main Points", "the content in order"),
        ("Call to Action", "anything the speaker asks the audience to do")]),
    # ------------------------------------------------------------------------------------------------ Marketing
    _t("marketing-campaign", "Campaign Planning", "Marketing", [
        ("Objective", "what the campaign should achieve"),
        ("Audience", "who it is for"),
        ("Channels & Messaging", "where it runs and what it says"),
        ("Metrics", "how success is measured"),
        ("Next Steps", "tasks and owners")]),
    _t("marketing-content", "Content Brainstorm", "Marketing", [
        ("Ideas Proposed", "the content ideas that came up"),
        ("Angles & Hooks", "how each could be told"),
        ("Priorities", "what the team chose to do first"),
        ("Owners & Dates", "who will make what, and when")]),
    _t("marketing-research", "Customer Interview", "Marketing", [
        ("Who They Are", "the customer's context"),
        ("Needs & Motivations", "what drives them"),
        ("Objections & Pain Points", "what holds them back"),
        ("Quotes Worth Using", "their words, exactly as spoken")]),
    # ------------------------------------------------------------------------------------------------ Education
    _t("edu-lecture", "Lecture Notes", "Education", [
        ("Topic", "what the lecture covers"),
        ("Key Concepts", "the main ideas explained"),
        ("Definitions & Examples", "terms defined and examples given"),
        ("Review Questions", "questions a student could use to check understanding")]),
    _t("edu-study", "Study Guide", "Education", [
        ("Overview", "what to know in a few sentences"),
        ("Must-Know Points", "the facts and ideas that matter most"),
        ("Common Mistakes", "pitfalls the speaker warned about"),
        ("Practice Questions", "questions based only on what was taught")]),
    _t("edu-discussion", "Class Discussion", "Education", [
        ("Question Explored", "the central question or text"),
        ("Viewpoints", "the different positions taken"),
        ("Conclusions", "what the group settled on"),
        ("Follow-up Reading or Tasks", "anything assigned")]),
    # ------------------------------------------------------------------------------------------------ Consulting
    _t("consulting-workshop", "Client Workshop", "Consulting", [
        ("Context", "the client's situation"),
        ("Findings", "what the discussion revealed"),
        ("Recommendations", "what was proposed"),
        ("Risks", "concerns raised"),
        ("Next Steps", "actions and owners")]),
    _t("consulting-stakeholder", "Stakeholder Interview", "Consulting", [
        ("Role & Perspective", "who they are and how they see it"),
        ("Priorities", "what matters most to them"),
        ("Concerns", "what worries them"),
        ("Requests", "what they asked for")]),
    _t("consulting-status", "Project Status", "Consulting", [
        ("Progress", "what has been completed"),
        ("Issues & Risks", "what is blocking or threatening the work"),
        ("Decisions Needed", "choices waiting on someone"),
        ("Next Period", "what happens next")]),
    # ------------------------------------------------------------------------------------------------ Product
    _t("product-review", "Product Review", "Product", [
        ("Goals", "what the product or release should achieve"),
        ("What Is Working", "positives discussed"),
        ("Problems & Feedback", "issues raised"),
        ("Priorities", "what to do first"),
        ("Action Items", "tasks and owners")]),
    _t("product-research", "User Research", "Product", [
        ("Participant Context", "who the user is"),
        ("Behaviours", "what they actually do"),
        ("Pain Points", "where they struggle"),
        ("Opportunities", "ideas the conversation points to"),
        ("Quotes", "their words, exactly as spoken")]),
    _t("product-spec", "Feature Discussion", "Product", [
        ("Problem", "what the feature is meant to solve"),
        ("Proposed Solution", "the approach discussed"),
        ("Open Questions", "what is unresolved"),
        ("Decisions & Next Steps", "what was agreed")]),
]

BY_ID = {t["id"]: t for t in TEMPLATES}
DEFAULT_ID = "basic-summary"


def get(template_id: str | None) -> dict:
    return BY_ID.get(template_id or "", BY_ID[DEFAULT_ID])


def catalog() -> dict:
    """What the picker needs: the categories in order, each with its templates."""
    return {"categories": [{"name": c, "icon": ICONS.get(c, ""), "templates": [t for t in TEMPLATES if t["category"] == c]} for c in CATEGORIES]}


def instructions(template: dict) -> str:
    """The section list the writer is told to follow."""
    lines = [f"## {s['title']}  ({s['hint']})" for s in template["sections"]]
    return "\n".join(lines)
