"""Your to-do list: order, dated reminders, notifications, progress and the Today card."""
from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import db, notify, todos
from career_agent.web.app import app


def test_order_dates_and_done(fake):
    a = todos.add("Tick your facts", "Start with roles.", href="/profile?tab=facts", check_kind="facts")
    b = todos.add("Fix the three slips in your newest CV")
    c = todos.add("Update the job after it ends", show_from="2027-02-01")
    assert todos.add("Bad date is ignored", show_from="31/01/2027") and \
        db.scalar("SELECT show_from FROM todos WHERE title LIKE 'Bad date%'") is None

    before = todos.lists(today="2027-01-31")
    assert [t["id"] for t in before["now"]][:2] == [a, b]
    assert [t["id"] for t in before["later"]] == [c]
    assert before["now"][0]["progress"] == "0 of 0 facts ticked"
    assert todos.count_now(today="2027-01-31") == 3
    after = todos.lists(today="2027-02-01")
    assert c in [t["id"] for t in after["now"]] and not after["later"]

    todos.move(b, -1)
    assert [t["id"] for t in todos.lists(today="2027-01-31")["now"]][:2] == [b, a]
    todos.set_done(a)
    lst = todos.lists(today="2027-01-31")
    assert a not in [t["id"] for t in lst["now"]] and lst["done"][0]["id"] == a
    todos.set_done(a, False)
    assert a in [t["id"] for t in todos.lists(today="2027-01-31")["now"]]


def test_a_reminder_notifies_once_on_its_day(fake, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, message: sent.append((title, message)))
    todos.add("Update the job after it ends", show_from="2027-02-01")
    todos.add("No date, no notification")
    assert todos.notify_due(today="2027-01-31") == 0
    assert todos.notify_due(today="2027-02-01") == 1 and sent == [("Reminder", "Update the job after it ends")]
    assert todos.notify_due(today="2027-02-02") == 0  # once only


def test_today_shows_the_list_first_and_escapes_text(fake):
    todos.add("Fix `cv.md` <script>alert(1)</script>", "The CV says **Jul**; it should be Aug.")
    todos.add("Later thing", show_from="2099-01-01")
    c = TestClient(app)
    page = c.get("/").text
    assert page.index('id="todo"') < page.index("Get set up")  # above everything else, setup included
    assert "Do this first" in page and "<code>cv.md</code>" in page and "<script>alert(1)</script>" not in page
    assert "<strong>Jul</strong>" in page and "Coming up" in page and "1 Jan 2099" in page
    assert '<span class="count"' in page  # the count next to Today in the menu

    r = c.post("/todos/add", data={"title": "Call the recruiter", "show_from": ""}, follow_redirects=False)
    assert r.status_code == 303
    tid = db.scalar("SELECT id FROM todos WHERE title = 'Call the recruiter'")
    c.post(f"/todos/{tid}/done", follow_redirects=False)
    assert db.scalar("SELECT done_at FROM todos WHERE id = ?", (tid,))
    c.post(f"/todos/{tid}/undo", follow_redirects=False)
    assert db.scalar("SELECT done_at FROM todos WHERE id = ?", (tid,)) is None
    c.post(f"/todos/{tid}/delete", follow_redirects=False)
    assert not db.scalar("SELECT COUNT(*) FROM todos WHERE id = ?", (tid,))
    empty = c.post("/todos/add", data={"title": " "}, follow_redirects=False).headers["location"]
    assert "err=" in empty and "title" in empty and not db.scalar("SELECT COUNT(*) FROM todos WHERE title = ''")


def test_the_ai_repeat_reminder_counts_what_the_check_set_aside(fake):
    from fastapi.testclient import TestClient

    from career_agent import jobs
    from career_agent.web.app import app

    tid = todos.add("Check the AI repeat decisions", href="/jobs?view=same-ad", check_kind="same_job")
    item = db.q1("SELECT * FROM todos WHERE id = ?", (tid,))
    assert todos.progress(item) == "0 jobs set aside by the AI repeat check so far"
    first = db.insert("jobs", {"url": "https://x.example/1", "title": "IT-supportkonsulent", "status": "new", "created_at": db.now()})
    copy = db.insert("jobs", {"url": "https://x.example/2", "title": "IT-supportkonsulent til MSP", "status": "new", "created_at": db.now()})
    jobs.mark_repeat(copy, {"id": first}, "a model read both ads and judged them the same opening (same team)")
    assert todos.progress(item) == "1 job set aside by the AI repeat check so far"
    page = TestClient(app).get("/jobs?view=same-ad").text
    assert "IT-supportkonsulent til MSP" in page and "Set aside by the AI repeat check." in page
    assert ">IT-supportkonsulent</a>" not in page
