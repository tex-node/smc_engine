# Phase 14B — Telegram Opportunity Notifications

**Date:** 2026-10-08
**Branch:** feature/gui-workstation
**Constraint:** Outbound notifications only. No bot commands. No trade execution. LIVE_EXECUTION_ENABLED = False unchanged.

---

## 1. Summary

This phase adds outbound Telegram push notifications for opportunity state changes. The background watcher already dispatches alerts to `alerts` (SQLite) and `events` (SSE); Telegram is a third sink in that same dispatch path, wired after the existing two sinks.

Key properties:
- Telegram is **outbound only** — no polling, no bot commands, no execution
- Disabled gracefully when `telegram.txt` is absent or malformed — scanner continues normally
- Bounded queue (50 messages), daemon background thread, max 2 retries with exponential backoff
- Credentials never appear in logs, API responses, or error messages
- Deduplication at two layers: DB (`record_event_once()`) and in-process (`_sent_keys`)

---

## 2. Files Changed

| File | Change |
|---|---|
| `.gitignore` | Added `telegram.txt` and `.venv/telegram.txt` |
| `src/smc_engine/telegram.py` | **NEW** — credential loader, message formatter, TelegramNotifier |
| `src/smc_engine/web/hub.py` | Import TelegramNotifier; instantiate in `__init__`; wire into `_dispatch_opportunity_events()`; expose diagnostics in `opportunity_diagnostics()` |
| `tests/test_telegram.py` | **NEW** — 22 required tests + 1 bonus (23 total, all passing) |

---

## 3. Architecture

```
_dispatch_opportunity_events(res)
  │
  ├─ web.add_alert()        → SQLite alerts table
  ├─ events.publish()       → SSE ring buffer
  └─ telegram.notify(ev, opp)  [new]
       │
       └─ if enabled and kind in enabled_kinds:
            └─ _queue.put_nowait(text)
                 │
                 └─ background worker thread
                      └─ _send_with_retry()
                           └─ _send_telegram_message(token, chat_id, text)
                                └─ POST api.telegram.org/bot{token}/sendMessage
```

The Telegram call is wrapped in a `try/except` that swallows all errors — a send failure **cannot** affect the scan cycle.

---

## 4. Credential File

Location: `C:\smc_engine\telegram.txt` (NOT tracked by git)

Format:
```
Token     = "...";
ChatID    = "...";
```

Path resolution order (hub.py):
1. Same directory as the DB file (`smc_engine_gui.sqlite3`)
2. Repository root (`C:\smc_engine\telegram.txt`)
3. `.venv\telegram.txt` under repository root

If none found: `TelegramNotifier("")` — disabled, scanner continues.

---

## 5. Default Notification Policy

| Event kind | Default |
|---|---|
| READY | ON |
| ENTRY_TRIGGERED | ON |
| CONVERTED_TO_SETUP | ON |
| EXECUTION_READY | ON |
| OPPORTUNITY_SUPERSEDED | ON |
| INVALIDATED | ON |
| EXPIRED | OFF |
| OPPORTUNITY_CREATED | OFF |
| OPPORTUNITY_ADVANCED | OFF |
| POI_FOUND | OFF |
| IDM_CONFIRMED | OFF |

---

## 6. Diagnostics

`GET /api/opportunities/diagnostics` now includes Telegram status keys:

```json
{
  "telegram_enabled": true,
  "telegram_configured": true,
  "telegram_last_success": "2026-10-08T14:00:00Z",
  "telegram_last_failure": null,
  "telegram_send_count": 3,
  "telegram_failure_count": 0,
  "telegram_deduplicated_count": 0
}
```

No credentials, no token, no chat_id ever appear in this response.

---

## 7. Rate Limiting & Retry

| Parameter | Value |
|---|---|
| Min send interval | 1.0 s |
| Max retries | 2 |
| Initial retry backoff | 2 s |
| Max retry backoff | 60 s |
| Queue size (max) | 50 messages |
| HTTP timeout | 10 s |
| 429 retry_after | Extracted from Telegram response body, capped at 60 s |

---

## 8. Test Coverage

`tests/test_telegram.py` — 23 tests, all passing:

| # | Test | Category |
|---|---|---|
| 1 | `test_credentials_load_successfully` | Credential loading |
| 2 | `test_malformed_credential_file_handled_safely` | Error handling |
| 3 | `test_missing_credential_file_handled_safely` | Error handling |
| 4 | `test_credentials_never_appear_in_logs` | Security |
| 5 | `test_credentials_never_appear_in_api_responses` | Security |
| 6 | `test_opportunity_created_notification` | Message formatting |
| 7 | `test_ready_notification` | Message formatting |
| 8 | `test_entry_triggered_notification` | Message formatting |
| 9 | `test_converted_to_setup_notification` | Message formatting |
| 10 | `test_execution_ready_notification` | Message formatting |
| 11 | `test_superseded_notification` | Message formatting |
| 12 | `test_invalidated_notification` | Message formatting |
| 13 | `test_duplicate_event_sends_once` | Deduplication |
| 14 | `test_restart_does_not_resend_events` | Deduplication |
| 15 | `test_telegram_failure_does_not_stop_watcher` | Failure isolation |
| 16 | `test_telegram_timeout_does_not_stop_scanner` | Failure isolation |
| 17 | `test_telegram_429_retry_is_bounded` | Rate limiting |
| 18 | `test_multiple_symbols_remain_isolated` | Multi-symbol |
| 19 | `test_browser_selection_is_irrelevant` | UI-independence |
| 20 | `test_no_browser_discovery_still_sends_notification` | UI-independence |
| 21 | `test_entry_triggered_never_formatted_as_execution_ready` | Semantic correctness |
| 22 | `test_no_credentials_appear_in_test_output` | Security |
| 23 | `test_disabled_kinds_do_not_notify` | Default policy (bonus) |

---

## 9. Safety Verification

| Constraint | Status |
|---|---|
| `LIVE_EXECUTION_ENABLED = False` | Unchanged |
| No broker orders, positions, pending orders | ✓ TelegramNotifier sends text messages only |
| No Gate A changes | ✓ |
| No Gate B fabrication | ✓ |
| No MT5 execution calls | ✓ |
| No Telegram bot commands | ✓ Outbound only; no polling, no command handler |
| Credentials never logged | ✓ Test 4 verifies |
| Credentials never in API responses | ✓ Test 5 verifies |
| `telegram.txt` not tracked by git | ✓ Added to `.gitignore` |
| Telegram failure cannot affect scan | ✓ Wrapped in bare `except Exception: pass` |

---

*Generated: 2026-10-08 — Phase 14B complete*
