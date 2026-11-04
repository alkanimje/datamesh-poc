# Data Mesh Proof of Concept: Contracts, Policy-as-Code, and Composite Products

This repository holds a small, reproducible prototype that accompanies the article *"Making Data Mesh Work in a Regulated Bank: Contracts, Composition, and Code-Enforced Governance."*

It uses **synthetic data only** to test whether three mechanisms behave as designed:

1. **Data contracts.** These catch broken data before it reaches consumers.
2. **Policy-as-code.** It makes access decisions (allow, mask, or deny) that are consistent and traceable to a named rule.
3. **Composite data products.** A mortgage product built from five domains blocks incompatible upstream versions and quarantines rows whose identity can't be resolved, instead of passing them through.

> **Note:** All data is synthetic, and the access policies are illustrative examples. They are not a legal interpretation of GDPR, GLBA, or BCBS 239. The results show that the mechanisms work as specified. They do not show improvements in a real institution.

## Files

| File | Purpose |
|---|---|
| `poc.py` | Generates the synthetic data, runs all three experiments, and prints the results |
| `contracts.yaml` | Data contracts for the five domain products (Customer, Lending, Payments, Risk, Collateral) |
| `results.json` | Results from a reference run (10 random seeds) |

## Requirements

- Python 3.9 or later
- `pandas`, `numpy`, `pyyaml`

```bash
pip install pandas numpy pyyaml
```

## How to run

1. From the repository folder, run:
   ```bash
   python poc.py
   ```
2. The results print to the screen and are saved to `results.json`. A full run takes a few minutes.

## What the experiments do

- **E1, fault detection:** injects 8 kinds of faults into 2% of rows and compares contract validation with a "does the column exist?" baseline. The faults are nulls, wrong types, out-of-range values, invalid codes, malformed keys, duplicate keys, dangling foreign keys, and a dropped column.
- **E2, policy enforcement:** evaluates 2,000 random access requests per seed and checks each decision against an independently written reference implementation.
- **E3, composition:** builds the mortgage product with dangling customer keys and with an incompatible upstream version, and compares the result with a naive left join.

## Reference results (10 seeds)

| Measure | Framework | Baseline |
|---|---|---|
| Fault classes detected | 100% | 12.5% |
| False alarms on clean data | 0% | — |
| Policy decisions matching reference | 100% | — |
| Mortgage rows with unresolved identity | 0 | 240 per run |
| Incompatible version blocked | 100% | 0% |
| Validation cost (per 100k rows) | ~69 ms | — |

Timing will vary by machine. All other numbers should reproduce exactly.

## Author

Alka Nimje
