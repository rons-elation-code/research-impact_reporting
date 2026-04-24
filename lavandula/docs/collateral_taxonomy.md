# Nonprofit Collateral Taxonomy — Revised Proposal for Review

**Date**: 2026-04-24 (Revision 3)
**Status**: Draft for external review
**Context**: Lavandula pipeline currently collects nonprofit annual/impact reports. This proposal expands scope to broader nonprofit fundraising and event collateral, for use as training data and reference material supporting Lavandula Design's product portfolio (reports, event invitations and tribute journals, auction catalogs, capital campaign identity systems, planned giving brochures, direct mail packages).

This is a starting point, not a finished taxonomy. The reviewer's domain expertise is the authority on anything below — corrections, additions, and collapses are expected.

## Revision notes

**rev 3 changes:**
- Added tier markers to every material type: `[web]` typically public and crawlable, `[mixed]` sometimes public, `[internal]` rarely or never public
- Restored **Capital Campaign / Fundraising Campaign Materials** as a first-class material group. Named capital campaigns ("West Wing Capital Campaign") are branded, identity-driven work with their own coordinated collateral system — not just a tag on other documents.
- Crawler signals (filename keywords, path keywords) target only `[web]` and `[mixed]` items. `[internal]` items are kept in the taxonomy for classification completeness and future acquisition via non-crawl means, but no crawler investment.

**rev 2 changes (from rev 1):**
- Restructured as a two-axis taxonomy (material type × event type), plus campaign/moment tags
- Added ~25 document types from consult review (gala tribute journal, case-for-support / case statement / prospectus triad, day-of suite, stewardship reports, direct mail components, sector-specific docs)
- Corrected terminology: `annual_fund` (program) vs `annual_fund_appeal` (solicitation); three distinct campaign case documents; `stakeholder_report` → `community_benefit_report`
- Dropped only `audit_990`, `financial_statement`, and corporate `sustainability_report` / `esg_report` / `csr_report` as standalone labels. Preserved breadth on everything else nonprofits genuinely produce.

---

## Tier concept — crawlable vs. internal

Two tiers, based on whether a document typically appears on a public website:

- **`[web]`** — Reliably public. Crawler actively targets. Filename keywords, path keywords, classifier labels optimized for these.
- **`[mixed]`** — Sometimes public, sometimes internal. Crawler catches them when they appear. Moderate keyword support.
- **`[internal]`** — Rarely or never on public websites (day-of event print, mailbox-only direct mail, bespoke donor materials). Kept in the taxonomy so the classifier has a label if one shows up, but **no crawler investment** and no filename keywords. Acquisition route for these is out of scope for the crawler (would require partner uploads, paid design archives, or direct engagement).

This is the most important structural change from rev 2: the taxonomy documents everything nonprofits produce, but the crawler only invests in what's actually reachable.

---

## Design: Two-axis classification + tags

Every document gets two labels plus optional tags:

1. **Material type** (always present) — what the document IS
2. **Event type** (nullable) — the event context, if any
3. **Campaign / moment tag** (zero or more) — giving_tuesday, year_end, emergency_appeal, capital_campaign, specific named campaign, annual_fund, etc.

For the classifier, a two-pass structure:
- **Pass 1**: Primary material bucket (Reports / Invitations / Programs-Journals / Appeals / Campaign Materials / Sponsorship / Major-Gift / Planned-Giving / Stewardship / Periodic / Membership / Day-of-Event / Other)
- **Pass 2**: Subtype within bucket + event_type tag alongside

---

## Material types (primary axis)

### Reports

