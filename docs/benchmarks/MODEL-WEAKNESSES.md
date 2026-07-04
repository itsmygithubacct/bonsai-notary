# Bonsai-8B — systematic model weaknesses (benchmark findings)

Observed while building + grading the elementary benchmarks (`benchmarks/kindergarten_v1.json` and the
grade 1–4 / final sets). The model is **Bonsai-8B Q1_0** (1-bit packed Qwen3) run on the deterministic
integer reference engine — **greedy, seed 0**, so every answer here is **byte-exactly reproducible**: these
are stable model behaviours, not sampling noise. Data: `benchmarks/results/*.jsonl` and the per-question
perf/answer log `~/.local/trinote/debug/bonsai-debug.jsonl`.

These are recorded as **fine-tuning / evaluation targets** and as **prompt-authoring guidance** (some
"misses" are really question flaws the model exposes — see §6).

---

## 0. The meta-pattern: confident confabulation

The single most important weakness is **fluent, authoritative justification of a wrong answer**. The model's
`thinking` is coherent, well-structured, and *factually wrong* — it does not hedge or signal uncertainty. This
makes its errors high-confidence and self-consistent (the wrong answer is "supported" by an invented rule).
Every category below is an instance of this. Implication: the model cannot be trusted to self-report
uncertainty; external grading against ground truth is required.

---

## 1. Animal sounds → substitutes an associated noun for the onomatopoeia

The model answers animal-**sound** questions with a **noun associated with the animal** (a body part or
object), not the sound, and rationalizes it confidently. Failed at least twice, identically:

| Question | Expected | Model answer | Model's reasoning (verbatim) |
|---|---|---|---|
| What sound does a cow make? | moo | **bell** | "The most common and recognizable sound is the **cowbell**, which is a bell attached to the cow" |
| What sound does a pig make? | oink | **snout** | "The most common and recognizable sound a pig makes is **'snout.'** This word … directly refers to the sound a pig produces" |

It maps "cow"→cowbell→bell and "pig"→snout, treating a salient associated noun as the sound. **Onomatopoeia
is a genuine gap.** (Both questions were ultimately replaced in the benchmark, per the review log.)

---

## 2. Elementary geometry property confusion (square / rectangle / rhombus / octagon)

The model conflates quadrilateral definitions and basic shape facts:

| Question | Expected | Model answer | Model's reasoning (verbatim) |
|---|---|---|---|
| What shape has four equal sides **and four right angles**? | square | **rectangle** | "A rectangle has **four sides of equal length** and four right angles … a fundamental property of rectangles" (FALSE — a rectangle's sides are not all equal) |
| What shape has four equal sides? | square/rhombus | rhombus | (actually *correct* — a rhombus has four equal sides) |
| What shape is a stop sign? | octagon | **circle** | "a red **circle** with a white border and a white triangle in the center" (confabulated description) |

The model asserts a **false defining property** ("a rectangle has four equal sides") and **hallucinates a
concrete description** (stop sign = circle + triangle). Adding the disambiguator "and four right angles"
*worsened* the square answer (it pattern-matched "right angles" → rectangle). Elementary geometry facts and
definitions are unreliable.

---

## 3. Subtractive color mixing

| Question | Expected | Model answer | Model's reasoning (verbatim) |
|---|---|---|---|
| What color do you get mixing blue and yellow? | green | **white** | "Blue and yellow are **complementary colors**, meaning they are opposite each other on the color wheel" (FALSE — they are not complementary; mixing pigments gives green) |

The model invents a color-theory rule and applies it wrongly. Color-mixing / color-theory facts are
unreliable.

---

## 4. Morphology: right lemma, wrong inflection

| Question | Expected | Model answer | Model's reasoning (verbatim) |
|---|---|---|---|
| What is a baby/young cow called? | calf | **calve** | "The term for a young cow is typically **'calve'**, which is a single calf" |

The model knows the concept (it even writes "which is a single calf") but emits the **verb form** "calve"
instead of the noun "calf" — a persistent inflection error that **survived rephrasing**. Suggests
weak control over word-form/number under the one-word constraint. (Grading on the strict `answer` field
correctly fails this; a lenient grader would mask it.)

---

## 5. Anthropomorphic literalism ("you" → a disembodied AI)

When a question says "**you**", the model often reasons as *itself* — a bodiless AI — rather than as a generic
person, producing literally-true-for-an-AI but unintended answers:

| Question | Expected | Model answer | Model's reasoning (verbatim) |
|---|---|---|---|
| How many eyes do you have? | 2 | **zero** | "Since I am an AI assistant and do not have physical form, I do not possess eyes" |
| What do you use to hear? | ears | **Text input** | "Since I am an AI assistant, I do not have physical senses or the ability to hear" |
| What do you use to smell? | nose | **olfactory** | "The sense of smell is typically referred to as 'olfactory'" |

This is **partly a prompt flaw, not a knowledge gap** — the same facts are answered correctly when phrased
non-anthropomorphically ("How many eyes does a **person** have?" → "two"; "What body part do **people** use to
hear?" → "ear"). See §6.

---

## 6. Format behaviour (not a knowledge gap, but it affects grading)

- **Verbose thinking-first → JSON truncation.** Under the `{"thinking", "answer"}` contract the model writes a
  long `thinking` first; at a tight `max_new` the JSON is cut off **before** the `answer` field closes. At
  `max_new=96`, **46/100** kindergarten answers had no parseable `answer` field. Mitigations (all applied):
  single-word answer directive, a larger budget (`max_new 160`), per-category caps floored above where real
  answers finish (`benchmarks/set_maxnew_caps.py`), and **strict grading on the JSON `answer` field only** (so
  a question echo in `thinking` cannot leak a false pass).
- **Confident, non-hedged tone** even when wrong (see §0) — the model does not say "I'm not sure".

---

## 7. Implications

**For prompt / benchmark authoring**
- Avoid anthropomorphic "you" for factual questions about people/bodies — phrase as "a person" / "people"
  (§5). The same model passes the de-anthropomorphized form.
- Avoid over-disambiguation that introduces a misleading keyword ("and four right angles" pulled the model to
  "rectangle"; §2).
- Keep answers single-word and the budget above the answer-field completion point (§6).
- Grade the **JSON `answer` field**, never the raw text (echo leakage).

**For model improvement (fine-tuning targets)**
- Onomatopoeia / animal sounds (§1).
- Elementary geometry definitions and shape facts (§2).
- Subtractive color mixing / color theory (§3).
- Word-form/inflection control under length constraints (§4).
- Calibrated uncertainty — the model should signal low confidence instead of confabulating (§0).

**For grading**
- Strict field grading separates genuine misses from format/echo artifacts. After mitigations, the
  kindergarten set scored **92/100 under the updated framework**, with the residual failures being the genuine
  weaknesses catalogued here.

---

## 8. Reproducing / extending

```bash
# re-run a benchmark (greedy, seed 0 → byte-exact; results + grade report written under benchmarks/results/)
PYTHONPATH=src .venv/bin/python tools/bonsai_kindergarten_bench.py --benchmark benchmarks/kindergarten_v1.json
# strict re-grade an existing run from its stored raw outputs
PYTHONPATH=src .venv/bin/python tools/bonsai_regrade.py --results benchmarks/results/<run>.jsonl
# collect still-failed questions for targeted retry
PYTHONPATH=src .venv/bin/python tools/bonsai_make_retry.py --results <regraded>.jsonl \
    --source benchmarks/kindergarten_v1.json --out benchmarks/kindergarten_retry.json
```
Per-question timing + answers + reasoning accumulate in `~/.local/trinote/debug/bonsai-debug.jsonl` for
model-improvement analysis.
