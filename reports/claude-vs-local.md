# Claude vs. the local model: same prompts, same candidate

*Generated 9 November 2026 with the fictional test candidate "Sara Lund" and two real Jobindex ads.*

## How it was run

* **Same input.** Both models got the exact prompts Career Agent sends: her career database, style guide, past applications, the fit analysis and the ad.
* **Local:** `gemma4:26b-mlx` in Ollama on this MacBook, through the app itself (thinking on for first drafts).
* **Claude:** Claude Opus 5.5 run as a subagent, standing in for the API (no API key on this Mac). The real API call adds structured output and effort settings; quality should be similar, speed and cost are not measured here.
* **Checks:** the app's own fact check (same local checker for both), the voice check against her own letters, and a blind review by a separate Claude subagent that saw versions "A" and "B" without knowing which was which. That reviewer is also Claude, so treat its preference as indicative, not neutral; the factual problems it lists are checkable.

## Results

| | KOMBIT letter | KOMBIT cv | Amgros letter |
|---|---|---|---|
| Words, Local (Gemma 4 26B) | 308 | 290 | 276 |
| Words, Claude (Opus 5.5) | 365 | 334 | 351 |
| Voice match, Local (Gemma 4 26B) | 79 | – | 72 |
| Voice match, Claude (Opus 5.5) | 88 | – | 86 |
| Fact check, Local (Gemma 4 26B) | 7 supported | 16 supported, 1 partly | 9 supported |
| Fact check, Claude (Opus 5.5) | 16 supported | 20 supported | 15 supported |
| Time, local | 209 s | 126 s | 98 s |

## Blind review

### KOMBIT, Product Owner til VALG: cover letter

| | Factual accuracy | Relevance to the ad | Honest about gaps | Sounds like her | Danish | Persuasive |
|---|---|---|---|---|---|---|
| Claude (Opus 5.5) | 9 | 7 | 8 | 7 | 9 | 7 |
| Local (Gemma 4 26B) | 4 | 5 | 2 | 4 | 6 | 5 |

**Would send with the least editing:** Claude (Opus 5.5). Send A: every fact checks out, it admits the missing election experience and sounds like Sara, but it needs cutting and less ad-quoting; B reads as security-tailored yet rests on invented motivation, a false 'Sidste år' and an unbacked 'sikkerhedskritiske systemer' claim.

**Claude (Opus 5.5)**
* Unbacked or stretched: Mild: 'fordi jeg gerne vil arbejde med et produkt, der skal virke, når det gælder' – motivation not in the database (forward-looking and plausible, but confirm with Sara).; Mild: 'Det er et ansvar, jeg gerne vil være med til at bære' – stated feeling not in the database.; Mild framing: 'betalinger skal virke hver gang' – hints at a reliability responsibility the database does not describe (phrased as a truism, not as a claim about her).
* Edits before sending: Cut from about 360 words to 200–250 (her own letters are 150–200): drop the PSPO/OKR/Power BI sentence and either the vendor countries or the 25-colleague detail.; Quote the ad once, not twice: the closing paragraph restates the ad's two requirements almost verbatim – say it in her own words or cut it.; Tie Novo's Quality & Compliance projects and the on-time, under-budget go-live for 400 users explicitly to VALG's quality and robustness demands, without claiming security expertise.; Confirm with Sara that 'et produkt, der skal virke, når det gælder' is her real motivation.; Optional polish: 'dem i kommunerne, der bruger VALG' → 'de kommunale brugere af VALG'; use a named contact if one is known.

