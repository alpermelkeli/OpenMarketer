import type { Metadata } from "next";

import { CreativeBudgetScreen } from "@/components/settings/creative-budget-screen";

export const metadata: Metadata = { title: "Creative budget" };

export default function CreativeBudgetPage() {
  return <CreativeBudgetScreen />;
}
