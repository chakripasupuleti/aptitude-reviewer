# Aptitude Content Reviewer V1

Local deterministic-first reviewer for aptitude questions.

## Input columns

The input CSV/XLSX must contain:

- S. No
- Question
- Option A
- Option B
- Option C
- Option D
- Explanation
- Key

The Key value must be exactly one of:

- Option A
- Option B
- Option C
- Option D

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py input.csv output_reviewed.xlsx
```

or

```bash
python app.py input.xlsx output_reviewed.xlsx
```

## V1 checks

- Required fields
- Strict key format
- Exact duplicate options
- Equivalent duplicate options for fractions, ratios, decimals, percentages, mixed fractions, and basic unit conversions
- LaTeX wrapping issues
- Dollar symbol used as currency
- Basic arithmetic mistakes in explanation
- Final answer vs options/key mismatch when the explanation has a clear final answer
- Strong ranking/direction data mismatch
- Basic grammar/formation issues
- Blood relation gender assumption checks

## Notes

V1 does not use an LLM. Do not add prompts yet. LLM prompts should be added in V2 for ambiguity, question formation, and deeper topic-wise reasoning.
