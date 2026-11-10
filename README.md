# Career Agent

A personal job-search agent that runs on your Mac. It learns what you've actually done and how you
write from your own documents, finds and ranks jobs, drafts applications and tailored CVs in your
voice, fact-checks every claim against your real experience, and tracks how each employer treats you.

You stay in charge of one thing: nothing is ever sent without you pressing Send.

## Start it

```bash
./start.sh
```

It opens <http://127.0.0.1:8765>. The first run installs what it needs. Stop it with Ctrl+C.

The **Today** page walks you through setup:

1. **AI model.** Your Mac already has Ollama with Gemma 4 26B, so it works out of the box, fully
   offline. For the best letters, add a Claude API key in Settings (see below).
2. **About you.** Name and contact details (printed on your documents, never sent to the AI),
   target roles, locations, deal-breakers, things to always or never mention.
3. **Import your documents.** Drop files (PDF, Word, PowerPoint…), or import a whole folder (`~/Documents/Job applications`).
   CVs, every application you've written, reference letters, reports, and emails you sent (Apple
   Mail → Mailbox → Export Mailbox gives an `.mbox` folder; only emails *you* wrote are used).
4. **Check your career database.** Every fact is extracted with a quote from the source document.
   Tick the correct ones, fix or hide the rest. Mark how old applications went (interview, rejected…):
   the ones that got interviews become the main examples of your voice.
5. **Add jobs.** Paste a job link, or add a job-search feed so jobs arrive automatically.

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
  most job sites' search pages (the agent picks out the job links). LinkedIn needs a login, so paste
  LinkedIn job links (or the ad text) by hand.
* **Honesty guardrails.** The writer may only use facts from your career database; it never invents
  employers, titles, dates, numbers or skills. A separate fact check then marks every claim in the
  letter and CV as backed, stretched or unsupported, so you fix problems before an interviewer finds them.
* **Your voice.** A style guide is written from your own texts, your best past applications are used
  as examples, and every draft is compared with your own sentence length, paragraphing and phrasing.
  Stock phrases you never use ("passionate about", "brænder for", em dashes…) are flagged.
* **Employers are graded too.** Paste a reply and it's classified (template rejection, real feedback,
  interview invite…). Silence is counted. Companies that ghost you or re-post the same job get a low
  grade, and you can block them from automatic drafting.
* **Interview prep.** When you get an interview: likely questions with answers built from your real
  experience, the gaps they may probe, every claim you made, and questions to ask them.

## Retrieval: the right facts for each job

Every document is split into passages and indexed on this Mac, by keywords and by meaning (a local
multilingual embedding model, `bge-m3` in Ollama, so Danish and English match). That's used to:

* **Pick the facts that matter for each job.** When your whole career database doesn't fit the model's
  budget (mostly with local models), every role, degree, certification and language is always kept, and
  the achievements, skills and values most relevant to the ad fill the rest.
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
* LM Studio, `mlx_lm.server`, llama.cpp and similar servers work through the
  “OpenAI-compatible server” option.
* A good middle ground: **Claude for Writing, the local model for Analysis.** Your documents are read
  locally; only the letter-writing context goes to Claude. The fact check runs on the Writing model by
  default (Settings → Search & fact check), since it's your safety net.

## Using Claude without an API key (Free or Pro plan)

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

## Using Claude (cloud, best quality)

1. Create an API key at <https://console.anthropic.com> → API keys (it's separate from a Claude.ai
   subscription and billed per use).
2. Settings → AI models → paste the key (it's stored in your macOS Keychain), tick “Use Claude for both
   tiers”, save, and press **Test**.

The default model is Claude Opus 5.5. A full application (letter, CV, fact check) typically costs on the
order of a few tens of cents, and reading and scoring a job a few cents. The large shared context
(your career database, style guide and examples) is prompt-cached, so batches cost less. Insights → AI usage
shows your actual spend. For cheaper bulk work, choose Claude Haiku 5.5 or Sonnet 5.5 for Analysis.

**How much better is it?** [reports/claude-vs-local.md](reports/claude-vs-local.md) has a side-by-side
on two real Jobindex ads with identical prompts. A blind reviewer would have sent Claude's version in all
three cases. The local drafts read well at first glance but contained invented motivation, a stale "last
year", overstated experience and a grammar slip; Claude's stayed inside the facts and named the gaps
honestly. The local *fact check* also missed most of those problems, so if you write locally, read the drafts
closely, or let Claude do at least the fact check (Settings → Search & fact check → "Writing model" with
Claude as the Writing model).

Before anything goes to a cloud model, ID numbers (CPR, etc.), bank and card numbers are removed.
Your contact details are never sent: they're added to the PDFs locally. Requests use Anthropic's
server-side fallback (`fallbacks: "default"`), so if the model declines a request it is retried on a
fallback model automatically. Since this only writes job applications, that should almost never happen.

## Keep it running in the background

To have it check job sources and draft applications even when you haven't opened it:

```bash
scripts/install-autostart.sh
```

It then starts at login and sends macOS notifications (strong match found, draft ready, a daily summary).
Undo with `scripts/uninstall-autostart.sh`.

## Your data

Everything lives in `./data` (or `--data /some/folder`): the SQLite database, uploaded files and
exported PDFs. Settings → Your data → *Download a backup* gives you a zip. The app only listens on
`127.0.0.1`, refuses requests addressed to other hosts, and blocks cross-site form posts, so websites
you visit can't talk to it.

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
| `career_agent/llm/` | Providers (Claude, Ollama, OpenAI-compatible), JSON schemas, cost tracking |
| `career_agent/ingest.py` | File, folder and mailbox import; text extraction |
| `career_agent/profile.py` | Document analysis into facts, de-duplication, prompt digests |
| `career_agent/retrieval.py` | Passages, local embeddings, hybrid keyword + meaning search |
| `career_agent/watch.py` | Watched folders and Nextcloud (WebDAV) sync |
| `career_agent/knowledge.py` | The agent folder: summaries and style guides in prompts, importing its facts |
| `career_agent/voice.py` | Style guide, writing statistics, the voice check |
| `career_agent/jobs.py`, `sources.py`, `fetch.py` | Adding, reading, screening and scoring jobs |
| `career_agent/writer.py` | Letters, CVs, revisions, fact check, interview prep, follow-ups |
| `career_agent/exporter.py`, `mailer.py` | PDF/Word rendering, Apple Mail drafts |
| `career_agent/tracker.py`, `companies.py`, `insights.py` | Timeline, ghosting, employer grades, stats |
| `career_agent/tasks.py` | Background queue, worker and scheduler |
| `career_agent/web/` | FastAPI app, templates, CSS, a little JS |
