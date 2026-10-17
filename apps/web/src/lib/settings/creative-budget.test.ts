import { describe, expect, it } from "vitest";

import type { CreativeBudget } from "@/lib/api/types";

import {
  capSourceDisplay,
  creativeBudgetFormOf,
  creativeBudgetRequestOf,
  spendLines,
  spendMonth,
} from "./creative-budget";

function cap(source: CreativeBudget["monthly"]["source"]): CreativeBudget["monthly"] {
  return { value_usd: 20, source, default_usd: 20, environment_variable: "CREATIVE_MONTHLY_USD" };
}

const SPEND: CreativeBudget["spent_this_month"] = {
  month_start: "2026-10-01T00:00:00Z",
  spent_usd: 3.2,
  reserved_usd: 0.12,
  cap_usd: 20,
  remaining_usd: 16.68,
};

describe("where a cap comes from", () => {
  it("is said in words, with the environment as a warning", () => {
    expect(capSourceDisplay("environment")).toEqual({ label: "Set by the environment", tone: "warning" });
    expect(capSourceDisplay("stored").label).toBe("Chosen here");
    expect(capSourceDisplay("default").label).toBe("The default");
  });
});

describe("this month's spending", () => {
  it("is what was spent, what is held and what is left of the cap", () => {
    expect(spendLines(SPEND)).toEqual([
      { label: "Spent", value: "$3.20" },
      { label: "Held for actions not settled yet", value: "$0.12" },
      { label: "Left of the monthly cap", value: "$16.68 of $20.00" },
    ]);
  });

  it("is counted for the calendar month in UTC", () => {
    expect(spendMonth(SPEND)).toBe("October 2026");
  });
});

describe("the form of the caps", () => {
  it("is filled with what the workspace chose, and empty for the rest", () => {
    expect(creativeBudgetFormOf({ monthly_usd: 50, auto_per_action_usd: null })).toEqual({
      monthlyUsd: "50",
      autoPerActionUsd: "",
    });
    expect(creativeBudgetFormOf(null)).toEqual({ monthlyUsd: "", autoPerActionUsd: "" });
  });

  it("sends the amounts typed, and null for an empty field", () => {
    const parsed = creativeBudgetRequestOf(
      { monthlyUsd: "$50", autoPerActionUsd: "" },
      { monthly: cap("stored"), auto_per_action: cap("default") },
    );
    expect(parsed).toEqual({ caps: { monthly_usd: 50, auto_per_action_usd: null }, problems: null });
  });

  it("leaves out a cap the environment sets", () => {
    const parsed = creativeBudgetRequestOf(
      { monthlyUsd: "50", autoPerActionUsd: "0.25" },
      { monthly: cap("environment"), auto_per_action: cap("default") },
    );
    expect(parsed.caps).toEqual({ auto_per_action_usd: 0.25 });
  });

  it("says which field is not an amount", () => {
    const parsed = creativeBudgetRequestOf(
      { monthlyUsd: "lots", autoPerActionUsd: "1" },
      { monthly: cap("default"), auto_per_action: cap("default") },
    );
    expect(parsed.caps).toBeNull();
    expect(Object.keys(parsed.problems ?? {})).toEqual(["monthly_usd"]);
  });
});
