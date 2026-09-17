# Read/unread sync and conversation actions: readiness review

Review date: 2026-09-17. Base: upstream `b5cafc2ed19e9adda7625b03c16328d5dc8b91ac`.
Branch: `port/read-sync-and-actions`.

**Decision: port complete; hold the remaining changes from release and additional PRs.**
The combined candidate passes automated tests, but the findings below prevent
calling it ready to ship. This is a separate review pass by the author, not an
independent maintainer review or a fresh live-macOS acceptance test.

## What was ported

- Conversation menu: pin/unpin, read/unread, hide/show alerts, delete with a
  confirmation dialog, icons, and the unread keyboard shortcut.
- Durable pending read actions, retries, metadata-only Mac read snapshots,
  alias reconciliation, reaction activity timestamps, older unread threads,
  ordered gestures, faster fallback polling, and visible failures.
- Conversation-based badge totals, local group unread behavior, bridge
  installation changes, tests, and documentation.

The integration includes the substance of submitted PRs #101 and #102. Before
extracting more PRs, rebase on their merged versions (or explicitly account for
the dependencies) to avoid submitting their fixes twice. Nothing in this branch
has been deployed to the installed plugin or the Mac.

Upstream Send Later behavior, message/link context actions, Review contact,
and all three Mark All Read entry points (header, A shortcut, bar right-click)
are retained. The prior removal of Mark All Read is deliberately not ported.

## Findings that block release

### P1: a queued Mark All Read has no original-message boundary

`read-sync.ts` (`queueReadIntent`, `reconcileReadIntents`) retains the `*` intent
until every current unread is clear. `collector.ts` sends `--all` when that
intent becomes due. The per-chat `--through` protection is not used for it.

Reproduction: queue Mark All Read while offline or make its first attempt fail;
a new inbound arrives; reconnect/retry. The eventual `--all` includes the new
message, even though it was not present when the user clicked. A pure-function
probe confirms the intent remains queued with `seen: ""` after a newer unread
snapshot; the CLI dispatch path then unconditionally chooses `--all`.

Acceptance: bind the action to the original scope/time, or cancel it when
newer activity makes a global click unsafe. Add a process-level regression
covering an offline click, a newer inbound, and reconnection. Include groups
and aliases; do not silently trade this for clearing an unrelated thread.

### P1: destructive targeting is not verified before the click

`bridge/mac/imsg-read` (`select_chat`, `delete_conversation`) treats a successful
`open imessage://...` plus a fixed delay as proof of which conversation is
selected. It does not read back the selected identity. `chat_present` checks a
chat row's existence, not its active-versus-Recoverable status. The initial
presence check does not gate selection or deletion.

A selection failure that still returns success can act on another conversation.
A post-click failure cannot undo that action. Conversely, a retained chat row
can make a successful deletion look like failure. These are code-review risks;
we did not attempt destructive actions on real data to reproduce them.

Acceptance: verify the selected target before any destructive action and verify
active/recoverable conversation state afterward. Use disposable, explicitly
approved test conversations on a Mac. If identity verification cannot be made
reliable, omit Delete from the first menu PR. Pin/mute/read also need wrong-target
and delayed-selection acceptance coverage.

### P2: Mac actions block the only collector

`collector.ts` (`runReadAction`) uses synchronous execution with a 180-second
timeout inside `collect`. The widget queues subsequent refreshes while that
collector is running. Pin/mute/delete use separate synchronous 60-second calls.
An Accessibility prompt, a stalled SSH call, or lock contention therefore delays
normal read-state updates and notifications as well as the action itself.

Acceptance: separate bounded action processing from polling while preserving a
single state owner, durable intent, ordering, and acknowledgement. A slow-action
integration test should demonstrate that unrelated incoming-message polling
continues. Merely reducing the timeout risks truncating macOS consent prompts.

### P2: persistent Mark All failure can starve unrelated actions

`collector.ts` selects only `pendingReads["*"]` while it exists. Even when its
retry deadline is in the future, no per-chat candidate runs. This preserves
ordering but indefinitely blocks later explicit per-chat gestures if the global
action cannot succeed.

Acceptance: define cancellation/supersession for a failed global operation and
prove a later explicit gesture can make progress without being undone by an
older retry. Coordinate this with the boundary fix above.

## Findings fixed during this port/review

- Resolved merge conflicts without losing scheduled-message exclusion from
  newest-message selection and rendered read boundaries.
- Restored Mark All Read and the upstream Review contact menu item.
- Added the conversation menu/delete dialog to the keyboard-focus guard.
- Pin/mute/delete no longer acknowledge unavailable verification as success.
  Flag settling retries transient unavailable readings within its bounded wait.
- Already-satisfied pin/mute actions do not select or click again. Pin/mute
  capture the prior foreground app before selecting a conversation.
- Complete Mac snapshots preserve local-only group unread overrides. A CLI
  regression proves the override survives another poll and clears on mark-all.
- Demo packaging includes the new icons and supplies the avatar cache contract;
  it can capture menus and confirmation without operating the real bridge.
- Included #102's final bounded settling behavior and regression tests, rather
  than the earlier local variant that stopped on a transient database failure.

## Validation and limits

| Check | Result |
| --- | --- |
| Full Bun suite | 600 passed, 0 failed |
| Python Mac bridge discovery | 119 passed |
| Headless Qt read-state tests | 3 scenarios passed, plus setup/cleanup |
| Generated `ReadSync.mjs` | Rebuilt output matches committed module |
| Python compile | imsg, imsg-read, read_state.py pass |
| QML parser | BarWidget.qml and BlipView.qml parse with qmlformat |
| Shell syntax | modified demo and Mac installer pass bash -n |
| Whitespace | git diff --check passes |
| Visual review | synthetic menu and confirmation inspected; icons and labels present |
| Shellcheck | Not run locally: executable unavailable; CI retains its check |
| GitHub CI for this candidate | Not run; no candidate PR published |
| Live Mac/iPhone acceptance | Not performed for this port |

The demo log has no QML errors after correcting its host contract. It reports a
host-portal registration warning, unrelated to the conversation renderer. The
screenshots are entirely invented fixture conversations:

- [Conversation menu](review-assets/conversation-menu.png)
- [Delete confirmation](review-assets/delete-confirmation.png)

Linux synthetic tests prove local state-machine behavior, not that every macOS
version exposes the expected menu or that iCloud completes a remote mutation.
The new read-state query also needs performance measurements on a large real
metadata-only database before accepting a ten-second full-snapshot cadence.

## Cleanup and product decisions before PR extraction

- Remove superseded `pushRead`, `pushReadCommand`, `pushReadArgs`, and
  `markUnreadOnMac` paths and replace their legacy tests with tests of the active
  durable runner. Keeping tests of unused code inflates apparent coverage.
- Separate sync reliability from menu actions. Consider excluding Delete until
  its target-verification finding is resolved.
- Obtain maintainer agreement on counting conversations instead of messages;
  this is a product behavior change, separate from #101's consistency bug.
- Reconcile documentation around muted conversations, local-only group actions,
  read-state heuristics, and the remaining Mac UI limitations.
- Tighten the QML test harness and reduce broad text-shape assertions. Retain
  runtime and process-level tests for the behavior each PR claims to fix.

Recommended next PR order after addressing the findings: durable sync/read-state
reconciliation, then conversation menu actions. Keep the two already-submitted
small fixes independent.
