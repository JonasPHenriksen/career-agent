# Which model for which step: findings, November 2026

Notes from a few small runs of `scripts/eval/letters.py`: the same cover letters written by several models for
three real job ads (a close fit, a stretch, an English one), fact-checked by one fixed checker (Claude Opus at
medium effort) and ranked blind by the candidate. Three letters per setting, so these are directions, not
measurements: between two runs of the same setting the share of flagged claims moved by as much as ten points.

Cloud models were used through Claude Code (a subscription), never the pay-per-call API. Testing stopped here on
purpose; the settings below are what the app now uses.

## What the app uses now

| Step | Model | Why |
|---|---|---|
| Letter, CV, fact check | Claude Opus, high effort | Best openings and most willing to take a position; high is where the gain stops |
| Screening, reading ads, scoring, reading documents | Local Gemma 4 26B | Fast enough, private, and these steps didn't show a need for more |

Every step can be given another model and effort under Settings → Model and effort for each step.

## Findings

1. **The brief mattered more than the model.** With a recipe ("status, then a paragraph per experience, answer the
   ad's points in order") all four models wrote the same letter: the CV in sentences. A goal ("make one person
   want to talk to you; the CV carries the facts; one line of thought; 150–300 words") changed the letters far
   more than any model swap. The style guide had the same problem: it described the structure of old letters, and
   the models obeyed it. It now describes a voice only.
2. **Local versus Claude for writing: Claude, clearly.** Gemma 4 31B had about two thirds of its claims flagged
   (12 backed, 24 partly, 1 unbacked over three letters), against 17–39% for the Claude settings in the same
   round, and took about 13 minutes per letter against half a minute.
3. **Effort: high is the knee.** Claude Opus on the same three jobs:

   | Effort | Share of claims flagged | Seconds per letter | Output tokens per letter |
   |---|---|---|---|
   | low | 43% | 18 | 1,600 |
   | medium | 19% | 33 | 3,000 |
   | high | 9% | 46 | 4,700 |
   | extra-high | 9% | 172 | 18,500 |

   Low still invented how the work was done. Extra-high cost four times the tokens and time of high for nothing
   measurable. Claude Sonnet showed the same: extra-high (21% flagged, 87 s) was no better than high (16%, 31 s).
4. **Opus versus Sonnet at high effort: the checker can't separate them** (20% and 16% flagged in the same run).
   The choice was made by reading: Opus opens better and commits to a view; Sonnet is tighter.
5. **Haiku at extra-high effort is accurate but expensive and flat.** Fewest flags of the first round (17%), at ten
   times the output tokens of Sonnet or Opus (about 42,000 per letter) and five times the wait.
6. **Loosening the brief invites invented reasons.** Free to argue, the writers made up how and why things were
   done ("the work began with the warehouse, not the code"). Two things fixed most of it: forbidding reasons the
   career database doesn't state (inferred ones are listed for the candidate to confirm), and asking the candidate
   for the real reasons behind their main projects and storing the answers as facts.
7. **Voice habits get overused.** A style guide that lists a person's hedges and devices gets all of them in every
   letter. The writer now uses any one habit at most once per text.
8. **The fact check must accept honest gaps.** "X is new to me" was flagged as unbacked, because a database can't
   record what someone hasn't done. It now passes unless the database says otherwise.
9. **Sameness is the open risk.** Letters for the same job converge on the same opening and the same project
   whatever the effort level. Several applications to one employer would read alike.

## Not tested

Local models other than Gemma 4; Claude as the model for screening, reading ads or scoring; whether a reader other
than the candidate prefers the same letters.
