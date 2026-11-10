# Building an agent folder

Career Agent reads an `Agents/` folder (see "The agent folder" in the main README) but doesn't write one. These
scripts are for writing it from a person's own files with AI subagents (for example in Claude Code), checking it,
and refreshing it later. Nothing here sends files anywhere by itself; the subagents you run do the reading.

```
Agents/                          for any agent: README.md, about-me.md, nextcloud-map.md, open-questions.md…
└── Career Agent/
    ├── profile.md               hand-written last, from all the summaries
    ├── experience/*.md          one summary per education, job, internship, project, free time
    ├── writing-style-<lang>.md  how the person writes, with verbatim example sentences
    ├── facts.jsonl / facts.md   every fact with its source file and a verbatim quote
    └── sources.jsonl / sources.md, notes-by-area.md, cv.md, applications.md…
```

## Steps

1. **Map the files.** Folder names, sizes and file types only (`find`, `du`), with the person's OK. Choose areas
   (CVs and applications, writing style, each education, each job, personal projects, a general area for
   housekeeping) and agree on what's off-limits: ID papers, bank, tax, salary, health, contracts, other people's
   personal data, photos.
2. **Run one subagent per area.** Fill in `brief-template.md` once (name, root folder, off-limits list, repo path)
   and give each subagent the brief, its scope and its own output folder `staging/<area>/`. The brief defines the
   file formats and where each file ends up.
3. **Check.**
   `uv run python -I scripts/agent-folder/verify.py staging --root ~/Nextcloud`
   Every quote against the extracted text of its source file, the JSON shape, and anything that looks like an ID
   number, email, phone number, account number or password field. Add `--pronouns` to flag he/she in summaries.
4. **Merge.**
   `python3 scripts/agent-folder/merge.py staging out/Agents --general general --confidential "Work/<Employer>"`
   Places the summaries, merges and renders facts and sources, and collects every area's notes. Paths inside a
   `--confidential` folder are cut back to its top folder (file names there often name clients), and quotes from
   confidential facts are dropped.
5. **Write the hand-made files:** `README.md`, `about-me.md`, `Career Agent/README.md`, `Career Agent/profile.md`
   and `Career Agent/open-questions.md`. Ask the person the open questions, fix the summaries, and merge again.
6. **Publish.** Read `out/Agents` once more, then `rsync -rc out/Agents/ ~/Nextcloud/Agents/`. Without
   `--delete`, nothing already in the folder is removed.
7. **Import.** In Career Agent: Profile → Documents → Agent folder → *Import facts from the folder*. It's safe to
   run again after every refresh.
8. **Optional: test it.** `compare.py` writes a letter with and without the folder for each job ad you give it
   and fact-checks both, for a blind comparison:
   `uv run python -I scripts/agent-folder/compare.py out/compare ad1.txt https://… ad2.txt https://…`

## Helpers for the subagents

- `extract.py FILE [--max-chars N] [--meta] [--stats]`: the text of a document, using Career Agent's own
  extraction (PDF, Word, PowerPoint, ODT, RTF, Pages, HTML, e-mail), plus JSON and CSV. `--meta` shows author and
  dates, which helps tell the person's own files from others'.
- `stats.py FILE...`: writing statistics per document (words, sentence and paragraph length, commas, sentences
  starting with I/Jeg, em dashes), measured the same way the app's voice check measures drafts.

## Refreshing later

When something changes (a job ends, a new reference, new projects), run only the areas that changed, keeping the
other areas' staging folders, then steps 3–7. Hand-written files are never overwritten by `merge.py`; edit them
directly.
