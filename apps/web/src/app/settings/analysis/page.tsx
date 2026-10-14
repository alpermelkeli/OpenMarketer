import type { Metadata } from "next";

import { AnalysisScreen } from "@/components/settings/analysis-screen";

export const metadata: Metadata = { title: "Analysis" };

export default function AnalysisPage() {
  return <AnalysisScreen />;
}
