# Which model for which step: findings, November 2026

Notes from a few small runs of `scripts/eval/letters.py`: the same cover letters written by several models for
three real job ads (a close fit, a stretch, an English one), fact-checked by one fixed checker (Claude Opus at
medium effort) and ranked blind by the candidate. Three letters per setting, so these are directions, not
measurements: between two runs of the same setting the share of flagged claims moved by as much as ten points.

Cloud models were used through Claude Code (a subscription), never the pay-per-call API. Testing stopped here on
purpose; the settings below are what the app now uses.

*Settings and "Since the test" updated 26 November 2026. The findings are unchanged.*

## What the app uses now

| Step | Model | Basis |
|---|---|---|
| Letter, CV, style guide, interview prep | Claude Opus 5.5, high effort | Tested for the letter: best openings and most willing to take a position; high is where the gain stops |
| Screening, reading ads, scoring, reading documents, reading replies | Claude Sonnet 5.5, high effort | Not tested. Chosen when local models were dropped |
| Fact check | Claude Sonnet 5.5, high effort | "Fact check with" is set to the Analysis model: comparing claims with the career database doesn't need the writing model. Sonnet has checked every draft since 16 November; the test's own checker was Opus at medium |
| Voice read | Claude Sonnet 5.5, high effort | Not tested. Set to follow the Analysis model: it judges how each sentence sounds and only suggests plainer versions, which you accept or not |

Every step follows the Writing or Analysis model and effort at the top of Settings. Under Settings → Model and
effort for each step, a step's model and its effort can each follow the other picker instead, or be set to one
fixed version or level. Only the voice read has a setting of its own: it follows the Analysis model, and with it
the Analysis effort.

## Since the test

- **Local models are no longer used** (16 November). The analysis steps moved from Gemma 4 26B to Claude Sonnet
  without a comparison run.
- **Every step follows the two pickers** (26 November). Until then each step had its own saved model, and three of
  them differed from the pickers: the fact check, the style guide and interview prep ran on Sonnet. The style
  guide and interview prep now run on Opus. The fact check was put back on Sonnet the same day, with "Fact check
  with" set to the Analysis model. Opus for scoring and reading documents was tried that day and taken back.
- **A step can follow either picker** (26 November), for its model and its effort separately. The voice read was
  moved to the Analysis model that day, so it runs on Sonnet.
- **The call log for 20–26 November**, before that change: 1,126 calls, 2 failed, about 245 minutes of model time
  and 1.9 million output tokens. Sonnet wrote 80% of them: scoring and reading ads a quarter each, screening
  about a fifth, the fact check a tenth. Screening a list of jobs took 6 seconds and 400 tokens a call at high
  effort, so high costs little on the simple steps.
- **If the plan's usage limit is ever reached**, the first thing to lower is the Analysis effort (high to medium),
  not the writing.

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

Local models other than Gemma 4; Sonnet against another model or a lower effort for screening, reading ads or
scoring; Sonnet against Opus as the fact checker on the same drafts; whether a reader other than the candidate
prefers the same letters.
