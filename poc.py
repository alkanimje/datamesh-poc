"""Synthetic proof of concept: contract validation, policy-as-code and
cross-domain composition of a Mortgage data product. Fully reproducible."""
import re, json, random, time
import numpy as np, pandas as pd, yaml

C = yaml.safe_load(open("/tmp/poc/contracts.yaml"))

# ---------- synthetic domains ----------
def make_domains(rng, n_cust=20000):
    cust = pd.DataFrame({
        "customer_id": [f"C{i:06d}" for i in range(n_cust)],
        "national_id_hash": [f"h{rng.integers(1e12)}" for _ in range(n_cust)],
        "jurisdiction": rng.choice(["US", "EU", "IN"], n_cust, p=[.6, .25, .15]),
        "credit_tier": rng.choice(list("ABCD"), n_cust)})
    n_loan = int(n_cust * 0.6)
    coll = pd.DataFrame({"collateral_id": [f"P{i:07d}" for i in range(n_loan)],
                         "appraised_value": rng.uniform(5e4, 2e6, n_loan).round(2)})
    loan = pd.DataFrame({
        "loan_id": [f"L{i:07d}" for i in range(n_loan)],
        "customer_id": rng.choice(cust.customer_id, n_loan),
        "principal": rng.uniform(5e4, 1.5e6, n_loan).round(2),
        "rate_pct": rng.uniform(2, 9, n_loan).round(3),
        "collateral_id": coll.collateral_id.values})
    n_pay = n_loan * 6
    pay = pd.DataFrame({
        "payment_id": [f"X{i:08d}" for i in range(n_pay)],
        "loan_id": rng.choice(loan.loan_id, n_pay),
        "amount": rng.uniform(300, 9000, n_pay).round(2),
        "currency": "USD",
        "status": rng.choice(["SETTLED", "LATE", "MISSED"], n_pay, p=[.9, .07, .03])})
    risk = pd.DataFrame({"customer_id": cust.customer_id,
                         "pd_12m": rng.beta(1.2, 30, n_cust).round(5)})
    return {"customer.profile": cust, "lending.loan": loan,
            "payments.event": pay, "risk.score": risk,
            "collateral.valuation": coll}

# ---------- fault injection (ground truth) ----------
FAULTS = ["null_required", "type_drift", "range_violation", "enum_violation",
          "pattern_violation", "duplicate_key", "orphan_fk", "schema_drop"]
TARGET = {"null_required": ("lending.loan", "principal"),
          "type_drift": ("risk.score", "pd_12m"),
          "range_violation": ("lending.loan", "rate_pct"),
          "enum_violation": ("payments.event", "currency"),
          "pattern_violation": ("customer.profile", "customer_id"),
          "duplicate_key": ("collateral.valuation", "collateral_id"),
          "orphan_fk": ("lending.loan", "customer_id"),
          "schema_drop": ("risk.score", "pd_12m")}

def inject(d, fault, rng, frac=0.02):
    prod, col = TARGET[fault]
    df = d[prod].copy(); n = max(1, int(len(df) * frac))
    idx = rng.choice(df.index, n, replace=False)
    if fault == "null_required": df.loc[idx, col] = np.nan
    elif fault == "type_drift": df[col] = df[col].astype(object); df.loc[idx, col] = "n/a"
    elif fault == "range_violation": df.loc[idx, col] = 99.0
    elif fault == "enum_violation": df.loc[idx, col] = "GBP"
    elif fault == "pattern_violation": df.loc[idx, col] = "CX" + df.loc[idx, col].str[1:]
    elif fault == "duplicate_key": df.loc[idx, col] = df[col].iloc[0]
    elif fault == "orphan_fk": df.loc[idx, col] = "C999999"
    elif fault == "schema_drop": df = df.drop(columns=[col])
    d = dict(d); d[prod] = df; return d, prod

