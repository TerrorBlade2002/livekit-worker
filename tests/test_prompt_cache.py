"""Verify the prompt-caching refactor is behavior-preserving and cache-friendly.

The goal of the refactor: a large, byte-identical static prefix (the whole of
config/system_prompt.md) is shared across every call so the OpenAI backend can
prompt-cache it, while per-call values are confined to a compact appended
``## CALL CONTEXT`` suffix.

These checks are an offline proxy for the live
``livekit_llm_prompt_cached_tokens_total`` metric: if the static prefix is
identical across two different calls, the provider cache prefix stays warm and
cached-token counts climb on consecutive calls. We assert:

  1. The static prefix is byte-identical across two different prompt_vars sets.
  2. No per-call value leaks into the static prefix.
  3. The suffix carries this call's real values, mapped to their tokens.
  4. The scripted ``{token}`` placeholders survive verbatim in the body.
  5. The tuned closing scripts / disposition statuses are unchanged.

Run directly (no pytest needed):  python tests/test_prompt_cache.py
Or under pytest:                   pytest tests/test_prompt_cache.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402

STATIC_BODY = agent.load_prompt("system_prompt.md")

# Two distinct calls. Values are chosen NOT to collide with any literal text in
# the prompt body (e.g. the body hard-codes "David Patel" as an example), so the
# "value must not leak into the static prefix" assertions are meaningful.
CALL_A = {
    "full_name": "Priya Sundaram",
    "company_name": "Northgate Recovery Partners",
    "company_address": "412 Marlowe Avenue, Suite 9, Wilmington, DE 19801",
    "call_back_number": "844-883-2027",
    "current_time": "03:15 PM EST, Tuesday June 24, 2026",
}
CALL_B = {
    "full_name": "Marcus Bellweather",
    "company_name": "Crestline Servicing Group",
    "company_address": "77 Beacon Court, Tower B, Phoenix, AZ 85004",
    "call_back_number": "855-201-4419",
    "current_time": "09:48 AM EST, Wednesday June 25, 2026",
}

INSTRUCTIONS_A = STATIC_BODY + agent.build_call_context(CALL_A)
INSTRUCTIONS_B = STATIC_BODY + agent.build_call_context(CALL_B)

# Exact closing scripts that must survive the refactor untouched (semantics are
# heavily tuned — only the variable-injection structure was meant to change).
CLOSING_SCRIPTS = [
    "Please hold for a moment while I transfer you to our representative who can assist you further.",
    "I'll go ahead and update our records so you won't get any more calls from us. Goodbye.",
    "Thank you for your time. Have a nice day!",
    "Thank you for your time. Have a great day!",
    "I'll go ahead and update our records right now so you don't get any more calls from us. Goodbye.",
    "Of course — please hold for a moment while I connect you to an agent to assist you further.",
    "Sorry, but as we are not able to proceed in our conversation, I have to end the call here. Thank you for your time.",
    "Oh, I'm so sorry to hear that. Sorry for bothering you. Take care. Bye-bye.",
    "Thank you for letting me know. We'll make a note and handle this on our end. Take care. Goodbye.",
]

DISPOSITION_STATUSES = [
    "verified", "wrong_number", "third_party_end",
    "consumer_busy_end", "dnc", "customer_wants_human", "other",
]


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def test_static_prefix_is_byte_identical_across_calls():
    # Both rendered prompts must begin with the exact same static body.
    assert INSTRUCTIONS_A.startswith(STATIC_BODY)
    assert INSTRUCTIONS_B.startswith(STATIC_BODY)
    # The first byte that differs between the two calls must be at/after the
    # end of the static body — i.e. the entire body is shared, only the suffix
    # diverges. This is exactly what the provider prompt cache keys on.
    cp = _common_prefix_len(INSTRUCTIONS_A, INSTRUCTIONS_B)
    assert cp >= len(STATIC_BODY), (
        f"shared prefix {cp} < static body {len(STATIC_BODY)} — a per-call value "
        "leaked into the cacheable region"
    )


def test_no_per_call_value_leaks_into_static_prefix():
    for call in (CALL_A, CALL_B):
        for key, value in call.items():
            assert value not in STATIC_BODY, (
                f"per-call {key}={value!r} appears in the static body; it must "
                "live only in the CALL CONTEXT suffix"
            )


def test_suffix_carries_this_calls_values():
    suffix_a = agent.build_call_context(CALL_A)
    assert "## CALL CONTEXT" in suffix_a
    for key, value in CALL_A.items():
        assert f"`{{{key}}}` = {value}" in suffix_a, f"missing mapping for {key}"
    # Every documented token gets a row.
    for key in agent.CALL_CONTEXT_KEYS:
        assert f"`{{{key}}}`" in suffix_a


def test_scripted_placeholder_tokens_survive_in_body():
    # The scripted lines keep their literal {token}s (filled by the model from
    # CALL CONTEXT), so wording/semantics are unchanged.
    for token in agent.CALL_CONTEXT_KEYS:
        assert "{" + token + "}" in STATIC_BODY, f"{token} placeholder vanished"
    # Spot-check a couple of the tuned scripted lines verbatim.
    assert "is this {full_name}?" in STATIC_BODY
    assert "Our main office is located at {company_address}." in STATIC_BODY
    assert "It's {call_back_number}." in STATIC_BODY


def test_closing_scripts_and_dispositions_unchanged():
    for script in CLOSING_SCRIPTS:
        assert script in STATIC_BODY, f"closing script changed/missing: {script!r}"
    for status in DISPOSITION_STATUSES:
        assert f"`{status}`" in STATIC_BODY, f"disposition status missing: {status}"
    # The mandatory disclosure phrase rule must remain.
    assert "personal business matter" in STATIC_BODY


def _report():
    """Print a human-readable cache-share summary (rough token estimate)."""
    body_chars = len(STATIC_BODY)
    suffix_chars = len(agent.build_call_context(CALL_A))
    total = body_chars + suffix_chars
    # ~4 chars/token is the usual English rule of thumb.
    body_tok = body_chars // 4
    suffix_tok = suffix_chars // 4
    first_token_pos = STATIC_BODY.find("{full_name}")
    print("\n=== Prompt-cache structure report ===")
    print(f"static body : {body_chars:>6} chars  (~{body_tok} tok)  <- cacheable prefix")
    print(f"call suffix : {suffix_chars:>6} chars  (~{suffix_tok} tok)  <- varies per call")
    print(f"cacheable prefix share : {body_chars / total:6.1%} of the prompt")
    print(
        f"first literal {{full_name}} token at char {first_token_pos} of {body_chars} "
        "(stays a placeholder now, so it no longer breaks the cache)"
    )
    print("Live check: livekit_llm_prompt_cached_tokens_total should approach "
          "livekit_llm_input_tokens_total across consecutive calls.")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    _report()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
