"""Build the analysis dataset behind the healthcare dashboard.

Reads the eight source CSVs, joins the star schema, derives the financial
measures, and writes a single JSON consumed by dashboard.html.

Usage:
    python analysis.py --data-dir "Healthcare Provide Dataset" --out data.json
"""

import argparse
import json
from pathlib import Path

import pandas as pd


def load(data_dir: Path) -> dict:
    """Load every source table."""
    names = ["visits", "patients", "providers", "departments",
             "diagnoses", "procedures", "insurance", "cities"]
    return {n: pd.read_csv(data_dir / f"{n}.csv") for n in names}


def derive_measures(visits: pd.DataFrame) -> pd.DataFrame:
    """Add the derived financial columns the dashboard reports on.

    The source gives a daily room rate but no room total, and no gross
    revenue at all, so both are calculated here.
    """
    v = visits.copy()

    v["visit_date"] = pd.to_datetime(v["Date of Visit"], format="%m/%d/%Y")
    v["admitted"] = pd.to_datetime(v["Admitted Date"], format="%m/%d/%Y", errors="coerce")
    v["discharged"] = pd.to_datetime(v["Discharge Date"], format="%m/%d/%Y", errors="coerce")

    # Length of stay is only defined where both dates exist. Visits without
    # an admission contribute no room revenue rather than being dropped.
    v["length_of_stay"] = (v["discharged"] - v["admitted"]).dt.days
    los_filled = v["length_of_stay"].fillna(0)

    v["room_revenue"] = v["Room Charges(daily rate)"] * los_filled
    v["gross_revenue"] = v["Treatment Cost"] + v["Medication Cost"] + v["room_revenue"]

    # 119 visits have no coverage recorded; treating as zero slightly
    # overstates patient liability, which is documented in the dashboard.
    v["covered"] = v["Insurance Coverage"].fillna(0)
    v["patient_liability"] = (v["gross_revenue"] - v["covered"]).clip(lower=0)

    v["is_pending"] = v["Payment Status"] == "Pending"
    v["month"] = v["visit_date"].dt.to_period("M")

    return v


def breakdown(df: pd.DataFrame, column: str) -> list:
    """Revenue and volume grouped by one dimension, ranked by revenue."""
    g = (df.groupby(column)
           .agg(revenue=("gross_revenue", "sum"), visits=("Patient ID", "count"))
           .reset_index()
           .sort_values("revenue", ascending=False))
    return [{"name": str(r[column]), "revenue": float(r["revenue"]),
             "visits": int(r["visits"])} for _, r in g.iterrows()]


def data_quality_checks(v: pd.DataFrame, months) -> dict:
    """Quantify the inconsistencies found in the source data.

    These are reported in the dashboard rather than silently corrected,
    because the right fix depends on which field is authoritative — a
    question for the data owner, not the analyst.
    """
    return {
        # Clinical categories appear to be populated independently
        "room_on_outpatient": int(((v["Service Type"] == "Outpatient")
                                   & v["Room Type"].notna()).sum()),
        "inpatient_no_admit": int(((v["Service Type"] == "Inpatient")
                                   & v["admitted"].isna()).sum()),
        "emergency_flag_mismatch": int(((v["Service Type"] == "Emergency")
                                        & (v["Emergency Visit"] == "No")).sum()),
        # Calendar gaps
        "missing_months": [str(m) for m in months if m not in v["month"].unique()],
        "oct_visits": int((v["month"] == pd.Period("2024-10")).sum()),
        "jan_visits": int((v["month"] == pd.Period("2025-01")).sum()),
        # Value-level issues
        "coverage_missing": int(v["Insurance Coverage"].isna().sum()),
        "coverage_exceeds": int((v["covered"] > v["gross_revenue"]).sum()),
        "low_sat_share": float((v["Patient Satisfaction Score"] <= 2).mean() * 100),
    }


