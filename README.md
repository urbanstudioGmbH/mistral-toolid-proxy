# mistral-toolid-proxy

A tiny, transparent reverse proxy that makes OpenAI-shaped clients work with
Mistral models by normalizing tool-call IDs at the contract boundary.

If you've ever pointed GitHub Copilot, Zed, opencode, or any other
OpenAI-compatible client at a Mistral endpoint and watched tool calls
**intermittently** fail with:

```
Tool call id was call_abc123xyz... but must be a-z, A-Z, 0-9, with a length of 9.
```

this fixes it, without patching your client, your inference engine, or Mistral.

---

## The problem

Mistral requires every tool-call ID to match `^[a-zA-Z0-9]{9}$` — exactly nine
alphanumeric characters. No underscores, no dashes, no `call_` prefix, no other
length.

Almost every other provider is permissive, so OpenAI-shaped clients happily mint
IDs like `call_PTLP8xhu3uwZk4l3nlnrrJha`, `toolu_01VpEm…`, or timestamp strings.
On a single turn that's invisible. On a multi-turn tool conversation, those IDs
get echoed back in the message history and Mistral's validator (`mistral-common`)
rejects them with a 400.

It often *looks* like it works, because some serving stacks already truncate the
ID to nine characters — but truncation fixes the **length**, not the **character
set**. The moment an underscore survives inside the kept window, it fails again.
Hence the maddening "works most of the time" behavior.

## What it does

The proxy sits in front of one OpenAI-compatible Mistral endpoint and rewrites
every `tool_call_id` and `tool_calls[].id` on inbound `POST /v1/chat/completions`
requests into a compliant nine-character form. Everything else — other routes,
streaming responses, auth headers — passes straight through. To the client it
looks exactly like the real endpoint; the IDs just come out clean.

## Quickstart

**pip**

```bash
pip install -r requirements.txt
UPSTREAM=http://127.0.0.1:8001 PORT=8081 python mistral_toolid_proxy.py
```

**Docker**

```bash
docker build -t mistral-toolid-proxy .
docker run --rm -p 8081:8081 -e UPSTREAM=http://host.docker.internal:8001 mistral-toolid-proxy
# On plain Linux, add: --add-host=host.docker.internal:host-gateway
```

**systemd** — see the install header in `mistral-toolid-proxy.service`.

Then point your client's base URL at `http://<host>:8081/v1` instead of the model.

## Configuration

| Variable   | Default                 | Purpose                                                                    |
|------------|-------------------------|----------------------------------------------------------------------------|
| `UPSTREAM` | `http://127.0.0.1:8001` | The Mistral endpoint to front — local vLLM or a public, keyed API.         |
| `HOST`     | `0.0.0.0`               | Listen address.                                                            |
| `PORT`     | `8081`                  | Listen port.                                                               |
| `API_KEY`  | _(unset)_               | If set, injected as `Authorization: Bearer …` upstream so the client can stay keyless. Unset = the client's own auth passes through. |

---

## Why a proxy, and not one of the other fixes?

There were several legitimate places to solve this. They are not equally good.
The short version: the more *elegant* layers reach the fewest people, and reach
is the entire problem here.

First, a ground rule that rules out the laziest option. **The nine-character
constraint is not arbitrary strictness.** The ID is rendered into the prompt the
model was trained on, and `mistral-common` validates it to keep inputs canonical.
"Just loosen the validator" trades a clean, loud 400 for silent quality drift.
So every approach below is about making IDs *comply* — never about weakening the
rule.

**Upstream, in `mistral-common`.** This is arguably the *right long-term home*:
normalization is explicitly part of the library's job, alongside validation. A
blessed, opt-in `normalize_tool_call_id()` would let everyone coerce IDs the same
way instead of reinventing it. Worth pursuing. But it doesn't help today — it has
to ship as opt-in (not lenient validation), every consumer then has to adopt it,
tokenizer releases are deliberately conservative for backward-compat reasons, and
it does precisely nothing for the largest affected group: people hitting the
*hosted* Mistral API, who never run `mistral-common` on their own machine.

**In the inference engine (e.g. vLLM).** vLLM already half-solves this by
truncating incoming IDs to nine chars — but, as above, truncation fixes length,
not character set, leaving the underscore failure intact. Extending it to coerce
the charset is a clean, correct patch, and the gap is genuinely unfilled. But it
only helps people who self-host on that specific engine and can patch or rebuild
it; it couples to engine internals; it ships on the engine's release cadence; and
it does nothing for other engines or the hosted API.

