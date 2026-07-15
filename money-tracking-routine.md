# Money Tracking — daily routine SOP

Runs once a day (cron). Reads the WhatsApp group **"Money Tracking"**, applies each
update to the portfolio, logs it, and clears the messages so they aren't seen again.

Files:
- Holdings:  `~/.config/portfolio/holdings.json`
- State:     `~/.config/portfolio/money-tracking-state.json`  (`{ "last_processed_ts": "<ISO>" }`)
- Audit log: `~/.config/portfolio/money-tracking-log.jsonl`   (one JSON object per applied action)
- Bridge DB: `~/code/whatsapp-mcp/whatsapp-bridge/store/messages.db`

## Steps

1. **Ensure the WhatsApp bridge is running.** If `pgrep -f whatsapp-bridge-bin` is empty,
   start it: `cd ~/code/whatsapp-mcp/whatsapp-bridge && setsid -f ./whatsapp-bridge-bin >/tmp/wa-bridge.log 2>&1`,
   then wait until the log shows "Connected to WhatsApp".

2. **Find the group.** `list_chats(query="Money Tracking")`. If not found, also try "Money"/"Track".
   If still not found → nothing to do; exit quietly.

3. **Read new messages.** `list_messages(chat_jid=<group>, include_context=false, limit=100)`.
   Only process messages with timestamp **strictly after** `last_processed_ts` (skip if already done).
   Process in chronological order. Use each message's **sent timestamp** as the action date
   unless the text names a different date.

4. **Interpret each message** as a money action. Be conservative — if a message is ambiguous or
   not a money update, log it as `skipped` and leave it. Supported intents (free text / Hebrew or English):
   - **Buy**: e.g. "bought 5 GOOG @ $352", "קניתי 10 NVDA ב-180". → increase that holding's `shares`
     and update `avg_cost` as a weighted average (new_avg = (old_shares·old_avg + qty·price)/(old_shares+qty)).
     If it's a new ticker, add an `equity` holding (`kind:equity, ticker, name, shares, avg_cost, price_currency`).
   - **Sell**: reduce `shares` (keep `avg_cost`). Remove the holding if shares hit 0.
   - **Deposit / withdraw cash**: adjust top-level `cash` (₪). "added 5000 to cash", "הפקדתי 5000".
   - **Income**: "income 8000 salary", "קיבלתי משכורת 8000". → append to `income`:
     `{ "date": "<sent or stated date>", "source": "<text>", "amount": <number> }`.
   - **Fund / statement update**: a new ₪ value or cost for a fund (5131644 / 5127766) →
     update its `value_ils` / `cost_ils`, and re-anchor `ref_proxy` (current ^NDX) and `ref_fx`
     (current USD/ILS) using `python3 ~/code/portfolio-desktop/datafeed.py`-style fetch, or just
     run a quick Yahoo fetch for `^NDX` and `USDILS=X`.
   - **Correction**: "set cash to 12000", "GOOG shares = 20" → overwrite that field.

5. **Apply** the change to `holdings.json` (valid JSON, keep the `_comment`). Validate by running
   `python3 ~/code/portfolio-desktop/datafeed.py` — it must succeed and print sane totals.

6. **Audit**: append one line per action to the log:
   `{ "sent_at": <msg ts>, "done_at": <now ISO>, "msg_id": ..., "raw": "<text>", "action": {...}, "result": "applied|skipped", "note": "..." }`.

7. **Clear messages** so they aren't seen tomorrow:
   - Update `last_processed_ts` to the newest processed message timestamp.
   - Delete the processed rows from the bridge DB (local mirror only — does not touch the phone):
     `DELETE FROM messages WHERE chat_jid='<group>' AND id IN (...)`.

8. **Confirm**: send a short summary back to the group via `send_message`, e.g.
   "✅ Logged: +5 GOOG @ $352 · income ₪8,000 · cash → ₪12,000 (2 items)". Keep it brief.

9. The desktop widget auto-refreshes every 60s, so changes appear on their own. (Optional: restart it
   with `~/.local/bin/portfolio` twice — once to toggle off, once on — only if a structural change needs it.)

## Safety
- Never guess wildly. Ambiguous → skip + log + (optionally) ask in the group.
- The timestamp guard means re-running the routine is safe (idempotent).
- The audit log preserves every raw message even after the WhatsApp copy is cleared.
