-- Output truncation and reasoning (M0.6.1): why each model call stopped, and how many of
-- its output tokens the provider reports as reasoning. A gateway that forces thinking on
-- can spend the whole max_tokens budget on reasoning and return empty content with
-- finish_reason "length". See docs/04-DATA-MODEL.md and docs/06-EVALUATION.md.

ALTER TABLE steps ADD COLUMN finish_reason    TEXT;      -- choices[0].finish_reason as sent
ALTER TABLE steps ADD COLUMN reasoning_tokens INTEGER;   -- usage.completion_tokens_details.reasoning_tokens
