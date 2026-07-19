# Proposal Radar redesign QA

- Source visual truth: `/var/folders/cz/pyq35x4j5wb0t9b664drgr300000gn/T/TemporaryItems/NSIRD_screencaptureui_eRjI0n/Screenshot 2026-07-17 at 23.59.55.png`
- Original mobile issue evidence: `/var/folders/cz/pyq35x4j5wb0t9b664drgr300000gn/T/codex-clipboard-8492ebef-31b3-4e03-9116-60fb6869a65b.png`
- Status-control issue evidence: `/var/folders/cz/pyq35x4j5wb0t9b664drgr300000gn/T/TemporaryItems/NSIRD_screencaptureui_4tm9Ta/Screenshot 2026-07-18 at 01.32.30.png`
- Implementation inbox capture: `design-qa-mobile-inbox.png`
- Implementation drawer capture: `design-qa-mobile-drawer.png`
- Simplified card controls capture: `design-qa-status-mobile.png`
- Simplified status drawer capture: `design-qa-status-drawer-mobile.png`
- Combined source/implementation comparison: `design-qa-comparison.png`
- Timeline analytics before capture: `design-qa-timeline-before.png`
- Timeline analytics accepted capture: `design-qa-timeline-desktop.png`
- Timeline analytics comparison: `design-qa-timeline-comparison.png`
- Primary viewport: 390 × 844
- Secondary viewport: 1440 × 900
- State: authenticated job inbox and open job-details drawer

## Comparison scope

The source is a Salom AI Business landing page, while the implementation is an operations dashboard. The requested fidelity target is therefore its visual system, not its page composition: warm cream canvas, deep navy type and structural surfaces, electric lime emphasis, sky-blue actions, crisp white content cards, restrained borders, compact rounded controls, and confident heavy headings.

## Full-view comparison evidence

`design-qa-comparison.png` places the complete Salom AI reference and the rendered 390 × 844 drawer in one image. The implementation now uses the same cream/navy/lime/blue balance and surface contrast while preserving the tracker’s information density. The live mobile inbox and desktop dashboard were also inspected for consistency and viewport overflow.

## Focused-region evidence

A separate crop was not needed because the 390 × 844 drawer capture renders the affected top region at readable size. The fixed top bar, drag handle, close control, first status, age, full title, metadata, status grid, and Upwork action are all visible in the capture. Browser geometry measured the sheet at y=67.5 with a dedicated 52px top bar and the content scroller at `scrollTop=0`.

## Comparison history

### Iteration 1 findings

- [P1] Mobile drawer header disappeared beneath Safari chrome.
  - Evidence: the original mobile screenshot clips the status row and close control at the top edge.
  - Fix: split the drawer into a non-scrolling safe-area top bar and an independent content scroller; use `dvh`, `env(safe-area-inset-top)`, overscroll containment, and reset content scroll position whenever a job opens.
  - Post-fix evidence: `design-qa-mobile-drawer.png` shows the complete top bar and title region with no clipping.
- [P1] The former dark-green theme did not match the selected Salom AI visual language.
  - Evidence: the original tracker used near-black green surfaces throughout; the reference uses a warm cream canvas with navy, lime, blue, and white contrast blocks.
  - Fix: replace the complete token system and every major component state, including navigation, search, cards, status chips, drawer, forms, analytics, login, loading, empty, focus, and mobile navigation.
  - Post-fix evidence: `design-qa-comparison.png` shows the intended palette and hierarchy carried across the tracker.

### Final pass

No actionable P0/P1/P2 issues remain.

