import type { Metadata } from "next";

import { CredentialsScreen } from "@/components/settings/credentials-screen";

export const metadata: Metadata = { title: "Credentials" };

export default function CredentialsPage() {
  return <CredentialsScreen />;
}