**In the clients.** This is what the ecosystem actually did — repeatedly. The
Vercel AI SDK, Zed, opencode, Roo Code and others each wrote their own
`normalizeMistralToolCallId`. It works, for that one client. The result is a dozen
slightly-different reimplementations of the same ten lines, each a separate
maintenance target, each requiring the user to upgrade. And it structurally
excludes anyone who *can't* change their client — a closed agent, a locked-down
environment, or a tool whose maintainer simply hasn't gotten to it.

**In a gateway (LiteLLM, OpenRouter).** A shared gateway would be a reasonable
home, but the popular ones don't do it: LiteLLM has an open request for exactly
this and already does the equivalent for Anthropic but not Mistral; OpenRouter
forwards the malformed ID untouched and lets Mistral reject it. Adopting a full
multi-provider gateway just to fix one field is a heavy answer to a small problem.

**So: a standalone proxy.** It's the only option that:

- works regardless of **client** (Copilot, Zed, curl, anything OpenAI-shaped) and
  regardless of **engine** (local vLLM, a hosted API, anything that speaks the
  protocol);
- helps people who can touch **neither** their client nor their serving stack —
  they run one small thing and repoint a URL;
- **respects the contract** instead of asking anyone to weaken it; it makes
  non-compliant clients comply, full stop;
- is **one focused thing to maintain** instead of the same fix smeared across a
  dozen codebases.

It is not the most elegant layer. It is the most *reachable* one — and reach is
what every other approach gives up.

## Design decisions

**Route by topology, not by inspection.** One instance fronts exactly one Mistral
endpoint. The proxy never reads the model name to decide whether to act, so there
is no string-matching gamble (nobody's `gemma-mistral-merge` accidentally gets
rewritten) and zero overhead on unrelated workloads — your TTS, ASR, and other
models simply don't route through it. Wiring, not guessing.

**Rewrite requests only.** The upstream already emits compliant IDs, so responses
stream straight back untouched and Server-Sent Events stay intact. Only inbound
`tool_call_id` and `tool_calls[].id` fields are touched.

**Deterministic, not clever.** Each ID is mapped through `blake2s → base62`,
truncated to nine characters.

- *Why a hash, not a character swap?* A naive substitution (say `_` → `Z`) is only
  safe if the source never contains the replacement character. Under near-identical
  adversarial IDs it can collide and cross-wire a tool result to the wrong call.
- *Why base62, not base32?* base62 uses the full allowed alphabet (~2⁵³ of space)
  rather than base32's ~2⁴⁵. The extra bits are free; take them.
- *The honest part:* any nine-character target is non-injective by pigeonhole. You
  **cannot** guarantee uniqueness — you can only make collisions negligible at
  conversation scale, which base62 does by a wide margin. The nine-char ceiling is
  the prison, not the encoding.
- Because the transform is deterministic, the assistant's `tool_calls[].id` and the
  matching `tool_call_id` (the same source string) map to the same output and stay
  paired — with no per-request state.

**Normalize unconditionally.** There is no "is this already valid?" branch.
Re-hashing an already-compliant ID is harmless because pairing is preserved, and
dropping the check removes a whole class of edge-case bugs.

## Limitations and the real fix

- One upstream per instance. This is a normalizer, not a router or load balancer.
- The nine-character ceiling is Mistral's. The durable fix is Mistral relaxing the
  format, or `mistral-common` shipping a canonical opt-in normalizer. Until then,
  this lives at the boundary as an adapter.
- Collisions are astronomically unlikely but not mathematically impossible. If you
  somehow run a single conversation with millions of distinct tool calls, widen the
  digest.

## Prior art

The underlying constraint is one of the most-reported Mistral integration
papercuts — it has surfaced across client SDKs, editors, gateways, and engines.
A representative sample (verify exact links before relying on them):

- Vercel AI SDK — `vercel/ai` #11802 (Bedrock-format IDs)
- opencode — `sst/opencode` #1680 (the `call_` prefix)
- Zed — `zed-industries/zed` #53034
- Roo Code — `RooCodeInc/Roo-Code` #10102 (per-provider normalizer)
- LiteLLM — `BerriAI/litellm` #22317 (open; Anthropic done, Mistral not)
- vLLM — `vllm-project/vllm` #9019 (engine-side ID generation)

Every one of these was solved in place, for one tool. This repo is the same fix,
in a place anyone can reuse.

## License

Apache-2.0 suggested (drop your `LICENSE` of choice in the repo root).