# ---------- contract validator ----------
def validate(name, df, d):
    c, v = C[name], []
    for col, s in c["schema"].items():
        if col not in df.columns: v.append(("missing_column", col)); continue
        x = df[col]
        if s.get("required") and x.isna().any(): v.append(("null", col))
        nn = x.dropna()
        if s["type"] == "float":
            if pd.to_numeric(nn, errors="coerce").isna().any(): v.append(("type", col)); continue
            nn = nn.astype(float)
            if "min" in s and (nn < s["min"]).any(): v.append(("range", col))
            if "max" in s and (nn > s["max"]).any(): v.append(("range", col))
        if "allowed" in s and (~nn.isin(s["allowed"])).any(): v.append(("enum", col))
        if "pattern" in s and (~nn.astype(str).str.match(s["pattern"])).any(): v.append(("pattern", col))
        if "fk" in s:
            p, _, k = s["fk"].rpartition(".")
            if (~nn.isin(d[p][k])).any(): v.append(("fk", col))
    for k in c["quality"].get("uniqueness", []):
        if k in df.columns and df[k].duplicated().any(): v.append(("unique", k))
    return v

# ---------- policy-as-code ----------
POLICIES = [  # (rule id, condition on request, decision)
    ("P1-residency-EU", lambda r: r["row_jur"] == "EU" and r["region"] != "EU", "deny"),
    ("P2-pii-purpose", lambda r: r["pii"] and r["purpose"] not in {"kyc", "regulatory"}, "mask"),
    ("P3-confidential-role", lambda r: r["cls"] == "confidential" and r["role"] not in {"analyst", "risk_officer", "auditor"}, "deny"),
]
def decide(req):
    hits = [(pid, dec) for pid, f, dec in POLICIES if f(req)]
    if any(dec == "deny" for _, dec in hits): return "deny", [p for p, _ in hits]
    if hits: return "mask", [p for p, _ in hits]
    return "allow", []

def oracle(req):  # independently written ground truth for the same rules
    if req["row_jur"] == "EU" and req["region"] != "EU": return "deny"
    if req["cls"] == "confidential" and req["role"] not in ("analyst", "risk_officer", "auditor"): return "deny"
    if req["pii"] and req["purpose"] not in ("kyc", "regulatory"): return "mask"
    return "allow"

# ---------- composition: Mortgage product ----------
def caret_ok(ver, spec):
    return spec.startswith("^") and ver.split(".")[0] == spec[1:]

def compose_mortgage(d, versions, naive=False):
    lineage, issues = [], []
    if not naive:
        for prod, c in C.items():
            for dep, spec in c.get("depends_on", {}).items():
                if not caret_ok(versions[dep], spec):
                    issues.append(f"version_incompatible:{prod}->{dep}@{versions[dep]}")
        if issues: return None, issues, lineage
    loan = d["lending.loan"]
    m = loan.merge(d["customer.profile"][["customer_id", "jurisdiction", "credit_tier"]],
                   on="customer_id", how="left" if naive else "inner")
    m = m.merge(d["risk.score"], on="customer_id", how="left")
    m = m.merge(d["collateral.valuation"], on="collateral_id", how="left")
    p = d["payments.event"]
    agg = p.groupby("loan_id").agg(n_pay=("payment_id", "size"),
                                   n_missed=("status", lambda s: (s == "MISSED").sum())).reset_index()
    m = m.merge(agg, on="loan_id", how="left")
    m["ltv"] = m.principal / m.appraised_value
    for s in ["customer.profile", "lending.loan", "risk.score", "collateral.valuation", "payments.event"]:
        lineage.append({"source": s, "version": versions[s]})
    return m, issues, lineage

def propagated_quality(d):  # min completeness of required fields across inputs
    q = {}
    for name, df in d.items():
        req = [c for c, s in C[name]["schema"].items() if s.get("required") and c in df.columns]
        q[name] = float(df[req].notna().all(axis=1).mean()) if req else 1.0
    return min(q.values()), q

