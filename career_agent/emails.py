"""Emails you send yourself, ready to copy: the one your application goes with, asking whether a job is still open,
and a follow-up when they haven't answered. Each comes with who to send it to.

They're short, plain templates in the application's language (Danish or English), so they're there at once without
a model; the follow-up can also be written in your voice by a model (Timeline tab), and then that one is shown.
Nothing is sent: you copy the text, or open it as a draft in Mail and press Send yourself."""
from __future__ import annotations

import re
from datetime import date

from . import settings

DA_MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli", "august", "september", "oktober",
             "november", "december"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]


def _lang(code: str | None) -> str:
    return "da" if (code or "").lower().startswith("da") else "en"


def _day(value: str | None, lang: str) -> str:
    """'22. november' / '22 November', with the year when it isn't this year."""
    try:
        d = date.fromisoformat((value or "")[:10])
    except ValueError:
        return ""
    year = f" {d.year}" if d.year != date.today().year else ""
    return f"{d.day}. {DA_MONTHS[d.month - 1]}{year}" if lang == "da" else f"{d.day} {EN_MONTHS[d.month - 1]}{year}"


def first_contact(name: str | None) -> str:
    """The first person when the ad names several ("Jacob Lindkvist (IT-chef); Helle Gregersen (HR-direktør)")."""
    first = re.split(r";|,|/|\bog\b|\band\b", re.sub(r"\([^)]*\)", " ", name or ""))[0]
    return " ".join(first.split())


def _greeting(lang: str, contact: str | None) -> str:
    who = first_contact(contact)
    if lang == "da":
        return f"Hej {who.split()[0]}" if who else "Hej"
    return f"Dear {who}," if who else "Hello,"


def _signature(lang: str) -> str:
    p = settings.get("profile")
    lines = ["Med venlig hilsen" if lang == "da" else "Best regards", (p.get("name") or "").strip(), (p.get("phone") or "").strip()]
    return "\n".join(x for x in lines if x)


def _to(row: dict) -> dict:
    """Who to send it to: the ad's contact address, or what there is instead."""
    email = (row.get("contact_email") or "").strip()
    phone = (row.get("contact_phone") or "").strip()
    name = first_contact(row.get("contact_name"))
    out = {"to": email, "name": name, "phone": phone, "link": row.get("apply_url") or row.get("job_url") or row.get("url") or ""}
    if not email:
        out["no_address"] = ("The ad names no email address" + (f", but a phone number: {phone}" if phone else "")
                             + ". Use the employer's general address or the application page.")
    return out


def application(a: dict) -> dict:
    """The email your letter and CV go with. `a` is an application with its job (tracker.app_with_job)."""
    lang = _lang(a.get("language"))
    title, company = a.get("job_title") or "", a.get("company_name") or ""
    at = (f" hos {company}" if lang == "da" else f" at {company}") if company else ""
    line = (f"Hermed min ansøgning og mit CV til stillingen som {title}{at}." if lang == "da"
            else f"Please find attached my cover letter and CV for the {title} position{at}.")
    subject = a.get("subject") or (f"Ansøgning: {title}" if lang == "da" else f"Application: {title}")
    return {**_to(a), "subject": subject, "body": f"{_greeting(lang, a.get('contact_name'))}\n\n{line}\n\n{_signature(lang)}"}


def still_open(job: dict) -> dict:
    """Asking whether the job is still open: the ad is gone, couldn't be read, or has been up for months."""
    lang = _lang(job.get("language"))
    title = job.get("title") or ""
    if lang == "da":
        subject = f"Er stillingen som {title} stadig ledig?"
        ask = f"Jeg har set jeres opslag om stillingen som {title} og vil gerne søge den. Er stillingen stadig ledig?"
        if not job.get("deadline"):
            ask += " Og hvis den er, er der så en ansøgningsfrist?"
    else:
        subject = f"Is the {title} position still open?"
        ask = f"I saw your ad for the {title} position and would like to apply. Is the position still open?"
        if not job.get("deadline"):
            ask += " And if it is, is there a deadline for applications?"
    return {**_to(job), "subject": subject, "body": f"{_greeting(lang, job.get('contact_name'))}\n\n{ask}\n\n{_signature(lang)}"}


def followup(a: dict) -> dict:
    """A short follow-up after a while without an answer. A follow-up a model wrote in your voice is used when there
    is one ("Subject: …" and the text, as writer.followup keeps it)."""
    lang = _lang(a.get("language"))
    title = a.get("job_title") or ""
    out = _to(a)
    drafted = (a.get("followup_draft") or "").strip()
    if drafted:
        head, _, body = drafted.partition("\n\n")
        subject = head.removeprefix("Subject:").strip() if head.startswith("Subject:") else ""
        return {**out, "subject": subject, "body": body.strip() if subject else drafted, "by_model": True}
    sent = _day(a.get("applied_at"), lang)
    if lang == "da":
        subject = f"Opfølgning på min ansøgning: {title}"
        text = (f"Jeg sendte min ansøgning til stillingen som {title}" + (f" den {sent}" if sent else "") + ". "
                "Jeg er stadig interesseret og vil gerne høre, hvor langt I er i processen, og hvornår jeg kan forvente "
                "at høre fra jer.")
    else:
        subject = f"Following up on my application: {title}"
        text = (f"I sent my application for the {title} position" + (f" on {sent}" if sent else "") + ". "
                "I'm still interested, and I'd like to know where you are in the process and when I can expect to "
                "hear from you.")
    return {**out, "subject": subject, "body": f"{_greeting(lang, a.get('contact_name'))}\n\n{text}\n\n{_signature(lang)}"}
