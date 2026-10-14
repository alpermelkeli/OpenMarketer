import type { Metadata } from "next";

import { ModelsScreen } from "@/components/settings/models-screen";

export const metadata: Metadata = { title: "Models" };

export default function ModelsPage() {
  return <ModelsScreen />;
}
