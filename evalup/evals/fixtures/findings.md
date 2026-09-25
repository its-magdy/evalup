# Design findings — helpdesk-demo (planted eval fixture)

Two findings with a live-observed symptom each. A generate session must read
this file and give each at least one case whose expectation fails on the
symptom, or list the finding under `known_gaps` in dataset.yaml.

1. **Invoice totals ignore the month asked.** "What did I spend on invoices in
   June 2026?" answered "You spent $412.50 on invoices in May 2026, across 4
   invoices." — the month in the question is not honoured; every invoice
   question returns the May 2026 figure.
2. **Any shift question returns the night-shift nurse count.** "How many nurses
   are on the day shift?" answered "There are 7 nurses on the night shift." —
   the shift named in the question is not honoured.

deferred:
- step 5 (patches): none offered in the lean first session
- step 7 (interview): route-target boundaries and business rules unconfirmed
