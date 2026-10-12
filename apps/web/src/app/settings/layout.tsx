import { SettingsFrame } from "@/components/settings/settings-frame";

export default function SettingsLayout({ children }: LayoutProps<"/settings">) {
  return <SettingsFrame>{children}</SettingsFrame>;
}