**Local (Gemma 4 26B)**
* Unbacked or stretched: 'fordi jeg er motiveret af at bygge digitale løsninger, der er fundamentale for vores samfund' – invented motivation; her recorded motivation is user value, measuring effect and the users/business/developers crossfield.; 'Når man arbejder med penge og betalinger, er der ingen plads til fejl; sikkerhed, stabilitet og robusthed er de absolut vigtigste krav' – implies hands-on security/stability work the database never mentions (and clashes with her real win, which was redesigning error messages).; 'Sidste år' – the 30 % reduction is recorded as 2022; in November 2026 'last year' is false.; 'arbejdede jeg målrettet på at optimere vores flows, hvilket blandt andet reducerede ...' – turns a team result into a solo effort, widens it to 'optimising our flows', and 'blandt andet' implies further results that are not recorded.; 'Det kræver et skarpt fokus på både de store systemiske krav og de helt små brugerdetaljer' – 'systemiske krav' has no basis in the database.; 'Det lærte mig vigtigheden af at sikre ekstremt høj kvalitet og overholde strenge regulatoriske krav i et miljø, hvor fejl kan have store konsekvenser' – invented lesson; the database says Novo taught her to keep track of large budgets and many stakeholders.; 'Det har givet mig en forståelse for de særlige behov og den struktur, der findes i den offentlige sektor' – stretch from under two years as a business analyst on public IT projects at Netcompany.; 'for at sikre, at vi hele tiden bygger det mest robuste produkt' – rewrites her stated philosophy (numbers that show whether we hit the mark) as a robustness claim.; 'min erfaring med sikkerhedskritiske systemer' – unbacked and material for this role; nothing in the database describes security-critical work.; 'mit fokus på kvalitet' – mild, not recorded as such.
* Edits before sending: Delete 'min erfaring med sikkerhedskritiske systemer' and 'der er ingen plads til fejl'.; Remove 'Sidste år' – the result is from 2022; drop the time word or write 'i 2022'.; Replace the invented motivation and the invented Novo lesson with what the database supports: large budgets, many stakeholders, three Quality & Compliance projects, on-time and under-budget go-live for 400 users.; Give the 30 % back to the team ('vi redesignede ...') and drop 'arbejdede jeg målrettet på at optimere vores flows ... blandt andet'.; Add an honest line about having no election or municipal experience and how she would close the gap; the letter never mentions 'kommunerne', although value for the municipalities is the ad's first responsibility.; Fix Danish: 'ansættelsesteam' → 'ansættelsesudvalg'; 'Det lærte mig vigtigheden af' → 'Her lærte jeg, hvor vigtigt det er'; 'bringe min erfaring ... til KOMBIT' → 'tage ... med til KOMBIT'; 'bruges af omkring 2 mio. aktive brugere' → 'har omkring 2 mio. aktive brugere'; untangle 'hvilket ... ved at redesigne'.; Replace 'de digitale valgprocesser' (Danish voting is not digital; the ad speaks of infrastructure 'omkring' the processes) and tone down the closing 'bidrage til at sikre de danske valg'.; Cut corporate filler: 'de absolut vigtigste krav', 'skarpt fokus', 'de store systemiske krav', 'ekstremt høj kvalitet'; shorten towards 200 words.

### KOMBIT, Product Owner til VALG: tailored CV

| | Factual accuracy | Relevance to the ad | Honest about gaps | Sounds like her | Danish | Persuasive |
|---|---|---|---|---|---|---|
| Claude (Opus 5.5) | 10 | 7 | 9 | 8 | 9 | 8 |
| Local (Gemma 4 26B) | 5 | 5 | 4 | 4 | 6 | 5 |

**Would send with the least editing:** Claude (Opus 5.5). Send A, an accurate and clean CV in Sara's voice that needs only a VALG-oriented profile line; B buys surface relevance with an inflated '8 års' product-owner claim, unbacked security wording, dropped SQL/Figma qualifiers and an English skill tag.

**Claude (Opus 5.5)**
* Edits before sending: Add one profile sentence aimed at VALG using only real facts (Quality & Compliance projects, on-time and under-budget go-live for 400 users, public IT for Skattestyrelsen) – no new security claims.; Consider leading the Novo Nordisk section with the on-time, under-budget document-management go-live.; Optional: add the KIT board seat (2021 – nu) under Frivilligt arbejde/Tillidshverv.; Optional: drop or rephrase 'Leveranceansvar' – it comes from a referee's recommendation and reads oddly as a skill tag.; Optional: Danish date format ('jan. 2022 – nu') and the scope detail from the evidence (MobilePay-integration, overførsler, regninger).

