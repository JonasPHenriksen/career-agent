# Career Agent

A personal job-search agent that runs on your Mac. It learns what you've actually done and how you
write from your own documents, finds and ranks jobs, drafts applications and tailored CVs in your
voice, fact-checks every claim against your real experience, and tracks how each employer treats you.

You stay in charge of one thing: nothing is ever sent without you pressing Send.

## Start it

```bash
./agent start
```

It starts the app and opens <http://127.0.0.1:8765>; Ollama (the local models) is started only when a step is
set to a local model. The first run installs what it needs. When you're done:

```bash
./agent stop
```

That stops the app, unloads the models and quits Ollama, so nothing is left running. `./agent status` shows what
is running.

To let it work through everything on its own and then switch itself off:

```bash
./agent run
```

It starts everything, checks your job sources, reads and scores new jobs, writes drafts for strong matches, and
stops everything when the queue is empty (or after 6 hours: `--max-hours 2` changes that). Review the results the
next time you start it. Stopping in the middle of work is safe: unfinished tasks go back in the queue and run on the next start.
While it runs, the Mac is kept from idle-sleeping so long jobs can finish (closing the lid still sleeps it).

To end a long job for good, `./agent cancel` drops everything that is waiting in the queue and stops (the Tasks
page has the same button). To open the app without it starting any work, switch on **Hold all automatic work**
under Settings → Automation: no job sources are checked and no model is used until you press Resume in the banner
at the top. What you start yourself still runs.

The **Today** page walks you through setup:

1. **AI model.** Your Mac already has Ollama with Gemma 4 26B, so it works out of the box, fully
   offline. For the best letters, use Claude through your Claude subscription (see below).
2. **About you.** Name and contact details (printed on your documents, never sent to the AI),
   target roles, locations, deal-breakers, things to always or never mention.
3. **Import your documents.** Drop files (PDF, Word, PowerPoint…), or import a whole folder (`~/Documents/Job applications`).
   CVs, every application you've written, reference letters, reports, and emails you sent (Apple
   Mail → Mailbox → Export Mailbox gives an `.mbox` folder; only emails *you* wrote are used).
4. **Check your career database.** Every fact is extracted with a quote from the source document.
   Tick the correct ones, fix or hide the rest. Mark how old applications went (interview, rejected…):
   the ones that got interviews become the main examples of your voice.
5. **Add jobs.** Paste a job link, or add a job-search feed so jobs arrive automatically.

## Your to-do list

The top of **Today** lists what only you can do, numbered in order ("Do this first"). Tick an item off with
**✓ Done**, move it up or down, or add your own. An item with a date waits under **Coming up** and moves into the
list on that day, with a macOS notification. Some items show live progress, e.g. how many facts you've ticked.
The count next to *Today* in the menu is how many are waiting on you.

## What happens automatically

```
job sources ──► quick screening ──► read the ad ──► fit score (0-100, with matches, gaps, best angle)
                                                        │
                       strong match (≥ 80, max 5 a day) ▼
                        letter + tailored CV in your voice ──► fact check + voice check
                                                        │
                                   you review, edit, ask for changes ("make it shorter")
                                                        │
                    PDF / Word, open the application page or a Mail draft ──► you send it
                                                        │
             tracking: follow-up reminders, paste replies to log them, "no response" after 30 days
                                                        │
                company grades: answer rate, reply speed, real feedback, ghosting, re-posted jobs
```

* **Job sources.** For Jobindex, run a search on jobindex.dk and paste the results page address in
  Settings → Job sources (it becomes an RSS feed automatically). Any RSS/Atom feed works, and so do
  most job sites' search pages (the agent picks out the job links; a page that spreads its jobs over several
  pages is followed through its own page links while they show new jobs). Large employers that skip the job
  boards can be watched on their own career sites: a search page like Vestas', or a Workday site such as
  LEGO's (`…myworkdayjobs.com/<site>`, with its filters in the address). The app checks each site's robots.txt
  and won't add an address the site doesn't allow robots to read. On Jobindex that allows up to two search
  words and a region (`area=storkbh`), but not distance (`radius=`), municipality (`geoareaid=`) or paging
  (`page=`), so a Jobindex feed shows the newest 20 jobs. LinkedIn needs a login, so paste
  LinkedIn job links (or the ad text) by hand.
