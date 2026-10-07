-- Prompt overhead (M0.3): which provider served each model call, and how many input tokens
-- it billed beyond our own estimate of the prompt we sent. Gateways can add hidden context,
-- and the spend guard reads the recent maximum per provider and model from here.
-- See docs/04-DATA-MODEL.md and docs/05-ROUTING-AND-COST.md.

ALTER TABLE steps ADD COLUMN provider               TEXT;      -- provider name from config
ALTER TABLE steps ADD COLUMN prompt_overhead_tokens INTEGER;   -- input_tokens - ceil(chars / 4), floored at 0

CREATE INDEX idx_steps_provider ON steps(provider, created_at);