# ---------- experiments ----------
def main(seeds=range(10)):
    out = {"fault_detection": [], "policy": [], "composition": [], "timing": []}
    for seed in seeds:
        rng = np.random.default_rng(seed)
        base = make_domains(rng)
        # E0 false positives on clean data
        clean_v = sum(len(validate(n, base[n], base)) for n in base)
        out["fault_detection"].append({"seed": seed, "fault": "none", "detected": clean_v > 0})
        # E1 fault detection: contract vs baseline (schema-only = column presence)
        for f in FAULTS:
            d, prod = inject(base, f, rng)
            t0 = time.perf_counter(); v = validate(prod, d[prod], d); dt = time.perf_counter() - t0
            baseline = set(C[prod]["schema"]) - set(d[prod].columns)
            out["fault_detection"].append({"seed": seed, "fault": f, "detected": bool(v),
                                           "baseline_detected": bool(baseline), "rows": len(d[prod])})
            out["timing"].append({"product": prod, "rows": len(d[prod]), "sec": dt})
        # E2 policy-as-code vs oracle over random requests
        prof = base["customer.profile"]
        for _ in range(2000):
            r = {"row_jur": rng.choice(prof.jurisdiction.values),
                 "region": rng.choice(["US", "EU", "IN"]),
                 "pii": bool(rng.integers(2)), "cls": rng.choice(["internal", "confidential"]),
                 "role": rng.choice(["analyst", "risk_officer", "auditor", "marketing", "intern"]),
                 "purpose": rng.choice(["kyc", "regulatory", "marketing", "analytics"])}
            dec, rules = decide(r)
            out["policy"].append({"seed": seed, "match": dec == oracle(r), "dec": dec,
                                  "traced": dec == "allow" or bool(rules)})
        # E3 composition: orphan customers + incompatible upstream version
        vers = {k: C[k]["version"] for k in C}
        d, _ = inject(base, "orphan_fk", rng)
        mesh, _, lin = compose_mortgage(d, vers)
        naive, _, _ = compose_mortgage(d, vers, naive=True)
        bad = dict(vers); bad["customer.profile"] = "3.0.0"
        blocked, iss, _ = compose_mortgage(base, bad)
        naive_bad, _, _ = compose_mortgage(base, bad, naive=True)
        qmin, _ = propagated_quality(d)
        out["composition"].append({
            "seed": seed,
            "naive_rows_missing_identity": int(naive.jurisdiction.isna().sum()),
            "mesh_rows_missing_identity": int(mesh.jurisdiction.isna().sum()),
            "version_conflict_blocked": blocked is None and len(iss) > 0,
            "naive_version_conflict_blocked": naive_bad is None,
            "lineage_complete": len(lin) == 5 and all(l["version"] for l in lin),
            "propagated_completeness": qmin})
    return out

if __name__ == "__main__":
    o = main()
    fd = pd.DataFrame(o["fault_detection"]); faults = fd[fd.fault != "none"]
    pol = pd.DataFrame(o["policy"]); comp = pd.DataFrame(o["composition"]); tm = pd.DataFrame(o["timing"])
    res = {
        "seeds": 10,
        "clean_false_positive_rate": float(fd[fd.fault == "none"].detected.mean()),
        "contract_detection_rate": float(faults.detected.mean()),
        "baseline_detection_rate": float(faults.baseline_detected.mean()),
        "per_fault_contract": faults.groupby("fault").detected.mean().to_dict(),
        "per_fault_baseline": faults.groupby("fault").baseline_detected.mean().to_dict(),
        "policy_requests": len(pol),
        "policy_agreement": float(pol.match.mean()),
        "policy_decision_mix": pol.dec.value_counts(normalize=True).round(3).to_dict(),
        "policy_traceability": float(pol.traced.mean()),
        "naive_missing_identity_rows_mean": float(comp.naive_rows_missing_identity.mean()),
        "mesh_missing_identity_rows_mean": float(comp.mesh_rows_missing_identity.mean()),
        "version_conflict_blocked_rate": float(comp.version_conflict_blocked.mean()),
        "naive_version_conflict_blocked_rate": float(comp.naive_version_conflict_blocked.mean()),
        "lineage_complete_rate": float(comp.lineage_complete.mean()),
        "validation_ms_per_100k_rows": float((tm.sec / tm.rows * 1e5 * 1000).median()),
    }
    print(json.dumps(res, indent=2))
    json.dump(res, open("/tmp/poc/results.json", "w"), indent=2)