* **Collect jobs from a page you have open.** A job board's filtered search (category, distance, page 2 and
  on) is closed to robots, and Jobnet has no feed, so the app can't watch them. Drag the *Collect jobs* button
  from Settings → Job sources to your browser's Favourites bar, run the search yourself, and click the bookmark.
  One click takes the whole search: on Jobindex and IT-Jobbank it reads every page of results from your own tab
  (up to 50 pages, a little apart), on Jobnet it presses "Indlæs flere job" until all the jobs show, and you press
  *Send to Career Agent* when it's done. The app fetches nothing from the site: it reads the jobs (Jobindex,
  IT-Jobbank and Jobnet by their layout; any other site after asking, with the same link-picking as a watched
  page), drops the ones it already has and screens the rest. It carries a private key, so a website can't feed
  the app listings on its own. Save the searches you run each week under Settings → Job sources (or from the
  page that opens after collecting): a to-do on Today lists them with links and comes back when one hasn't been
  collected for a week. After each *Send*, the page that opens links straight to the next search still to collect,
  so the weekly run is one search after another.
* **Kinds of job you don't want.** Under Profile → About you, switch off student jobs, internships, part-time,
  temporary, freelance, research posts or volunteer work. Those are set aside by their title before any model
  sees them, so they cost no time or tokens. A job you added by hand, chose to keep or have an application for
  is never set aside.
* **Ads that turn down AI-written applications.** Some ads say that applications written with AI or ChatGPT
  are rejected, or ask you to write it yourself. The application page and the job page then show a warning
  with the ad's own sentence, so you write that letter yourself.
* **Honesty guardrails.** The writer may only use facts from your career database; it never invents
  employers, titles, dates, numbers or skills. A separate fact check then marks every claim in the
  letter and CV as backed, stretched or unsupported, so you fix problems before an interviewer finds them.
* **Your voice.** A style guide is written from your own texts, your best past applications are used
  as examples, and every draft is compared with your own sentence length, paragraphing and phrasing.
  Stock phrases you never use ("passionate about", "brænder for", em dashes…) are flagged. With the fact
  check, a model reads the letter next to your style guide and marks the sentences that don't sound like
  you, each with a plainer version; tick the ones you want and the letter is revised with them. A letter
  that hasn't changed isn't read again.
* **Employers are graded too.** Paste a reply and it's classified (template rejection, real feedback,
  interview invite…). Silence is counted. Companies that ghost you or re-post the same job get a low
  grade, and you can block them from automatic drafting.
* **Interview prep.** When you get an interview: likely questions with answers built from your real
  experience, the gaps they may probe, every claim you made, and questions to ask them.

## Your CV, tailored and typeset

The CV for a job is your own CV with small changes, not a new one. Under Profile → **Your CV**:

* **Pick the base.** Choose one of your imported CVs. It is read once into data, word for word, and you can correct
  the result. Switch to another CV at any time; new applications start from the one in use.
* **One base per language.** With a base in Danish and one in English, an application starts from the base in its
  own language, so nothing is translated and the wording is the one you approved. For any other language the one
  in use is translated. Click a base in the list to show, check and correct it.
* **Small changes per job.** The most relevant points and skills first, a point left out, the headline and profile
  turned towards the job, translated if the ad is in another language. Employers, titles, dates and numbers never
  change, and every change is listed in the draft's notes.
* **Typeset in the app.** The CV is laid out on this Mac in two columns with a coloured sidebar. Colour, font, text
  size, sidebar width and the order of the sections are settings on the same page. The application page shows the
  pages, and gives you the PDF and a JSON Resume file that tools like rxresu.me can import.
* **The page is filled.** The lines and the gaps between sections open up as far as they can without adding a page,
  so a CV doesn't end in a blank band, and a CV with more points gets tighter lines instead of a second page. Your
  name is kept on one line, whatever the font.
* **Your photo.** Add a picture under Profile → Your CV and it is printed at the top of every CV, cropped to a
  square. When a job ad asks for applications without a photo, or says they are read anonymously, that job's CV is
  made without it and the list of changes says so. The picture never goes to a model.
* **Your contact details stay here.** Name, email, phone and links come from About you when the CV is typeset; they
  are taken out of a CV's text before a model reads it.

