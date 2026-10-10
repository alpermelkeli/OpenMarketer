# Analyzer evaluation 20261010T085633Z

- analyzer: apodex/apodex-1.1-mini:free (tier:fast), fallbacks anthropic/claude-haiku-5.5; limits 40 steps, $0.50
- judge: via **claude_code**, model left to that route; spends Claude Code subscription usage; cost not known; settings of the judge role not applied: temperature, max_tokens, structured_output, fallbacks; the role's own model (anthropic/claude-opus-5.5) did not answer; at most 30 evidence judgements per run
- runs per case: 1; run made by code f193fd6f645c12b3744b55d109dfe1ce5df18b1e with uncommitted changes; started 2026-10-10T08:56:33+00:00
- **scored again on 2026-10-10T09:41:34+00:00** from the stored drafts and verdicts, without a model call, by code f193fd6f645c12b3744b55d109dfe1ce5df18b1e with uncommitted changes: the scores below are that code's, not those the run itself computed

## Limits of this result

- The golden set is one repository (excalidraw). It does not show how the analyzer does on other repositories, stacks or shapes.
- The label of excalidraw was written by an AI assistant and has not been reviewed by a person: it can be wrong, and disagreement with it is a question, not an analyzer error.
- The label of excalidraw does not list every feature of the product. A drafted feature it lacks is not an error by that alone, and there is no precision for this case.
- Feature matching, evidence support and section agreement are a model's judgements. They vary between judge models and between the routes a judge is reached by, and can be wrong; the lists matter more than the ratios. Compare runs only when judge and route are the same.
- Features are matched one to one. A drafted feature that covers two expected ones matches one and leaves the other among the missed.
- The judge saw cited lines up to a size limit, at most 30 claims per run, and was not asked whether a feature marked live is reachable by users.
- With one run per case, min, median and max are that run's value and say nothing about how much runs differ.

## About the files of this run

- `raw/` holds the analyzer's drafts and `verdicts/` the judge's verdicts; scores are recomputed from these two.
- A verdict's `reason` is the judge's own short sentence about a public repository. It is a model's words, kept as written and cut to a fixed length.
- `excerpts/` (the cited repository lines shown to the judge) and `malformed_replies/` (what the judge wrote where it could not be read) are kept out of git by this folder's own `.gitignore`.

## excalidraw

- label: **written_by_ai_assistant_unreviewed** (written by an AI assistant from the repository, not reviewed by a person: not a human label); **not exhaustive**
- commit 4c00f31ddcc20086bac1020428819fe0b9a30bce; 1 run: 1 profile
- live in the draft, not live in the label, in any run (the costly error): **none**
- live in the draft and not in the label, in any run (to look at, not errors): infinite-hand-drawn-canvas, arrow-binding, undo-redo, zoom-pan, grid-zen-modes, frames, charts-from-spreadsheet, command-palette, element-linking, grouping-alignment, element-locking, stats-panel, eye-dropper, excalidraw-plus, react-component-library

The label was written by an AI assistant (Claude, working as a coding agent on this
evaluation) from the repository at the pinned commit, read through intake. It is not an
analyzer draft and did not start from one, and it is NOT a human label: no person has reviewed
it. Treat disagreement with it as a question, not as an analyzer error, until someone has.

The assistant that wrote the label is of the same model family as the judge on the Claude Code
route. Agreement between this label and that judge's reading is therefore weaker evidence
than it looks: the two may share the same blind spots.

The commit was the head of the default branch when the case was added.

What the label covers: the web app in `excalidraw-app/` (excalidraw.com) and the editor it is
built from. It is not an inventory of the monorepo; the docs site and the examples are left out.

The label is NOT exhaustive (`label_is_exhaustive: false`). It was kept to what a person can
check in an hour and leaves out real features of the editor (undo and redo, zoom and pan, the
command palette, grouping, locking, charts from pasted data, and more). A drafted feature that
is not in the label is therefore something to look at, not an error, and no precision is
computed for this case. What the label can show is the costly error: a drafted `live` feature
whose counterpart here is not `live`.

