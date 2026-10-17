/**
 * The creative budget as the settings show it: where each cap comes from, this month's
 * spending in words, and the form that chooses the caps, as it holds what a person typed and
 * as the API takes it. Display only: the bounds of a cap and whether a spend fits are the API's.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { CapSource, CreativeBudget, CreativeBudgetRequest, MonthlySpend } from "@/lib/api/types";

import { numberIn } from "./role-form";

type SourceDisplay = { label: string; tone: Tone };

const SOURCES: Record<CapSource, SourceDisplay> = {
  environment: { label: "Set by the environment", tone: "warning" },
  stored: { label: "Chosen here", tone: "success" },
  default: { label: "The default", tone: "neutral" },
};

export function capSourceDisplay(source: CapSource): SourceDisplay {
  return SOURCES[source];
}

const DOLLARS = new Intl.NumberFormat("en", { style: "currency", currency: "USD" });

/** "$12.40": what the month's spending is shown in. */
export function formatDollars(usd: number): string {
  return DOLLARS.format(usd);
}

export type SpendLine = { label: string; value: string };

/** This month's spending, line by line: what was spent, what is held, what is left of the cap. */
export function spendLines(spend: MonthlySpend): SpendLine[] {
  return [
    { label: "Spent", value: formatDollars(spend.spent_usd) },
    { label: "Held for actions not settled yet", value: formatDollars(spend.reserved_usd) },
    { label: "Left of the monthly cap", value: `${formatDollars(spend.remaining_usd)} of ${formatDollars(spend.cap_usd)}` },
  ];
}

const MONTH = new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" });

/** "October 2026": the calendar month, in UTC, that the spending is counted for. */
export function spendMonth(spend: MonthlySpend): string {
  const start = new Date(spend.month_start);
  return Number.isNaN(start.getTime()) ? spend.month_start : MONTH.format(start);
}

export type CreativeBudgetForm = { monthlyUsd: string; autoPerActionUsd: string };

/** The form filled with what the workspace chose; a cap it did not choose is left empty. */
export function creativeBudgetFormOf(chosen: CreativeBudget["chosen"]): CreativeBudgetForm {
  return {
    monthlyUsd: chosen?.monthly_usd?.toString() ?? "",
    autoPerActionUsd: chosen?.auto_per_action_usd?.toString() ?? "",
  };
}

export type ParsedCreativeBudgetForm =
  | { caps: CreativeBudgetRequest; problems: null }
  | { caps: null; problems: Readonly<Record<string, readonly string[]>> };

const NOT_AN_AMOUNT = "Enter an amount in US dollars, such as 5.00, or leave it empty.";

/**
 * The request for the form, or what in it is not an amount. An empty field keeps the environment's
 * cap or the default; a cap the environment sets is left out, since the API refuses to choose it.
 */
export function creativeBudgetRequestOf(
  form: CreativeBudgetForm,
  budget: Pick<CreativeBudget, "monthly" | "auto_per_action">,
): ParsedCreativeBudgetForm {
  const problems: Record<string, string[]> = {};
  const caps: CreativeBudgetRequest = {};
  if (budget.monthly.source !== "environment") {
    const monthly = amountIn(form.monthlyUsd);
    if (Number.isNaN(monthly)) problems.monthly_usd = [NOT_AN_AMOUNT];
    else caps.monthly_usd = monthly;
  }
  if (budget.auto_per_action.source !== "environment") {
    const perAction = amountIn(form.autoPerActionUsd);
    if (Number.isNaN(perAction)) problems.auto_per_action_usd = [NOT_AN_AMOUNT];
    else caps.auto_per_action_usd = perAction;
  }
  if (Object.keys(problems).length > 0) return { caps: null, problems };
  return { caps, problems: null };
}

function amountIn(text: string): number | null {
  return numberIn(text.trim().replace(/^\$/, ""));
}
