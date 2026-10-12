/** The sections of the settings area, in the order its menu lists them. Display only. */

export type SettingsSection = {
  /** Where the section lives, under `/settings`. */
  href: string;
  label: string;
};

export const SETTINGS_SECTIONS: readonly SettingsSection[] = [{ href: "/settings/credentials", label: "Credentials" }];

/** The section `/settings` itself opens. */
export const FIRST_SECTION = SETTINGS_SECTIONS[0];

/** Whether an address is the section's own or one below it. */
export function isSectionOpen(section: SettingsSection, pathname: string): boolean {
  return pathname === section.href || pathname.startsWith(`${section.href}/`);
}