What the writer was unsure of:
- product.type: `consumer_app` for the hosted whiteboard. The same code is also shipped as a
  React component for developers, so `dev_tool` is defensible.
- `comments` and `presentations` are `unreleased` here because this app only advertises them
  (they belong to the paid Excalidraw+). The schema has no status for "a feature of another
  product"; what matters is that they must not be called `live`.
- `complex-arrow-bindings` is behind a flag that is off by default; whether it counts as a
  feature a user would recognise is a judgement.
- The two AI features and the offline app are `live` because the shipped interface reaches
  them, but they depend on a backend address and build settings in `.env` files, which intake
  does not read. Their confidence is lower for that reason.
- product.languages is left empty although the interface is translated into about forty
  languages; that is recorded as the feature `localization`.
- business_model is `free` for this app; the 14-day trial in the sidebar is Excalidraw+'s.
- measurement names Simple Analytics, loaded in production builds; whether event tracking is
  switched on depends on a build setting that could not be read. Sentry (error reports) is
  not listed as analytics.
- The audience is inferred from the README, with low confidence; pains are left empty.

| Metric | Runs counted | Min | Median | Max | Per run |
|---|---|---|---|---|---|
| product_name_matches | 1 of 1 | 1 | 1 | 1 | 1 |
| product_type_matches | 1 of 1 | 1 | 1 | 1 | 1 |
| platforms_exact | 1 of 1 | 1 | 1 | 1 | 1 |
| platforms_missed | 1 of 1 | 0 | 0 | 0 | 0 |
| platforms_extra | 1 of 1 | 0 | 0 | 0 | 0 |
| features_drafted | 1 of 1 | 28 | 28 | 28 | 28 |
| features_drafted_live | 1 of 1 | 28 | 28 | 28 | 28 |
| features_matched | 1 of 1 | 13 | 13 | 13 | 13 |
| features_missed | 1 of 1 | 5 | 5 | 5 | 5 |
| features_not_in_label | 1 of 1 | 15 | 15 | 15 | 15 |
| features_live_but_expected_not_live | 1 of 1 | 0 | 0 | 0 | 0 |
| features_live_and_not_in_label | 1 of 1 | 15 | 15 | 15 | 15 |
| feature_recall | 1 of 1 | 0.722 | 0.722 | 0.722 | 0.722 |
| feature_precision_exhaustive_label_only | 0 of 1 | - | - | - | - |
| status_agreement | 1 of 1 | 1 | 1 | 1 | 1 |
| claims_with_evidence | 1 of 1 | 1 | 1 | 1 | 1 |
| evidence_supported | 1 of 1 | 0.467 | 0.467 | 0.467 | 0.467 |
| evidence_at_least_partly_supported | 1 of 1 | 1 | 1 | 1 | 1 |
| evidence_claims_supported | 1 of 1 | 14 | 14 | 14 | 14 |
| evidence_claims_partly_supported | 1 of 1 | 16 | 16 | 16 | 16 |
| evidence_claims_not_supported | 1 of 1 | 0 | 0 | 0 | 0 |
| evidence_claims_not_asked | 1 of 1 | 3 | 3 | 3 | 3 |
| business_model_type_matches | 1 of 1 | 1 | 1 | 1 | 1 |
| analytics_tools_missed | 1 of 1 | 0 | 0 | 0 | 0 |
| analytics_entries_not_names | 1 of 1 | 1 | 1 | 1 | 1 |
| analyzer_cost_usd | 1 of 1 | 0 | 0 | 0 | 0 |
| analyzer_model_calls | 1 of 1 | 29 | 29 | 29 | 29 |
| analyzer_seconds | 1 of 1 | 169.1 | 169.1 | 169.1 | 169.1 |
| judge_cost_usd | 0 of 1 | - | - | - | - |
| judge_calls | 1 of 1 | 33 | 33 | 33 | 33 |