**Local (Gemma 4 26B)**
* Unbacked or stretched: Headline 'Product Owner med erfaring fra kritiske digitale systemer' – 'kritiske' is a characterisation the database never makes.; 'Erfaren Product Owner med over 8 års erfaring' – she has been a product owner since 2022-01 (under five years); the 8 years only add up by counting her Novo Nordisk project-manager years.; 'fintech' – Danske Bank is a traditional bank; the database never calls it fintech.; 'erfaring med at drive digitale produkter, der stiller høje krav til sikkerhed og robusthed, herunder betalingsløsninger' – security/robustness is not in the database, and plural 'produkter' plus 'herunder' imply more than the payments area.; 'Styrede interessenter og leverandører for at sikre overholdelse af de digitale projekter' – compliance framing not in the database (and the sentence does not make sense).; 'SQL' – the database says SQL (grundlæggende); dropping the qualifier overstates the skill.; 'Figma' – the database says Figma (læse og kommentere); dropping the qualifier overstates the skill.
* Edits before sending: Replace the headline with an accurate one, e.g. 'Product Owner med erfaring fra bank, life science og offentlige it-projekter'.; Fix 'Erfaren Product Owner med over 8 års erfaring': product owner since 2022; write 'ca. 10 års erfaring med it-projekter og produktudvikling' or drop the number.; 'fintech' → 'bank'; delete 'der stiller høje krav til sikkerhed og robusthed' and the plural 'produkter ... herunder'.; Put the public-sector experience (Netcompany, Skattestyrelsen) in the profile – it is her most KOMBIT-relevant fact and is missing there.; Rewrite the stakeholder bullet from the database: got IT, Quality and external vendors pulling in the same direction, kept the steering committee informed, coordinated vendors in India and Poland.; Restore 'til tiden og under budget' on the document-management bullet.; Restore 'SQL (grundlæggende)' and 'Figma (læse og kommentere)'; translate or remove the English 'Delivery responsibility'.; Danish polish: 'et team af' → 'et team på'; cut the erfaren/erfaring repetition (four times in headline and profile); keep one grammatical person in the profile; 'Trænede' → 'Oplærte'.

### Amgros, Product Owner / Procesleder: cover letter

| | Factual accuracy | Relevance to the ad | Honest about gaps | Sounds like her | Danish | Persuasive |
|---|---|---|---|---|---|---|
| Local (Gemma 4 26B) | 4 | 6 | 3 | 5 | 5 | 4 |
| Claude (Opus 5.5) | 10 | 8 | 9 | 7 | 9 | 7 |

**Would send with the least editing:** Claude (Opus 5.5). Send B: it is accurate, built cleverly on the ad's split between daily development and larger changes and honest about the missing procurement experience; A opens with an invented 'Jeg har længe beundret', a gender-agreement error and a stale 'Sidste år', and claims daily roadmap work the database does not support.

**Local (Gemma 4 26B)**
* Unbacked or stretched: 'Jeg har længe beundret den centrale rolle, Amgros spiller ...' – invented feeling and history; nothing in the database about Amgros.; 'jeg arbejder hver dag med at prioritere en roadmap gennem kvartalsvise OKR'er og Scrum' – roadmap ownership and daily roadmap prioritisation are not in the database (only OKRs and Scrum are).; 'Sidste år reducerede vi ...' – the result is recorded as 2022; false in November 2026.; 'gennem tæt samarbejde med vores udviklere og designere' – the team has one designer; crediting 'tæt samarbejde' as the cause is an embellishment of the recorded redesign.; 'Min erfaring fra Novo Nordisk har givet mig en solid forståelse for life science-sektoren' – overclaim; her role was digitalisation projects in Quality & Compliance.; 'eksterne leverandører i bl.a. Indien og Polen' – 'bl.a.' implies more countries than recorded.; 'en klar prioritering af, hvad der skaber mest værdi på den lange bane' – mild addition to her recorded philosophy.; 'Jeg arbejder målrettet på at sikre, at de daglige opgaver og de store strategiske forandringer følges ad' – lifted from the ad and presented as her practice; not in the database.; Mild: 'jeg gerne vil bidrage til de rammer, der sikrer effektiv udbud og levering til det danske sundhedsvæsen' – motivation not in the database.
* Edits before sending: Delete 'Jeg har længe beundret ...' and replace it with a true, Sara-confirmed reason for wanting Amgros.; Remove 'Sidste år' (the 30 % result is from 2022).; Remove the daily-roadmap claim, the 'følges ad' claim, 'solid forståelse for life science-sektoren' and 'bl.a.'; 'designere' → one designer, or just 'teamet'.; Add an honest gap line: no udbud/kontraktstyring experience, bridged by vendor coordination at Novo and kravspecifikation for Skattestyrelsen at Netcompany (Netcompany is missing entirely).; Add the on-time, under-budget go-live for 400 users as proof she can run 'de større forandringer'.; Fix Danish: 'effektiv udbud' → 'effektivt udbud og effektiv levering'; 'bringe værdi til' → 'skabe værdi for'; break up the two 'hvilket' sentences; avoid 'skaber ... skaber'; 'Kære Amgros,' → 'Kære ansættelsesudvalg'.; Use the full job title (Product Owner / Procesleder til udbud og kontraktstyring i IT).