- `annual_report` `[web]`
- `impact_report` `[web]`
- `year_in_review` `[web]`
- `community_benefit_report` `[web]` — health-system-specific, IRS Schedule H-mandated (replaces rev 1's `stakeholder_report` / `community_report`)
- `donor_impact_report` `[internal]` — personalized "your gift at work" to individual donors
- `endowed_fund_report` `[internal]` — annual stewardship report on a specific named fund

### Capital Campaign / Fundraising Campaign Materials

A capital campaign or comprehensive campaign is a branded, multi-year initiative with its own visual identity and a coordinated suite of collateral. Materials here are typically designed as a system under that campaign's identity.

- `campaign_case_statement` `[web]` — external polished brochure presenting the case (the public-facing flagship campaign piece)
- `campaign_case_for_support` `[mixed]` — comprehensive internal document (40–80pp), used by staff and committee members; sometimes published in slimmer public form
- `campaign_prospectus` `[mixed]` — gift-opportunity-specific: one named space, one program, one scholarship fund
- `campaign_gift_opportunities_menu` `[web]` — "Name the atrium: $2M" naming schedule
- `campaign_progress_update` `[web]` — periodic update piece ("We've raised $42M of $100M")
- `campaign_identity_package` `[web]` — the campaign brand guide / identity system document (logo, colors, typography, voice)
- `campaign_master_brochure` `[web]` — top-of-funnel public brochure introducing the campaign
- `campaign_pledge_form` / `campaign_commitment_form` `[mixed]` — sometimes standalone PDF, sometimes bound into case materials
- `campaign_groundbreaking_piece` `[web]` — event-specific keepsake at campaign milestones (groundbreaking, dedication, opening)
- `campaign_launch_package` `[web]` — materials for the public campaign launch event
- `fundraising_campaign_brochure` `[web]` — broader fundraising campaign material not tied to a capital campaign (annual campaign, seasonal drive, comprehensive campaign, matching challenge campaign)
- `feasibility_study_report` `[internal]` — pre-campaign feasibility testing document

### Invitations and event paper

- `save_the_date` `[mixed]` — usually mailed/emailed, sometimes archived as PDF
- `event_invitation` `[mixed]` — formal invitation piece, sometimes posted as PDF
- `rsvp_reply_card` `[internal]` — reply device bound to invitation, usually mail-only
- `event_announcement` `[web]` — broader announcement / save-the-date social

### Programs and journals (event print)

- `event_program` `[mixed]` — run-of-show, playbill, handed out at the door; sometimes archived online
- `tribute_journal` `[mixed]` — ad book / commemorative journal with honoree tributes, corporate ads, in-memoriams. Typically the highest-revenue printed piece at a gala. Occasionally archived publicly.
- `honoree_tribute_page` `[internal]` — individual tribute pages, rarely separate from the journal online

### Auction materials

- `live_auction_catalog` `[web]` — bid book with lot essays and photography, heavy design. Often posted for online preview bidding.
- `silent_auction_materials` `[mixed]` — bid sheets, lot cards (increasingly digital/app-driven)
- `auction_lot_display_card` `[internal]` — at-event lot signage
- `online_auction_microsite_design` `[web]` — web-native auction experience (captured as screenshots/static snapshots)

### Appeals (direct mail and digital)

- `appeal_letter` `[internal]` — the letter itself, mailbox-only
- `appeal_reply_device` `[internal]` — donation form insert
- `appeal_insert` / `appeal_lift_note` `[internal]` — secondary piece
- `appeal_outer_envelope` `[internal]` — when designed with teaser copy/imagery
- `digital_appeal` `[web]` — email template, landing-page design, social graphics
- `pledge_form` / `commitment_form` `[mixed]` — sometimes downloadable from web
- `response_card` `[internal]` — generic donation reply device

### Sponsorship

- `sponsor_prospectus` `[web]` — pre-sale pitch to prospective sponsors (promoted to attract sponsors, so often public)
- `sponsor_benefits_sheet` `[internal]` — fulfillment / deliverables list for committed sponsors

### Major gifts

- `major_gift_proposal` `[internal]` — bespoke document pitching a specific ask to a specific donor; by definition private
- `cultivation_piece` `[internal]` — printed piece left behind after a donor visit
- `donor_deck` `[internal]` — slideshow-style pitch deck (keep if observed in corpus)

### Planned giving

- `planned_giving_brochure` `[web]` — general planned-giving program intro
- `bequest_guide` / `sample_bequest_language` `[web]`
- `legacy_society_newsletter` `[mixed]` — periodic communication to planned-giving donors; sometimes posted to member-area of site
- `gift_vehicle_one_pager` `[web]` — CGA, CRT, QCD, donor-advised fund explainer

### Stewardship

- `donor_impact_report` `[internal]` (also listed under Reports)
- `endowed_fund_report` `[internal]` (also listed under Reports)
- `named_fund_brochure` `[mixed]` — acquisition piece for new endowed funds
- `donor_acknowledgment` `[internal]` — thank-you letter design, mailbox-only

### Periodic publications

- `donor_newsletter` `[web]` — 4–8pp print, audience is current donors, often archived online
- `planned_giving_newsletter` `[web]` — quarterly 4pp, stable conventions, often archived
- `program_newsletter` / `constituent_newsletter` `[web]` — membership/constituent communication
- `magazine` `[web]` — alumni magazine, patient magazine, museum member magazine
- `annual_letter` `[web]` — president/CEO annual letter, often standalone on website

### Membership and giving society

- `membership_acquisition_brochure` `[web]`
- `membership_renewal_notice` `[internal]`
- `member_welcome_kit` `[internal]`
- `giving_society_material` `[web]` — "President's Circle," "1884 Society" cultivation pieces

### Day-of-event materials

All `[internal]` unless noted. These are produced for the event itself and rarely leave the venue. Kept in taxonomy for training-data completeness, but crawler won't find them.

- `menu_card` `[internal]`
- `table_card` / `table_number` `[internal]`
- `place_card` `[internal]`
- `seating_chart` `[internal]`
- `name_badge` `[internal]`
- `event_signage` `[internal]` — step-and-repeat, directional, stage signage
- `bid_paddle` / `bid_number_card` `[internal]`
- `fund_a_need_graphic` `[internal]` — screen graphic for live fund-a-need asks
- `hole_sponsor_sign` `[internal]` — golf
- `pairing_sheet` / `scorecard_sponsor` `[internal]` — golf
- `tee_gift_card` `[internal]` — golf
- `course_map` `[mixed]` — walk/run/ride; sometimes posted for participant reference
- `bib_design` `[internal]` — walk/run
- `finisher_certificate` `[internal]`

### Peer-to-peer fundraising

- `team_fundraising_kit` `[mixed]` — sometimes downloadable from P2P participant portal, often gated
- `participant_welcome_pack` `[internal]`

### Program/services collateral

- `program_brochure` `[web]` — general information about specific programs or services
- `services_guide` `[web]`
- `advocacy_piece` `[web]`

### Sector-specific (cross-cut)

- `viewbook` `[web]` — higher-ed / independent school admissions
- `grateful_patient_appeal` `[mixed]` — health-system-specific (published samples occasionally)
- `physician_referral_to_philanthropy` `[internal]` — health-system internal
- `parent_fund_appeal` `[mixed]` — higher-ed / independent school
- `reunion_giving_piece` `[mixed]` — higher-ed, sometimes posted
- `patron_recognition_wall_artwork` `[internal]` — permanent installations (arts/museum)

### Other

- `other_collateral` — catch-all
- `not_relevant` — classifier negative class

---

## Event types (secondary axis)

Used as the second label on any event-linked document. Nullable — many materials (case statement, planned giving brochure) have no event context.

### Formal / themed events
- `gala` — formal black-tie fundraising dinner
- `ball` — formally distinct (masquerade, winter ball, charity ball); the word is often the title even when visually gala-like
- `benefit_event` — generic "benefit" when no more specific label fits (benefit concert, benefit performance)

### Meal-based fundraisers
- `breakfast_fundraiser`
- `luncheon`
- `dinner_fundraiser`
- `cocktail_reception`

### Athletic / participatory
- `golf_tournament` (covers golf classic, outing, invitational — naming variations)
- `walk_run_event` (walkathon, 5K, 10K, fun run)
- `ride_event` (bikeathon, charity ride, cycling classic)

### Themed fundraisers
- `fashion_show`
- `food_wine_event` — wine tasting, chef-driven, cookoff, BBQ
- `derby_polo_regatta` — Kentucky Derby parties, polo classics, regattas

### Broadcast fundraisers
- `telethon`
- `radiothon`

### Auction as event
- `auction_event` — when the auction itself is the headline event

---

## Campaign / moment tags

Zero or more per document. Not mutually exclusive with material type or event type.

- `giving_tuesday`
- `year_end_appeal`
- `spring_appeal`
- `emergency_appeal` / `disaster_appeal`
- `matching_challenge`
- `giving_day` — org-specific ("Day of Giving," "24 Hours of…")
- `capital_campaign` — context tag for any document produced during an active capital campaign (paired with named campaign when known)
- `comprehensive_campaign` — multi-year combined annual + capital + planned
- `annual_fund` — piece is part of the annual fund program
- `restricted_fund` — supports a specific restricted fund (scholarship, building, program)
- `named_campaign:{name}` — free-form slot for the specific campaign name ("West Wing Capital Campaign")

---

## Path keywords for crawler (Tier 1/mixed targets only)

### Strong — pass alone (candidate accepted on path match without anchor)

- `/annual-report`, `/annualreport`, `/impact`, `/our-impact`, `/transparency`, `/year-in-review`, `/reports`
- `/gala`, `/ball`, `/benefit`, `/luncheon`, `/breakfast`, `/dinner`, `/golf`, `/tournament`, `/auction`
- `/walk`, `/5k`, `/run`, `/ride`, `/regatta`, `/polo`, `/derby`
- `/capital-campaign`, `/campaign`, `/our-campaign`, `/the-campaign`, `/fundraising-campaign`
- `/case-for-support`, `/case-statement`, `/gift-opportunities`, `/naming-opportunities`
- `/planned-giving`, `/legacy`, `/bequest`, `/endowment`, `/estate-planning`
- `/sponsorship`, `/sponsor`, `/sponsors`, `/sponsorship-opportunities`
- `/newsletter`, `/magazine`, `/publications`
- `/events`, `/our-events`, `/upcoming-events`, `/special-events`
- `/membership`, `/friends-of`, `/giving-society`, `/societies`
- `/ways-to-give`, `/giving`, `/donate`, `/support-us`, `/how-to-help`
- `/giving-tuesday`, `/year-end`, `/annual-appeal`

### Weak — need anchor or filename match to count (generic CMS buckets)

- `/media`, `/press`, `/resources`, `/downloads`, `/library`, `/documents`, `/files`, `/assets`, `/uploads`

No path keywords for `[internal]`-only document types — crawler won't find them.

---

## Filename signals for heuristic grading

Only `[web]` and `[mixed]` items have filename signals. Internal-only items (day-of event materials, direct mail pieces, bespoke donor materials) have no dedicated keywords.

### Strong positive (filename contains these → likely in-scope)

- `annual-report`, `impact-report`, `year-in-review`, `yir`, `community-benefit-report`
- `gala`, `ball`, `benefit`, `luncheon`, `breakfast`, `dinner`, `cocktail`
- `golf-tournament`, `golf-classic`, `golf-outing`, `golf-invitational`
- `walkathon`, `walk-a-thon`, `5k`, `10k`, `fun-run`, `bikeathon`, `ride`
- `fashion-show`, `wine-tasting`, `cookoff`, `derby`, `polo`, `regatta`
- `telethon`, `radiothon`
- `auction`, `bid-book`, `catalog`, `catalogue`, `silent-auction`, `live-auction`
- `program`, `playbill`, `tribute`, `journal`, `ad-book`, `commemorative`
- `save-the-date`, `std`, `invitation`, `invite`
- `sponsorship`, `sponsor-prospectus`, `sponsor-package`
- `capital-campaign`, `fundraising-campaign`, `case-for-support`, `case-statement`, `prospectus`
- `gift-opportunities`, `naming-opportunities`, `campaign-update`, `campaign-brochure`
- `planned-giving`, `legacy`, `bequest`, `cga`, `crt`, `qcd`, `gift-vehicle`
- `pledge`, `commitment-form`
- `appeal`, `year-end`, `spring-appeal`, `annual-fund`
- `newsletter`, `magazine`, `quarterly`, `legacy-news`
- `membership`, `member-benefits`, `welcome-kit`
- `giving-society`, `friends-of`
- `viewbook`, `reunion`, `parent-fund`
- `giving-tuesday`, `day-of-giving`
- `program-brochure`, `services-guide`

### Strong negative (filename contains these → likely out-of-scope)

- `form` (unless pledge/commitment), `application`, `waiver`, `consent`, `release` (unless press-release)
- `policy`, `policies`, `guidelines`, `handbook`, `manual`, `terms`, `privacy`
- `coloring`, `campus-map`, `facility-map`, `directions`
- `schedule`, `agenda`, `minutes` (board), `bylaws`
- `instastories`, `lawn-signs`, `commencement` (unless fundraiser), `curriculum`, `syllabus`
- `bill`, `legislation`, `summary` (of bills)
- `checklist`, `template` (internal), `worksheet`
- `resume`, `cv`, `bio`, `request`, `inquiry`

### Year signal

Patterns matching `20[12]\d`, `FY-?\d{2}`, `FY20\d{2}` contribute positive signal on top of keyword matches.

---

## Out of scope (explicit drops)

Nonprofits either don't produce these or they have no design value worth training on.

- `audit_990` — IRS Form 990 PDFs pulled from GuideStar/ProPublica. Zero typography. Every 501(c)(3) has one per year — would flood the corpus.
- `financial_statement` — Auditor-produced. Minimal design. The design-relevant version lives inside annual reports as an appendix.
- `sustainability_report` / `esg_report` / `csr_report` — Corporate documents. Nonprofits don't produce these under those names. Kept as filename signals only (for disambiguation), not as standalone labels.

Everything else is **preserved**: `ball`, `telethon`, `radiothon`, `giving_tuesday`, `benefit_event`, meal-fundraiser types. These are real things nonprofits produce.

---

## Questions for the Reviewer

1. **Tier assignments** — are any `[web]` items actually mostly internal, or any `[internal]` items more publicly posted than I assumed? The tiering drives crawler investment, so this matters.

2. **Capital Campaign materials** — does the list capture the real design deliverables of a capital campaign engagement? Specifically:
   - Is `campaign_identity_package` a real thing or am I making up that category?
   - Is there a piece I've missed that typically anchors the campaign launch?
   - How does `campaign_master_brochure` differ from `campaign_case_statement` in a working shop?

3. **Fundraising vs. capital campaigns** — the taxonomy treats "capital campaign" and "fundraising campaign" as partially overlapping. Is that the right way a development shop would think about it, or are they really the same thing with different scope?

4. **Event-type coverage** — still weak on:
   - Food-service events (chef showcases, tasting events)
   - Faith-community events (interfaith breakfasts, parish fundraisers)
   - Youth/sports-tied events (jog-a-thons, spellathons)
   - Virtual-native events (virtual gala, online auction)

5. **Material-type coverage** — sector-specific documents are the likeliest gaps. If you've worked in health, higher-ed, arts, human services, or faith sectors: what do you produce regularly that isn't listed?

6. **Terminology corrections**:
   - `case_for_support` vs `case_statement` vs `campaign_prospectus` — are these distinct in your shop?
   - `annual_fund` vs `annual_fund_appeal` — is the program/solicitation distinction meaningful?
   - `membership` vs `giving_society` — is "Friends of X" a giving society or a membership in your shop's usage?

7. **Day-of event materials as `[internal]`** — is that tier assignment right, or do you see galas/golf tournaments posting menu PDFs, programs, or hole-sponsor galleries to their websites?

8. **Cross-cut structure** — does the two-axis (material × event) + tags model match how you think about these documents, or is a flat label more natural?

---

## Implementation caveats

- **Single source of truth**: once locked, this taxonomy should live in one reference file consumed by the crawler (keywords), classifier (labels), and dashboard (filters).
- **Tier 2 acquisition**: `[internal]` items won't come from the crawler. Acquisition routes if we later want them: direct partner uploads, paid design-archive subscriptions, bespoke engagement with individual nonprofits.
- **Multi-label classifier**: two-axis structure requires a multi-label classifier or two sequential calls. Prompt design enforces "exactly one material_type, optional event_type, zero-or-more tags."
- **Weak-tier path keywords are a precision risk**: `/media`, `/press`, `/resources`, `/documents`, `/files` are common CMS buckets that produce junk when used alone (observed in production: Fordham returned 207 false-positive PDFs via `/media`). They must always require a matching anchor or filename signal.
- **Signal hierarchy**: URL basename > alt/title/aria > anchor visible text > path keyword.
- **Renaming `reports` table**: under this expanded scope, `reports` becomes misleading. Proposed rename to `collaterals` (table and UI). Separate implementation task.
- **Backward compatibility**: existing classified rows under the old 5-label scheme need migration. Likely action: mark as `legacy_classification` and re-classify in batch or leave historical.
- **Two-pass classifier reliability**: with ~70 labels across the two axes, single-pass classification will be unreliable. Two-pass structure (primary material group → subtype) strongly recommended.