### Run 1: profile, apodex/apodex-1.1-mini:free, $0.0000, 29 model calls, 169s; judge claude-sonnet-5-5 via claude_code, cost unknown, 33 calls

- name: drafted 'Excalidraw', expected 'Excalidraw'; type: drafted consumer_app, expected consumer_app
- platforms missed: none; extra: none
- feature statuses: drafted 28 live, 0 unreleased, 0 unknown; expected 15 live, 3 unreleased, 0 unknown
- **every drafted feature is live (28 of 28)**: the draft tells nothing apart as unreleased or unknown
- **live in the draft, not live in the label (the costly error): none**
- matched 13, recall 13 of 18 (0.722)
- missed: end-to-end-encryption (live in the label), wireframe-to-code-ai (live in the label), complex-arrow-bindings (unreleased in the label), comments (unreleased in the label), presentations (unreleased in the label)
- not in the label, which is not exhaustive (to look at, not errors; no precision): infinite-hand-drawn-canvas, arrow-binding, undo-redo, zoom-pan, grid-zen-modes, frames, charts-from-spreadsheet, command-palette, element-linking, grouping-alignment, element-locking, stats-panel, eye-dropper, excalidraw-plus, react-component-library
- of these, live: infinite-hand-drawn-canvas, arrow-binding, undo-redo, zoom-pan, grid-zen-modes, frames, charts-from-spreadsheet, command-palette, element-linking, grouping-alignment, element-locking, stats-panel, eye-dropper, excalidraw-plus, react-component-library
- partial matches: drawing-tools ~ drawing-shape-tools, images-frames-and-embeds ~ embeddable-elements, shape-libraries ~ shape-libraries, text-to-diagram-ai ~ ai-text-to-diagram
- status of matched features: agrees for 13 of 13 (1); differs: none
- evidence: 14 supported, 16 partly supported, 0 not supported; strictly supported 14 of 30 (0.467), at least partly 30 of 30 (1)
- evidence supported: product, business_model, feature:infinite-hand-drawn-canvas, feature:drawing-shape-tools, feature:zoom-pan, feature:shareable-links, feature:local-first-autosave, feature:pwa-offline, feature:grid-zen-modes, feature:charts-from-spreadsheet, feature:mermaid-import, feature:ai-text-to-diagram, feature:command-palette, feature:embeddable-elements
- evidence partly supported (the cited lines show part of the claim), with the judge's reason:
  - brand: The theme.scss lines confirm all six colours (#6965db, #a8a5ff is not shown but #121212, #fff, #db6965, #f5c354 are), and the README shows an open-source, hand-drawn whiteboard with a 'PRs welcome' badge, but it does not show free pricing, translations, or the friendly, encouraging voice described.
  - audience: The cited lines show a hand-drawn whiteboard, collaboration, and embeddability as a React component, but they say nothing about the target audience, brainstorming/wireframing use, or real-time collaboration without account friction (only 'collaborative' and encrypted).
  - measurement: The cited lines confirm window.sa_event tracking gated by VITE_APP_ENABLE_TRACKING and the three whitelisted categories, and Firebase Firestore/Storage imports, but nothing shows that sa_event is Sentry Analytics (it is a Simple Analytics function; Sentry is only a dependency), nothing shows Vercel 
  - feature:arrow-binding: The action file shows an arrow binding toggle (arrows can bind to elements), but nothing shows labeled arrows, and the README lines cited are about export formats.
  - feature:undo-redo: The actionHistory file imports UndoIcon, RedoIcon and a History module and defines history actions, so undo/redo exists, but the cited lines don't show that it covers all edits, and the README lines are about drawing tools and arrows.
  - feature:export-png-svg-clipboard: The export dialog code shows toggles for selection-only, background and dark mode, but the shown lines do not mention PNG, SVG or clipboard output formats.
  - feature:open-json-format: The JSON export dialog shows a save-to-disk action (actionSaveFileToDisk), which suggests saving drawings to a file, but the cited lines never mention the .excalidraw extension, JSON format, or opening files, and the README lines are about shape libraries and localization.
  - feature:realtime-collaboration: The README only says 'Real-time collaboration', while the Collab.tsx and firebase.ts excerpts show real-time sync with encryption (decryptData/encryptData), cursor-related constants (CURSOR_SYNC_TIMEOUT) and a Collaborator type, but the shown lines don't explicitly confirm end-to-end encrypted rooms
  - feature:shape-libraries: The README lines only say 'Customizable', but DefaultSidebar.tsx imports LibraryMenu and LibraryIcon and defines a sidebar with a library tab (LIBRARY_SIDEBAR_TAB), which makes a browsable shape library in a sidebar likely, though the shown code does not explicitly show custom libraries.
  - feature:localization: The cited README lines cover only hand-drawn style and dark mode, but the i18n.ts file lists English plus 44 candidate languages; these are filtered by an 85% completion threshold, so the final count of 40+ is likely but not shown.
  - feature:frames: The lines show a frame tool (key F) and frame-related actions that add/remove elements to frames, supporting grouping via frames, but nothing shown mentions 'magic frames' or presenting content.
  - feature:element-linking: The cited lines show an element-link action and a link dialog that imports normalizeLink (suggesting URL handling), but they only show copying links to elements and the start of a dialog, not explicitly creating links from elements to external URLs.
  - feature:grouping-alignment: The cited lines are only imports from actionGroup.tsx and actionAlign.tsx (e.g. alignElements, group-related helpers like getSelectedGroupIds), which make grouping and aligning likely but show no implementation, and nothing about ungrouping or distributing.
  - feature:element-locking: The file name actionElementLock.ts and the imports of LockedIcon/UnlockedIcon, newElementWith and group-selection helpers strongly suggest an element lock/unlock action, but these lines are only imports and do not show the locking behavior or that it prevents edits.
  - feature:dark-mode: The theme.scss lines define a dark theme with its own color variables (implying a light default), but they show nothing about system preference detection, and the README lines cited are only about being free and open-source.
  - feature:stats-panel: The lines are only imports for a Stats component (STATS_PANELS, CanvasGrid, Dimension, MultiDimension, getCommonBounds, selection-related helpers), which strongly suggest a stats panel with canvas and selection metrics but do not show the rendered panel itself.
- evidence not supported (the cited lines do not show the claim), with the judge's reason: none
- evidence the judge was not asked about (the run's limit of questions): feature:eye-dropper, feature:excalidraw-plus, feature:react-component-library
- evidence with a malformed or failed verdict: none
- sections: brand both_filled; audience both_filled; business_model both_filled; measurement both_filled
- brand_voice, as judged: partly_agrees: Both describe a friendly, plain, hand-drawn tone, but the first stresses brevity, short imperative slogans and candid privacy/storage notes, while the second instead emphasizes open-source, community-driven contribution invitations, which the first does not mention.
- audience, as judged: agrees: Both describe people who sketch diagrams/wireframes/whiteboards alone or collaboratively plus developers embedding the editor; the second adds hand-drawn style and pain points but the core audience matches.
- business model type: same; drafted 'free', expected 'free'
- trial days: **differs**; drafted 14, expected not stated
- attribution: **differs**; drafted 'Firebase (Firestore + Storage) for collaboration and file storage; Vercel for hosting', expected not stated
- deep links: **differs**; drafted False, expected not stated
- palette: missed #5B57D1; extra #121212, #DB6965, #F5C354, #FFFFFF
- analytics tools: found simple analytics; missed none; drafted entries naming no expected tool: none
- analytics entries that are not a tool's name (a defect of the draft): 'Simple analytics via window.sa_event (Sentry Analytics), gated by VITE_APP_ENABLE_TRACKING and a whitelist of categories (command_palette, export, ai)'
