-- Payment idempotency fix: make the Flutterwave numeric transaction id the
-- sole uniqueness key, not tx_ref.
--
-- A static Flutterwave *Payment Page* reuses one auto-generated tx_ref
-- ("Rave-Pages<pagesId>") across every payment made through that link. With a
-- UNIQUE(tx_ref) constraint, two different users paying through the same page
-- could never both be credited — the second createClaim insert collided on the
-- shared ref and returned 409 "already used by another account", locking out a
-- legitimate payer.
--
-- The correct identity of a payment is its unique numeric transaction id, which
-- is already enforced by payment_claims_flutterwave_tx_id_key (UNIQUE). We drop
-- the over-eager UNIQUE(tx_ref) and keep a plain lookup index instead. pay-verify
-- now keys duplicate detection on flutterwave_transaction_id when present.

ALTER TABLE public.payment_claims
  DROP CONSTRAINT IF EXISTS payment_claims_tx_ref_key;

CREATE INDEX IF NOT EXISTS idx_payment_claims_tx_ref
  ON public.payment_claims (tx_ref);