def build(data_dir: Path) -> dict:
    t = load(data_dir)
    v = derive_measures(t["visits"])

    out = {}

    out["kpi"] = {
        "visits": int(len(v)),
        "revenue": float(v.gross_revenue.sum()),
        "avg_visit": float(v.gross_revenue.mean()),
        "covered": float(v.covered.sum()),
        "covered_pct": float(v.covered.sum() / v.gross_revenue.sum() * 100),
        "patient_liability": float(v.patient_liability.sum()),
        "patients": int(v["Patient ID"].nunique()),
        "satisfaction": float(v["Patient Satisfaction Score"].mean()),
        "outstanding": float(v.loc[v.is_pending, "gross_revenue"].sum()),
        "outstanding_pct": float(v.is_pending.mean() * 100),
        "avg_los": float(v.loc[v.length_of_stay > 0, "length_of_stay"].mean()),
        "date_min": v.visit_date.min().strftime("%d %b %Y"),
        "date_max": v.visit_date.max().strftime("%d %b %Y"),
    }

    # Monthly series, reindexed so a missing month shows as a gap rather
    # than silently closing up and distorting the trend line.
    months = pd.period_range(v["month"].min(), v["month"].max(), freq="M")
    m = (v.groupby("month")
           .apply(lambda g: pd.Series({
               "visits": len(g),
               "revenue": g.gross_revenue.sum(),
               "outstanding": g.loc[g.is_pending, "gross_revenue"].sum(),
           }), include_groups=False)
           .reindex(months, fill_value=0)
           .reset_index(names="month"))
    out["monthly"] = [{"label": str(r["month"]), "visits": int(r["visits"]),
                       "revenue": float(r["revenue"]),
                       "outstanding": float(r["outstanding"])} for _, r in m.iterrows()]

    # Departments
    vd = v.merge(t["departments"], on="Department ID")
    g = (vd.groupby("Department")
           .agg(revenue=("gross_revenue", "sum"), visits=("Patient ID", "count"),
                avg=("gross_revenue", "mean"),
                sat=("Patient Satisfaction Score", "mean"),
                pend=("is_pending", "mean"))
           .reset_index().sort_values("revenue", ascending=False))
    out["departments"] = [{"name": r["Department"], "revenue": float(r["revenue"]),
                           "visits": int(r["visits"]), "avg": float(r["avg"]),
                           "sat": float(r["sat"]), "pending": float(r["pend"] * 100)}
                          for _, r in g.iterrows()]

    # Providers
    vp = v.merge(t["providers"], on="Provider ID")
    gp = (vp.groupby(["Provider Name", "Nationality", "Gender", "Age", "Image"])
            .agg(visits=("Patient ID", "count"), revenue=("gross_revenue", "sum"),
                 avg=("gross_revenue", "mean"),
                 sat=("Patient Satisfaction Score", "mean"),
                 los=("length_of_stay", "mean"), pend=("is_pending", "mean"))
            .reset_index().sort_values("revenue", ascending=False))
    out["providers"] = [{"name": r["Provider Name"], "nationality": r["Nationality"],
                         "gender": r["Gender"], "age": int(r["Age"]), "image": r["Image"],
                         "visits": int(r["visits"]), "revenue": float(r["revenue"]),
                         "avg": float(r["avg"]), "sat": float(r["sat"]),
                         "los": float(r["los"]), "pending": float(r["pend"] * 100)}
                        for _, r in gp.iterrows()]

    # Dimensional breakdowns
    out["service_type"] = breakdown(v, "Service Type")
    out["referral"] = breakdown(v, "Referral Source")
    out["payer"] = breakdown(v.merge(t["insurance"], on="Insurance ID"), "Insurance Provider")
    out["diagnosis"] = breakdown(v.merge(t["diagnoses"], on="Diagnosis ID"), "Diagnosis")
    out["procedure"] = breakdown(v.merge(t["procedures"], on="Procedure ID"), "Procedure")

    vc = v.merge(t["patients"], on="Patient ID").merge(t["cities"], on="City ID")
    out["cities"] = breakdown(vc, "City")[:10]

    out["composition"] = [
        {"name": "Treatment", "value": float(v["Treatment Cost"].sum())},
        {"name": "Room charges", "value": float(v["room_revenue"].sum())},
        {"name": "Medication", "value": float(v["Medication Cost"].sum())},
    ]

    sd = v["Patient Satisfaction Score"].value_counts().sort_index()
    out["satisfaction_dist"] = [{"score": int(k), "count": int(c)} for k, c in sd.items()]

    out["quality"] = data_quality_checks(v, months)

    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="Healthcare Provide Dataset")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    result = build(Path(args.data_dir))

    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)

    k = result["kpi"]
    print(f"Wrote {args.out}")
    print(f"  {k['visits']:,} visits · ${k['revenue']:,.0f} gross revenue")
    print(f"  {k['outstanding_pct']:.1f}% unpaid · {k['covered_pct']:.1f}% insurance covered")
    print(f"  {len(result['quality']['missing_months'])} missing month(s) detected")
