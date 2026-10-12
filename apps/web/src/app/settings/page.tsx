import { redirect } from "next/navigation";

import { FIRST_SECTION } from "@/lib/settings/sections";

/** The settings open on their first section. */
export default function SettingsPage() {
  redirect(FIRST_SECTION.href);
}