- Fonts and typography: system Inter/SF-style stack matches the reference’s geometric sans-serif character; display headings use stronger optical weight, compact tracking, and stable wrapping. Long real Upwork titles wrap without clipping.
- Spacing and layout rhythm: 16px mobile gutters, compact cards, 11–16px control radii, consistent section rules, and stable desktop/sidebar spacing match the reference’s clean density. No horizontal overflow at 390 or 1440.
- Colors and visual tokens: cream canvas, navy structure/text, lime selection/high-signal states, blue primary actions, and white cards map directly to the source intent. Semantic pipeline colors remain distinct without breaking the brand palette.
- Image quality and assets: the dashboard does not require the landing page’s hero photography, so no fake image substitute was introduced. UI icons use the Phosphor icon family rather than handcrafted SVG or CSS icon art.
- Copy and content: operational labels remain concise and unchanged; dynamic job content remains readable and visually subordinate to actions.
- Icons: all redesigned icons use one regular-weight Phosphor family with consistent sizing and alignment.
- States and interactions: authenticated reload, loading, populated inbox, bottom navigation, job open/close, fixed drawer header, and desktop responsive state were tested. No browser console errors were reported.
- Accessibility: labeled controls, visible focus states, reduced-motion support, sufficient contrast, practical mobile targets, semantic buttons/links, and safe-area accommodation are present.

### Status-control iteration

- [P1] The drawer removed the current status from its action list, so every update visibly changed and reordered the available controls.
  - Fix: keep Applied, Viewed, Replied, Interview, and Won in a permanent order; keep the current stage visible, selected, and disabled; place Lost and Didn't apply in one stable More menu.
  - Evidence: `design-qa-status-drawer-mobile.png` shows six large top-level targets in a fixed 2 × 3 mobile grid, with Viewed still present and marked Current. The live transition from Viewed to Replied preserved the exact order and moved only the selected state.
- [P1] Updating a job required opening its details drawer.
  - Fix: add two large inline card controls: one context-aware next action and one complete status selector. The next action advances the normal pipeline while the selector handles corrections, regressions, Lost, and Didn't apply.
  - Evidence: `design-qa-status-mobile.png` shows Mark applied and Change status directly on every inbox card. A synthetic live job successfully moved New → Applied from the card, Applied → Viewed from the selector, and Viewed → Replied from the drawer.
- [P2] The former controls were too small and crowded for confident mobile use.
  - Fix: use 46px card controls and 58px drawer targets on mobile, with Phosphor icons and explicit labels.
  - Post-fix checks: 390 × 844 rendered with `scrollWidth = innerWidth`, the top section remained visible, the temporary QA record was removed after testing, and the browser console contained no errors.

### Timeline, automatic application, and search iteration

1. [P1] Performance data was all-time only, so recent changes and month-over-month results could not be evaluated.
   - Fix: add a persistent timeline control with Last 30 days, the current four calendar months, the current year, All time, and an inclusive custom date range.
   - Evidence: the accepted live DOM exposes Last 30 days, July 2026, June 2026, May 2026, April 2026, 2026, All time, and Custom. Selecting May changed the live funnel from 17 July-period applications to 0 May applications and updated every rate and hook table from the same range.
   - Visual evidence: `design-qa-timeline-comparison.png` places the pre-change all-time view beside the accepted timeline view.
2. [P1] Proposal generation required a redundant confirmation even though generation means the application was sent in the normal workflow.
   - Fix: proposal generation now records Applied, sets the application timestamp, and includes it in analytics immediately. Didn't apply remains the explicit correction path and removes the record from performance calculations.
   - Evidence: a disposable local job moved New → Applied on proposal generation, appeared in July analytics, then moved to Didn't apply and disappeared from the same range. No production job data was changed for this test.
3. [P1] Multi-word search required all tokens and only covered a subset of stored fields.
   - Fix: search is now global across tabs, Unicode-aware, any-token matching, relevance-ranked, and covers title, brief, skills, matched terms, proposal, screening answers, budget, link, ID, tier, hook, status, labels, and notes.
   - Evidence: the deployed search `Airbridge completely-unrelated` returned the single Airbridge job despite the second token not matching. A disposable local record was also found from both its description and Cloudflare skill field while searching from the inbox tab after it had moved to Didn't apply.
4. [P2] Timeline controls needed to remain usable on narrow screens without turning the header into a large form.
   - Fix: presets use one horizontally scrollable 42px control row on mobile; custom dates expand only when selected and use full-width native date inputs plus one Apply action.
   - Evidence limit: the accepted in-app Browser capture was desktop-width. Mobile behavior was verified from the responsive CSS and semantic DOM, but this iteration does not include a fresh physical-iPhone screenshot.

## Follow-up polish

- [P3] The icon font package includes fallback font formats that increase deployed asset storage, although supported browsers download only the compressed WOFF2 resource.

final result: passed