The font is IBM Plex Serif if it's installed on this Mac (free from fonts.google.com), otherwise Charter.

## Retrieval: the right facts for each job

Every document is split into passages and indexed on this Mac, by keywords and by meaning (a local
multilingual embedding model, `bge-m3` in Ollama, so Danish and English match). That's used to:

* **Pick the facts that matter for each job.** When your whole career database doesn't fit the model's
  budget (mostly with local models), every role, degree, certification and language is always kept, and
  the projects, skills and things you say about yourself most relevant to the ad fill the rest.
* **Choose example letters** that worked and are closest to the job.
* **Double-check the fact check.** Before a claim is called unsupported, your original documents are
  searched. If a passage backs it, you see where, and can add it to your career database in one click.
* **Read long documents in parts**, so nothing past page 10 is skipped.
* **Search your documents yourself:** Profile → Documents → *Search your documents*.

Without the embedding model it falls back to keyword search. Settings → Search & fact check has a
download button (about 1 GB), or run `ollama pull bge-m3`.

## Keep it in sync with Nextcloud

Profile → Documents → **Keep in sync**:

* **Watch a folder on this Mac.** If the Nextcloud desktop app syncs to `~/Nextcloud`, watch the
  subfolders with your career documents (not everything). Folders are checked every 30 minutes: new
  files are read, a changed file replaces its old version (facts you've marked ✓ are kept), and files
  you delete stay in your database. The first time, macOS may ask to allow access to the folder.
* **Or connect Nextcloud directly** (no desktop app): paste the folder's address from Nextcloud in your
  browser, your username and an app password (Nextcloud → Settings → Security → Devices & sessions).
  The password goes into your macOS Keychain; files are copied into the data folder and read from there.

## The agent folder: summaries for local models

A small local model can't read your whole career at once. An **agent folder** gives it the big picture in a
few pages. The app looks for `Agents` in your Nextcloud folder (or set another path under Profile → Documents →
Agent folder):

```
Agents/                       for any AI agent: about you, a map of your files, notes
└── Career Agent/
    ├── profile.md            one page: who you are, now and before, strengths, what you're after
    ├── experience/*.md       one summary per education, job, internship, project, free time
    ├── writing-style-da.md   how you write in Danish, with real sentences of yours
    ├── writing-style-en.md
    ├── facts.jsonl           facts with their source file and a verbatim quote (importable)
    └── sources.jsonl         every source file: what it is, whether you wrote it alone
```

* **When writing**, the letter writer gets the profile and the summaries most relevant to the job (three with a
  local model, six with Claude), and the folder's style guide for the letter's language instead of the app's own.
* **They're never evidence.** The writer may only claim what your career database backs, and the fact check
  compares every sentence with the database and your own documents, not with the summaries.
* **Import facts from the folder** (Profile → Documents) brings in `facts.jsonl`: each fact with its quote, plus
  its source documents, already sorted (your own texts, group work, other people's), so the local model doesn't
  spend hours reading them again. Facts stay unticked until you check them, and importing twice is safe.
  Past applications are stored up to the sign-off and your name: a Google Docs export with several tabs (notes,
  the job ad, private notes) keeps only the letter. A source marked `"use_for_style": false` (e.g. old template
  letters) still gives facts but isn't used as an example of your voice.
* **Edit freely.** It's plain Markdown: fix anything in any editor, on any device, and the next letter uses it.
* The folder is never imported as documents, even when you watch your whole Nextcloud: summaries of your
  documents aren't your documents.

When the app reads documents itself, it now recognises course material (lecture slides, reading material,
assignment texts), which adds no facts, and only texts you wrote alone count as examples of your style.

To write or refresh an agent folder from your own files with AI subagents, and check it before publishing, see
[scripts/agent-folder/README.md](scripts/agent-folder/README.md).

## Running fully local (no cloud)

It already can. With Ollama running, Settings → AI models → pick your local model for both
**Writing** and **Analysis**, then tick **Local only**: cloud models are blocked and nothing leaves
the Mac (except fetching public job ads).

Measured on this MacBook Pro (M1 Pro, 32 GB) with `gemma4:26b-mlx`: about 300 tokens/s reading and
25 tokens/s writing, so roughly 30 seconds per imported document, under a minute to read and score a
job, and a few minutes per full application (letter, CV, fact check). Local work runs one task at a time
in the background.

**Best local setup on this Mac (tested blind, see the report):** `gemma4:31b-mlx` for Writing and
`gemma4:26b-mlx` for Analysis. The dense 31B writes noticeably better letters (about 4 minutes per letter or CV);
the 26B is a mixture-of-experts model that is much faster for reading documents, scoring jobs and fact-checking,
and checks as well as the 31B. With both installed the app picks this automatically. To add the 31B:

```bash
ollama pull gemma4:31b-mlx
```

Tips:
* Any model you pull with Ollama shows up in Settings. On 32 GB, models up to roughly 30B parameters
  (quantised) run; the 31B uses 21–27 GB while running, so close heavy apps while it writes.
* **Context size** (Settings → local model options) defaults to 32k tokens and grows automatically up to
  64k for big requests. With hundreds of facts, raising it helps.
* A good middle ground: **Claude for Writing, the local model for Analysis.** Your documents are read
  locally; only the letter-writing context goes to Claude. The fact check runs on the Writing model by
  default (Settings → Search & fact check), since it's your safety net.

## Using Claude.ai by copy and paste (Free or Pro plan)

No per-call costs: the app prepares everything and you paste it into your normal Claude.ai chat.

1. On a job, click **Write with Claude.ai** (or open an application's **Claude.ai** tab).
2. Click **Copy prompt**, open Claude.ai, paste it into a new chat and send. The prompt holds your career
   facts, style guide, best past letters, the job ad, and the rules for honesty, gaps and dates.
3. Click the copy button under Claude's reply, paste the whole answer into the app, and click **Use this answer**.

The letter and CV become the new draft (the previous one stays under Versions), and the fact check and voice
check run locally as usual. The Free plan gives you Claude's Sonnet; with Pro you can choose Opus for the best
letters. Usage is limited per 5 hours and per week on both, which is plenty for a few applications a day. What
goes into the chat: your career facts, style guide, example letters and the ad. ID and bank numbers are removed,
and your contact details are never included (they're added to the PDFs locally).

## Switching models, and redoing work

* **Interrupted? Nothing is lost.** Tasks that were running when the app stopped go back in the queue and run
  again on the next start, and results are only saved when a task finishes.
* **It waits for what it needs.** If Ollama isn't running, the app starts it; if this Mac is offline, or your
  Claude plan's usage limit is reached, the tasks that need it wait and carry on when it's back, without failing.
  Tasks → *waiting* shows what for. A passing hiccup (a server restarting, a site not answering) is tried again
  after 2, 10 and 30 minutes before it's reported as failed.
* **Each start catches up.** Work an earlier session couldn't finish for one of those reasons is queued again,
  and job sources that failed are checked right away.
* **Switch any time** in Settings → AI models: the next task uses the new model.
* **A model and an effort for every step.** Settings → *Model and effort for each step* lists what the app does
  (screening, reading ads, scoring, reading documents, the letter, the CV, the fact check, the style guide,
  interview preparation, reading replies). Each uses the Writing or Analysis model unless you pick another, and
  each can have its own effort (low to max: how carefully a Claude model works; on a local model, high or above
  switches thinking on).
* **For one run:** every button that starts work has a model and an effort picker next to it, and *Start over* can
  write the letter or the CV again alone.
* **See who made what.** Drafts, fact checks, job readings and scores, documents and the style guide say which
  model made them ("Read by gemma4:26b-mlx (local)").
* **Redo one thing** with the picker next to its button (New draft, Run fact check, Score again, Read the ad
  again, Analyse again, Rebuild): any installed model or Claude, without changing your settings. Earlier drafts
  stay under Versions. A document read again with another model replaces its unticked facts and keeps the ones
  you've ticked or hidden.
* **Redo in bulk:** Settings → *Redo work with another model*, e.g. everything `gemma4:26b-mlx` made, with Claude.

### Claude through Claude Code (your subscription)

With Claude Code installed (the Claude desktop app brings its own copy), pick *Claude through Claude Code* under
Settings → AI models. It uses the Claude plan you're logged in with, so there's no bill per call: calls count against your plan's limits, and tasks wait and retry when you hit one. Log in once in Terminal
(Settings shows the exact command; then type `/login`). The app runs it with every tool switched off, without
plugins, connectors or saved sessions, in an empty folder, so it can only answer. *Local only* blocks it like any
cloud model, and ID and bank numbers are removed first.

The choices name exact versions, so you always know which model did the work: the newest of each kind (Claude
Opus, Sonnet, Haiku, Fable) first, and the older versions your plan still runs under *earlier versions*. The list
follows your subscription: the app reads the model IDs from the installed Claude Code (which updates itself and
learns about new models), asks each one for a single word through your plan, and keeps the ones that answer as
themselves. That check runs by itself when Claude Code has been updated and once a week (not while automatic work
is on hold), and Settings → *Check again* runs it now. You get a notification when a new model appears, or when one
you use is no longer on your plan; nothing switches on its own.

**Which model for which step?** [reports/model-findings-2026-11.md](reports/model-findings-2026-11.md) has the short
version from real job ads: Claude Opus at high effort for writing, the local model for the rest, and why.

**How much better is it?** [reports/claude-vs-local.md](reports/claude-vs-local.md) has a side-by-side
on two real Jobindex ads with identical prompts. A blind reviewer would have sent Claude's version in all
three cases. The local drafts read well at first glance but contained invented motivation, a stale "last
year", overstated experience and a grammar slip; Claude's stayed inside the facts and named the gaps
honestly. The local *fact check* also missed most of those problems, so if you write locally, read the drafts
closely, or let Claude do at least the fact check (Settings → Search & fact check → "Writing model" with
Claude as the Writing model).

Before anything goes to a cloud model, ID numbers (CPR, etc.), bank and card numbers are removed.
Your contact details are never sent: they're added to the PDFs locally.

## Running it only when you want it

By default nothing runs on its own: `./agent start` starts everything and `./agent stop` stops everything. While
it runs it checks job sources, drafts applications and sends macOS notifications (strong match found, draft ready,
a daily summary). `./agent stop --keep-ollama` leaves Ollama running if you use it for other things.

If you'd rather have it start at login and stay in the background, run `scripts/install-autostart.sh` (undo with
`scripts/uninstall-autostart.sh`). It then uses memory and battery all the time, and Ollama has to be running too.

## Your data

Everything lives in `./data` (or `--data /some/folder`): the SQLite database, uploaded files and
exported PDFs. Settings → Your data → *Download a backup* gives you a zip. The app only listens on
`127.0.0.1`, refuses requests addressed to other hosts, and blocks cross-site form posts, so websites
you visit can't talk to it.

### Automatic backup

Settings → Your data → **Automatic backup** copies a zip with your database, your files and exported PDFs to a
folder you choose, once a week unless you pick another interval (every day to every month). A folder in Nextcloud,
such as `~/Nextcloud/Backups/Career Agent`, puts it on your server as well; the field suggests that one when it
finds your Nextcloud folder.

* **Keep every backup, or replace it.** *Adds a dated file* puts a new zip next to the earlier ones each time
  (`career-agent-backup-2026-11-17_1305.zip`) and, if you set *Keep the newest*, removes the oldest beyond that
  number. *Replaces the last one* keeps one file, `career-agent-backup.zip` (Nextcloud keeps its older versions
  under the file's Versions). Other files in the folder are never touched.
* **When the app is off.** A backup that came due while the app wasn't running is made within a minute of the next
  start, also while automatic work is on hold (it uses no model). A failed backup (Nextcloud folder gone, disk full)
  shows on the Settings page and as a notification, and is tried again after 6 hours.
* **Back up now** saves the settings and makes one right away.
* **Restoring.** Each zip has a `README.txt`: stop the app, move `career.db`, `career.db-wal` and `career.db-shm`
  aside in the data folder, put the backup's `career.db` there, copy `files/` and `exports/` back, and start again.
  Passwords and API keys live in the macOS Keychain and are never in a backup.

### Undo anything

Every change to your data is recorded before it happens, so nothing is lost by a click:

* **Undo** appears in the message after anything you change: a deleted fact, an edited letter, a removed job.
* **History** (in the menu) lists every change, newest first: yours and the agent's (reading documents, tidy-ups,
  scoring). Open an entry to see each field before and after, and undo it. Undo puts every row back exactly as it
  was, with the same id; a deleted application comes back with its saved drafts and timeline, a deleted document
  with its facts and search passages. If something was changed again since, it says what and asks before undoing.
  An undo can itself be undone.
* **Recently deleted** on the career database tab brings back a single fact, including facts a tidy-up merged away.

Your own changes and every deletion are kept for good; the agent's own edits (mostly jobs scored again) are kept
for 90 days. Not recorded: search passages and embeddings (rebuilt when needed), the model-call log and the task
queue. Changes made outside the app, such as with the `sqlite3` command, can't be seen by the history, so the app
also makes a full copy of the database when it starts, once a day, in `data/snapshots/` (the newest 7 are kept).
A Nextcloud folder you stop watching comes back without its password, since that lives in the Keychain.

The career database has eight kinds of fact: experience, education, projects, smaller programs and utilities,
skills, certifications, languages and about me. A result is never a fact of its own: it is written into the
project, job or education it came from, so each project reads as what it was, your part and the result. The tab
groups each kind by where it happened, newest first: your job or education there names the place and gives its
years, and spellings such as "Nordvik" and "Nordvik P/S" count as one place. Skills are grouped by area instead,
and each says how well you know it (used at work, used in a larger project, coursework, insight only), which the
writer sees, so insight is never written up as experience.

A fact remembers the titles of the facts that were merged into it. Reading a document again, or importing the
agent folder again, doesn't bring those back as facts of their own.

## Troubleshooting

| Problem | Fix |
|---|---|
| “Ollama isn't running” | Open the Ollama app, or run `ollama serve`. |
| “model isn't installed” | `ollama pull <model>` in Terminal, then pick it in Settings. |
| A job site blocks fetching | Paste the ad text on the job's page (“Paste the ad text and I'll work from that”). |
| “macOS didn't allow reading that folder” | Use the upload box, or give your Terminal app Full Disk Access. |
| Mail draft fails | Allow automation of Mail in System Settings → Privacy & Security → Automation. |
| Scanned PDFs | They have no text layer. Export or OCR them first (Preview can't; most scanner apps can). |
| `.pages` files | In Pages: File → Export To → PDF or Word, then import that. |

## For developers

```bash
uv run pytest            # tests (a fake model, no network)
uv run python -m career_agent --port 8765 --data ./data --no-browser
```

| Path | What |
|---|---|
| `career_agent/llm/` | Providers (Claude Code, Ollama), the models your Claude plan offers, JSON schemas, usage tracking, who made what |
| `career_agent/ingest.py` | File, folder and mailbox import; text extraction |
| `career_agent/profile.py` | Document analysis into facts, de-duplication, prompt digests |
| `career_agent/retrieval.py` | Passages, local embeddings, hybrid keyword + meaning search |
| `career_agent/watch.py` | Watched folders and Nextcloud (WebDAV) sync |
| `career_agent/knowledge.py` | The agent folder: summaries and style guides in prompts, importing its facts |
| `career_agent/voice.py` | Style guide, writing statistics, the voice check |
| `career_agent/jobs.py`, `sources.py`, `fetch.py` | Adding, reading, screening and scoring jobs |
| `career_agent/writer.py` | Letters, CVs, revisions, fact check, interview prep, follow-ups |
| `career_agent/cvdata.py`, `cvrender.py`, `cv_templates/` | The CV as data: base CVs, tailoring, JSON Resume; typesetting with Typst |
| `career_agent/exporter.py`, `mailer.py` | PDF/Word rendering, Apple Mail drafts |
| `career_agent/tracker.py`, `companies.py`, `insights.py` | Timeline, ghosting, employer grades, stats |
| `career_agent/tasks.py` | Background queue, worker and scheduler, retries after passing errors |
| `career_agent/redo.py` | Redoing work with another model, one item or in bulk |
| `career_agent/todos.py` | Your to-do list and dated reminders |
| `career_agent/history.py` | Recording every change, undo and restore, the daily database copy |
| `career_agent/backup.py` | Automatic backups to a folder you choose (e.g. Nextcloud), dated or replaced |
| `career_agent/web/` | FastAPI app, templates, CSS, a little JS |
| `scripts/eval/letters.py` | A small benchmark: the same letters from several models, fact-checked by one checker, laid out blind for you to rank (results in `data/eval/`) |