**Claude (Opus 5.5)**
* Edits before sending: Add one true 'why Amgros' sentence (what in Amgros's work interests her) – confirm it with Sara, no invented history.; Trim from about 340 words towards 250: drop the PSPO/OKR/Power BI sentence or the 25-colleague detail.; In the gap paragraph, lead with the Netcompany kravspecifikation work (closest to udbud) and soften 'og det er det nærmeste, jeg kommer', which undersells her.; Minor: untangle 'indført kvartalsvise OKR'er, som vi sætter sammen i teamet, og et fælles dashboard i Power BI'.

**Overall (reviewer):** In all three cases the version that stays strictly inside the career database (kombit-letter A, kombit-cv A, amgros-letter B) is the one to send: it names real gaps, credits the team and sounds like Sara, though both of those letters run about 340–360 words against her 150–200 and stack facts, so they mainly need trimming. The other versions (kombit-letter B, kombit-cv B, amgros-letter A) look more tailored at first glance but lean on invented motivation or history, a stale 'Sidste år' for a 2022 result, unbacked security or roadmap claims, dropped skill qualifiers and anglicised corporate Danish that would not survive an interview.

## The drafts

### KOMBIT, Product Owner til VALG: cover letter

#### Local (Gemma 4 26B)

```text
Kære ansættelsesteam,

Jeg søger stillingen som Product Owner til VALG, fordi jeg er motiveret af at bygge digitale løsninger, der er fundamentale for vores samfund. Det er præcis det, de digitale valgprocesser er – en central del af den digitale infrastruktur, som kræver den højeste grad af sikkerhed og pålidelighed.

I dag er jeg product owner for betalingsfunktionerne i Danske Banks mobilbank, som bruges af omkring 2 mio. aktive brugere. Når man arbejder med penge og betalinger, er der ingen plads til fejl; sikkerhed, stabilitet og robusthed er de absolut vigtigste krav. Sidste år arbejdede jeg målrettet på at optimere vores flows, hvilket blandt andet reducerede supporthenvendelser om betalinger med 30 % ved at redesigne fejlbeskeder og kvitteringsflowet. Det kræver et skarpt fokus på både de store systemiske krav og de helt små brugerdetaljer.

Før Danske Bank var jeg projektleder hos Novo Nordisk i lidt over tre år. Her ledte jeg digitale projekter inden for Quality & Compliance med et samlet budget på 12 mio. kr. Det lærte mig vigtigheden af at sikre ekstremt høj kvalitet og overholde strenge regulatoriske krav i et miljø, hvor fejl kan have store konsekvenser.

Som PSPO I-certificeret arbejder jeg efter Scrum-metodikken, og jeg har desuden erfaring med kravspecifikation og test i offentlige IT-projekter fra min tid hos Netcompany, blandt andet for Skattestyrelsen. Det har givet mig en forståelse for de særlige behov og den struktur, der findes i den offentlige sektor.

Jeg trives i krydsfeltet mellem brugere, forretning og udviklere. Jeg tror på at arbejde med små skridt og hurtig feedback for at sikre, at vi hele tiden bygger det mest robuste produkt. Jeg vil gerne bringe min erfaring med sikkerhedskritiske systemer og mit fokus på kvalitet til KOMBIT.

Jeg håber, vi kan tage en snak om, hvordan jeg kan bidrage til at sikre de danske valg.

Med venlig hilsen
Sara Lund
```

*Notes from the writer:* Emphasized your experience with mission-critical systems (banking/compliance) to match the high stakes of election infrastructure. Highlighted your previous experience with public sector IT (Netcompany/Skattestyrelsen) to build credibility for KOMBIT. Included your PSPO I certification as it is highly relevant for this role. The letter is approximately 340 words, fitting your target length.

#### Claude (Opus 5.5)

```text
Kære ansættelsesudvalg

Jeg søger stillingen som Product Owner til VALG, fordi jeg gerne vil arbejde med et produkt, der skal virke, når det gælder. I skriver, at løsningen ikke blot er et it-system, men en central del af den digitale infrastruktur omkring nogle af samfundets vigtigste demokratiske processer. Det er et ansvar, jeg gerne vil være med til at bære.

I dag er jeg product owner for betalingsfunktionerne i Danske Banks mobilbank. Den bruges af omkring 2 mio. mennesker, og betalinger skal virke hver gang. Jeg leder et team på otte udviklere, en designer og en tester. Vi arbejder i Scrum, og jeg er PSPO I-certificeret. Sammen redesignede vi fejlbeskederne og kvitteringsflowet, og det fik supporthenvendelserne om betalinger ned med 30 % på 12 måneder. Jeg har også indført kvartalsvise OKR'er for teamet og et fælles dashboard i Power BI.

Før det var jeg projektleder i Novo Nordisk i lidt over tre år. Jeg ledte tre digitaliseringsprojekter i Quality & Compliance med et samlet budget på 12 mio. kr. og koordinerede leverandører i Indien og Polen. Undervejs stod jeg for implementeringen af et nyt system til dokumenthåndtering for 400 brugere på tre lokationer. Det gik i luften til tiden og under budget, og jeg oplærte 25 kolleger i systemet. Her lærte jeg at holde styr på store budgetter og at få IT, Quality og eksterne leverandører til at trække i samme retning.

Offentlige it-projekter kender jeg fra Netcompany. Som Business Analyst arbejdede jeg med kravspecifikation og test, blandt andet for Skattestyrelsen. Jeg har ikke arbejdet med valg før, så det skal jeg sætte mig grundigt ind i. Jeg vil starte med at lytte til dem i kommunerne, der bruger VALG.

Jeg trives i krydsfeltet mellem brugere, forretning og udviklere. Jeg tror på små skridt, hurtig feedback og tal, der viser, om vi rammer rigtigt. VALG skal både skabe værdi for kommunerne og understøtte de høje krav til sikkerhed, kvalitet og robusthed i dansk valgafvikling. Det kræver en blanding af brugerfokus og struktur, og det er netop den blanding, jeg gerne vil tage med til KOMBIT.

Jeg håber, vi kan tage en snak om, hvad jeg kan bidrage med hos jer.

Med venlig hilsen
Sara Lund
```

*Notes from the writer:* The ad names no contact person, so the letter opens with 'Kære ansættelsesudvalg'. Swap in a name if you find one. The fit analysis suggests a 'high-security, mission-critical' angle, but your database shows no security or election work. The letter only draws the parallel that payments have to work every time, and says plainly that elections are new to you. The 30 % result is from 2022, so 'sidste år' from your old letter was dropped. 'Det var teamets idé' isn't in your database, so it was left out; add it back if it's true. Only a short intro from the ad came through. If the full ad lists more requirements, the letter is worth another pass.

### KOMBIT, Product Owner til VALG: tailored CV

#### Local (Gemma 4 26B)

```text
# Product Owner med erfaring fra kritiske digitale systemer
Erfaren Product Owner med over 8 års erfaring fra fintech og life science. Jeg har erfaring med at drive digitale produkter, der stiller høje krav til sikkerhed og robusthed, herunder betalingsløsninger til omkring 2 mio. brugere. Jeg trives i krydsfeltet mellem brugere, forretning og udviklere med fokus på datadrevet beslutningstagning.

## Erfaring
### Product Owner — Danske Bank
2022-01 – nu · København
- Ansvarlig for betalingsfunktioner i mobilbanken med omkring 2 mio. aktive brugere.
- Leder et team af otte udviklere, en designer og en tester i et Scrum-miljø.
- Reducerede supporthenvendelser om betalinger med 30 % ved at redesigne fejlbeskeder og kvitteringsflow.
- Indførte kvartalsvise OKR'er og et fælles dashboard i Power BI.

### Projektleder — Novo Nordisk
2018-08 – 2021-12 · Bagsværd
- Ledte tre digitaliseringsprojekter i Quality & Compliance med et samlet budget på 12 mio. kr.
- Implementerede et nyt system til dokumenthåndtering for 400 brugere på tværs af tre lokationer.
- Trænede 25 kolleger i det nye system.
- Styrede interessenter og leverandører for at sikre overholdelse af de digitale projekter.

### Business Analyst — Netcompany
2016-09 – 2018-07 · København
- Udførte kravspecifikation og test i offentlige IT-projekter, herunder for Skattestyrelsen.

## Uddannelse
### Cand.merc.it — Copenhagen Business School
2014 – 2016

### HA (Erhvervsøkonomi) — Aarhus Universitet
2011 – 2014

## Kompetencer
Scrum · SAFe · Jira · Confluence · Product discovery · Stakeholder management · Budgetstyring · OKR · Power BI · SQL · Figma · Delivery responsibility

## Certificeringer
Professional Scrum Product Owner I (PSPO I) (2020)

## Frivilligt arbejde
Bestyrelsesmedlem, Kvindelige Iværksættere i Tech (KIT) (2021 – nu)

## Sprog
Dansk (modersmål) · Engelsk (flydende) · Tysk (læse og forstå)
```

*Notes from the writer:* I have emphasized your experience with high-stakes systems (banking/compliance) to match KOMBIT's need for security and robustness. Your experience with public IT projects at Netcompany is highlighted to establish credibility for the public sector. The summary connects your background in mission-critical digital infrastructure to the requirements of the election systems role.

#### Claude (Opus 5.5)

```text
# Product Owner med erfaring fra bank, life science og offentlige it-projekter
Jeg er product owner for betalingsfunktionerne i Danske Banks mobilbank, som bruges af omkring 2 mio. mennesker. Før det ledte jeg tre digitaliseringsprojekter i Quality & Compliance i Novo Nordisk og arbejdede med kravspecifikation og test i offentlige it-projekter hos Netcompany. Jeg er PSPO I-certificeret og trives i krydsfeltet mellem brugere, forretning og udviklere. Jeg tror på små skridt, hurtig feedback og tal, der viser, om vi rammer rigtigt.

## Erfaring
### Product Owner — Danske Bank
2022-01 – nu · København
- Ansvarlig for betalingsfunktionerne i mobilbanken, som bruges af omkring 2 mio. mennesker.
- Redesignede sammen med teamet fejlbeskeder og kvitteringsflow, så supporthenvendelserne om betalinger faldt med 30 % på 12 måneder.
- Leder et team på otte udviklere, en designer og en tester, der arbejder i Scrum.
- Indførte kvartalsvise OKR'er for teamet og et fælles dashboard i Power BI.

### Projektleder — Novo Nordisk
2018-08 – 2021-12 · Bagsværd
- Ledte tre digitaliseringsprojekter i Quality & Compliance med et samlet budget på 12 mio. kr.
- Stod for implementeringen af et nyt system til dokumenthåndtering for 400 brugere på tre lokationer. Det gik i luften til tiden og under budget.
- Fik IT, Quality og eksterne leverandører til at trække i samme retning og holdt styregruppen orienteret.
- Koordinerede leverandører i Indien og Polen.
- Oplærte 25 kolleger i det nye system.

### Business Analyst — Netcompany
2016-09 – 2018-07 · København
- Kravspecifikation og test i offentlige it-projekter, blandt andet for Skattestyrelsen.

## Uddannelse
### Cand.merc.it — Copenhagen Business School
2014 – 2016

### HA (Erhvervsøkonomi) — Aarhus Universitet
2011 – 2014

## Certificeringer
### Professional Scrum Product Owner I (PSPO I)
2020

## Kompetencer
Stakeholder management · Scrum og SAFe · Product discovery · OKR · Leveranceansvar · Budgetstyring · Jira og Confluence · Power BI · SQL (grundlæggende) · Figma (læse og kommentere)

## Sprog
Dansk (modersmål) · Engelsk (flydende) · Tysk (læse og forstå)
```

*Notes from the writer:* Added a Certificeringer section for PSPO I. Left out the KIT board role, which adds little here, and a Projekter section, since the document-management project is already under Novo Nordisk. Your database shows no security or election work, so the CV doesn't claim any. If you've done security, stability or incident work at Danske Bank, add it to the database first. 'Leveranceansvar' comes from the 'Delivery responsibility' skill entry. Cut it if it feels vague.

### Amgros, Product Owner / Procesleder: cover letter

#### Local (Gemma 4 26B)

```text
Kære Amgros,

Jeg søger stillingen som Product Owner, fordi jeg gerne vil bidrage til de rammer, der sikrer effektiv udbud og levering til det danske sundhedsvæsen. Jeg har længe beundret den centrale rolle, Amgros spiller i at optimere driften gennem velstrukturerede IT-løsninger.

I dag er jeg Product Owner hos Danske Bank, hvor jeg har ansvaret for betalingsfunktionerne i mobilbanken. Vi har omkring 2 mio. aktive brugere, og jeg arbejder hver dag med at prioritere en roadmap gennem kvartalsvise OKR'er og Scrum. Sidste år reducerede vi supporthenvendelser om betalinger med 30 % ved at redesigne fejlbeskeder og kvitteringsflowet – et resultat, vi opnåede gennem tæt samarbejde med vores udviklere og designere.

Min erfaring fra Novo Nordisk har givet mig en solid forståelse for life science-sektoren, hvilket jeg håber kan bringe værdi til Amgros. Her ledede jeg tre digitaliseringsprojekter med et samlet budget på 12 mio. kr., hvor jeg også havde ansvaret for at koordinere eksterne leverandører i bl.a. Indien og Polen. Det har givet mig erfaring med at holde styr på både budgetter og de mange interessenter, der er involveret i komplekse processer, hvilket jeg ser som en vigtig del af jeres arbejde med udbud og kontraktstyring.

Jeg trives i krydsfeltet mellem brugere, forretning og udviklere. Jeg tror på, at man skaber de bedste løsninger gennem små skridt, hurtig feedback og en klar prioritering af, hvad der skaber mest værdi på den lange bane. Jeg arbejder målrettet på at sikre, at de daglige opgaver og de store strategiske forandringer følges ad.

Jeg håber, vi kan tage en snak om, hvordan jeg kan bidrage til jeres roadmap og de fremtidige løsninger hos Amgros.

Med venlig hilsen
Sara Lund
```

*Notes from the writer:* I emphasized your Novo Nordisk experience to bridge the gap toward procurement/contract management. I highlighted your vendor coordination experience to match the job requirements. The length is optimized to meet the specific word count requested while maintaining your concise voice.

#### Claude (Opus 5.5)

```text
Kære ansættelsesudvalg

Jeg søger stillingen som Product Owner / Procesleder til udbud og kontraktstyring i IT. I søger et bindeled mellem forretningen og IT, der kan sikre sammenhæng mellem den daglige videreudvikling og de større forandringer. Det er præcis det krydsfelt, jeg trives i.

Den daglige videreudvikling kender jeg fra mit nuværende job. Jeg er product owner for betalingsfunktionerne i Danske Banks mobilbank, som bruges af omkring 2 mio. mennesker. Jeg leder et team på otte udviklere, en designer og en tester. Vi arbejder i Scrum, og jeg er PSPO I-certificeret. Sammen redesignede vi fejlbeskederne og kvitteringsflowet, og det fik supporthenvendelserne om betalinger ned med 30 % på 12 måneder. Jeg har også indført kvartalsvise OKR'er, som vi sætter sammen i teamet, og et fælles dashboard i Power BI.

De større forandringer kender jeg fra Novo Nordisk, hvor jeg var projektleder i lidt over tre år. Jeg ledte tre digitaliseringsprojekter i Quality & Compliance med et samlet budget på 12 mio. kr. Undervejs stod jeg for implementeringen af et nyt system til dokumenthåndtering for 400 brugere på tre lokationer. Det gik i luften til tiden og under budget, og jeg oplærte 25 kolleger i systemet. Her lærte jeg at holde styr på store budgetter og at få IT, Quality og eksterne leverandører til at trække i samme retning.

Jeg har ikke arbejdet med udbud og kontraktstyring før. I Novo Nordisk koordinerede jeg leverandører i Indien og Polen, og det er det nærmeste, jeg kommer. Som Business Analyst i Netcompany arbejdede jeg desuden med kravspecifikation og test i offentlige it-projekter, blandt andet for Skattestyrelsen. Selve fagområdet vil jeg sætte mig grundigt ind i, og jeg vil starte med at lytte til dem i forretningen, der arbejder med udbud og kontrakter.

Jeg tror på små skridt, hurtig feedback og tal, der viser, om vi rammer rigtigt. Men de små skridt skal pege i samme retning, og det er roadmappens opgave. Den blanding af brugerfokus og struktur vil jeg gerne tage med til Amgros.

Jeg håber, vi kan tage en snak om, hvad jeg kan bidrage med hos jer.

Med venlig hilsen
Sara Lund
```

*Notes from the writer:* Tenders and contract management is the main gap. The letter says so plainly and points to vendor coordination and public IT projects as the closest experience. If you've worked on a tender or a vendor contract, add it to your database. The fit analysis credits you with managing roadmaps, but your database doesn't show that, so the letter only says how you'd think about the roadmap. The ad text doesn't say what Amgros does, so the letter doesn't spell out a life-science link beyond naming Novo Nordisk. The ad names no contact person, so the greeting is 'Kære ansættelsesudvalg'.

## Follow-up: why the local drafts went wrong

Same two letters, local model only, one input change at a time, scored by the same blind reviewer.
"Unbacked" counts claims the career database doesn't support (invented motivation, stale time words, inflated scope).

| Local model with… | KOMBIT: accuracy / unbacked | Amgros: accuracy / unbacked | Voice (KOMBIT, Amgros) |
|---|---|---|---|
| Original setup (~300 words) | 4 / 10 | 4 / 9 | 4, 5 |
| Original setup, short (~175 words) | 4 / 8 | 6 / 4 | 5, 5 |
| **No fit analysis + "don't copy time words from old letters"** | **8 / 4 (mostly mild)** | **8 / 3 (mild)** | **7, 8** |
| Facts and ad only (no style guide, no example letters) | 6 / 5 | 5 / 6 | 4, 3 |
| Plan which facts to use first, then write | 6 / 3 | 7 / 4 | 5, 6 |
| *Claude, original setup (for reference)* | *9* | *10* | *7, 7* |

* **Misleading input was the biggest cause.** The automatic fit analysis is written by the same local model,
  and its own inventions ("security-critical" angle, "roadmap ownership") were copied into the letters. Old
  letters supplied the stale "sidste år". Removing those doubled factual accuracy (4 → 8).
* **Less information is not better.** Dropping the style guide and example letters made the voice much worse
  and didn't improve accuracy.
* **The rest is model capacity.** Gemma 4 26B is a mixture-of-experts model with 3.8B active parameters per word.
  Even with clean input it adds mild invented motivation and "lessons" lifted from the ad, and its Danish is less
  idiomatic. Claude resisted the same misleading input.

Changed in the app after this test: drafts are now written without the automatic fit analysis, the writer must
take every date from the career database, and the fact check and voice check flag relative time words and
invented history.

## Round 3: a bigger local model (Gemma 4 31B, dense)

The app as it is now (no fit analysis in the writing prompt, dates only from the database), same prompts, scored blind
by the same reviewer alongside Claude's drafts. Scores are factual accuracy / voice / Danish (1–10), then unbacked claims.

| | KOMBIT letter | Amgros letter | KOMBIT CV | Time per document on this Mac |
|---|---|---|---|---|
| Gemma 4 26B (mixture of experts, 3.8B active) | 7 / 5 / 7 · 4 | 8 / 6 / 7 · 3 | 6 / 4 / 7 · 4 | ~2.6 min |
| Gemma 4 31B (dense) | 8 / 7 / 7 · 4 | 9 / 7 / 6 · 1 | 7 / 5 / 6 · 4 | ~4 min |
| Claude Opus 5.5 | 9 / 7 / 9 · 3 | 10 / 7 / 9 · 0 | 10 / 8 / 9 · 0 | (not measured) |

Ranking for "send with the least editing" was Claude > 31B > 26B in all three cases. Neither local model mentions
the real gaps (no election experience, no tendering experience), and both still add characterisations like
"kritiske systemer" or "fintech"; Claude names the gaps honestly and its Danish is more natural.

**Fact checking.** The same two flawed letters, checked by each model (how many of the reviewer's 19 problems were flagged):

| Checker design | Gemma 4 26B | Gemma 4 31B |
|---|---|---|
| Old: "list every claim" | 0 of 19 | 1 of 19 |
| New: a verdict for every sentence (now in the app) | 13 of 19 (~37 s per letter) | 12 of 19 (~170 s) |

The design mattered far more than model size: the app now checks sentence by sentence, and locally it uses the fast 26B.

**Recommended local setup:** Gemma 4 31B for Writing, Gemma 4 26B for Analysis (the app now picks this automatically
when both are installed). The 31B needs about 21–27 GB of memory while it runs, so the two models take turns.

## Round 4: telling the local models to name the main gap honestly

Claude named real gaps on its own ("I haven't worked with elections before"); the local models didn't. The letter
format now has the model decide the most important unbacked must-have (or new field) *before* writing, and put one
plain sentence about it in the letter. Tested on KOMBIT and Amgros (real gaps) and a fictional Nordlys Bank ad that
matches her career database completely (no real gap). Scores: factual accuracy and gap honesty (1–10), blind.

| | Without the rule | Rule, first version | Rule, final version |
|---|---|---|---|
| 31B, Amgros (gap: tendering) | facts 9, gap 5 | facts 6, gap 7 | **facts 9, gap 8**: "Jeg har ikke arbejdet direkte med udbud og kontraktstyring, men…" |
| 26B, Amgros | facts 8, gap 4 | facts 7, gap 4 | **facts 9, gap 7**: "Selvom jeg ikke har arbejdet specifikt med udbud…" |
| 31B, KOMBIT (gap: elections) | facts 8, gap 5 | facts 5, gap 3 | facts 9, gap 5 (gap not named) |
| 26B, KOMBIT | facts 7, gap 4 | facts 6, gap 3 | facts 6, gap 3 (spotted the gap, left it out of the letter) |
| Both, Nordlys (no gap) | – | facts 7–9, no invented gap | facts 9, no invented gap |

* The first version brought back the stale "sidste år" in 4 of 6 letters: the date rule in the system prompt wasn't
  enough. The final version removes relative time words from the example letters before the model sees them and
  repeats the date rule at the end of the request: 0 of 6.
* A gap in a listed must-have is now stated honestly by both models, at no cost to accuracy. A gap that's about the
  field rather than a listed requirement (elections for KOMBIT) is still usually missed. When the model spots a gap
  but leaves it out of the letter, the app now shows the gap and the ready-made sentence in the writer's notes.
* One letter per model and job per round: treat these as indications, not precise measurements.
